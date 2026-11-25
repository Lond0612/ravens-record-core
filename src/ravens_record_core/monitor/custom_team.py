"""チームのカスタムマッチ（CUSTOM MATCH の対戦形式「チーム」。v2.2〜）の試合を記録する

試合中の画面はチームのランクマッチと同じ（上のポイント・残り時間・自分の SCORE・撃破の流れ）なので、同じ部品
（team_hud / TeamTimeline / match_end の帯）で読む。ランクマッチと違うところ:
  - 試合のあと（FINISH → YOU WIN / YOU LOSE の帯 → リザルト画面 → 通信中の表示 → ロード）は、ロビーではなくルームへ戻る。
    リザルト画面はランクマッチと同じ読み取り（team_match.read_team_result）。読めなかったときは、勝敗は帯から、
    ポイント（両チームの合計・自分の SCORE）は試合中の表示で最後に読めた値から、自分の陣営は上の見出しの色から決める
  - 制限時間がルームの設定で変わる（読めなければ 5 分とみなす）
  - レートは無い。マップはリザルト画面から（照合できなければルームの設定から）
記録はチームのランクマッチと同じ形（team_match.build_payload）に match_kind = "custom" とルームの設定を足し、
同じ保存先・同じ知らせ（on_team_match_*）で使う側のアプリへ渡す（送り先の表も team_matches。match_kind で分ける）。

観戦（カメラのマーク。v2.2〜）: 観戦の画面も、試合中の表示は同じ（左が ALPHA・右が BETA。左下の SCORE はカメラが追っている人の点数）。
試合の終わりは FINISH → SQUAD-ALPHA WINS（または BETA）の帯 → リザルト画面（左 ALPHA・右 BETA。自分の行は無い）→ ルーム。
帯は読まず、リザルト画面まで試合中の表示を読み続ける。ALPHA・BETA のポイントはリザルト画面から（読めなければ、試合の終わりまで
見ていたときだけ、試合中の表示の最後の値）。試合の流れは、自チーム = ALPHA として読み、観戦では意味の無いもの（自分の SCORE・
REBOOTING・番号）を除く。記録は on_custom_match（kind = team_spectated）で渡す（送り先の表は team_spectated_matches）。
"""

from __future__ import annotations

import re
import time
from datetime import datetime

from ravens_record_core import logger, matchlog
from ravens_record_core.detectors import custom_match, match_end, team_match
from ravens_record_core.detectors.loading_sequence_detector import capture_frame_with_fallback
from ravens_record_core.detectors.mode_detector import detect_current_screen_mode
from ravens_record_core.detectors.team_timeline import TeamTimeline
from ravens_record_core.hooks import CUSTOM_TEAM_SPECTATED
from ravens_record_core.monitor.base import MonitorContext, is_process_running, log
from ravens_record_core.monitor.children import start_loading_watcher, stop_loading_watcher
from ravens_record_core.monitor.team import RESULT_POLL_SEC, RESULT_WAIT_AFTER_BANNER_SEC, _read_result_screen

DEFAULT_TIME_LIMIT_SEC = 300
# 対戦の開始の合図から、試合が終わるまでを待つ最長の秒数（ロード ＋ 紹介 ＋ 制限時間 ＋ 余裕）
LOAD_ALLOWANCE_SEC = 300.0
# 帯のあと、ルームへの復帰を待つ最長の秒数（通信中の表示とロードがある）
ROOM_RETURN_TIMEOUT_SEC = 120.0
# 残り時間がこの秒数以下になったら（または残り時間が読めなくなったら）、帯を探し始める
BANNER_SEARCH_REMAINING_SEC = 15
POLL_SEC = 1.0
BANNER_POLL_SEC = 0.3
# 観戦と判断する、カメラのマークが続けて見えた回数
CAMERA_FRAMES = 3
# 合図のあと、ルーム画面の見出しが見えても戻ったとみなさない秒数（合図はルーム画面の上に出るため）
START_GRACE_SEC = 20.0
# 観戦: 試合の最初のこの秒数の間も、カメラのマークを探す（マークは試合中の表示と一緒に出るため、残り時間より先とは限らない）
CAMERA_SEARCH_SEC = 30.0
# 観戦: 残り時間がこの秒数以下・読めなくなったら、リザルト画面を見逃さないよう、短い間隔で読む（リザルト画面は 1〜2 秒で終わることがある）
SPECTATE_END_REMAINING_SEC = 5
SPECTATE_END_POLL_SEC = 0.3
# 観戦: リザルト画面を読めなかったとき、試合中の表示の最後の値を使うのは、試合の終わり（残りこの秒数以下）まで見ていたときだけ
SPECTATE_HUD_FALLBACK_REMAINING_SEC = 3
SPECTATING = "spectating"   # _watch_until_banner が、観戦と分かったときに返す


def time_limit_sec(settings) -> int:
    """ルームの制限時間（例: 「5分」「5 min」「5분」）を秒にする。読めなければ 5 分"""
    m = re.search(r"(\d{1,2})", getattr(settings, "time_limit_raw", "") or "")
    if m and 1 <= int(m.group(1)) <= 30:
        return int(m.group(1)) * 60
    return DEFAULT_TIME_LIMIT_SEC


def run_custom_team_match(ctx: MonitorContext, settings) -> dict | None:
    """チームのカスタムマッチの試合 1 回を記録する。対戦の開始の合図の直後に呼ぶ。記録した payload を返す"""
    ctx.hooks.on_team_match_started()
    start_loading_watcher(ctx)
    payload, spectated = None, False
    try:
        payload, spectated = _record(ctx, settings)
    finally:
        stop_loading_watcher(ctx, grace_sec=0.0)
        # 観戦は、参加した試合の知らせ（クリップ・診断ログ）には渡さない
        ctx.hooks.on_team_match_ended(None if spectated else payload)
    return payload


def _capture(ctx: MonitorContext):
    return capture_frame_with_fallback(ctx.trigger_exe)


def _record(ctx: MonitorContext, settings) -> tuple[dict | None, bool]:
    """(記録した payload | None, 観戦か)"""
    played_at = datetime.now().astimezone()
    started = time.monotonic()
    limit = time_limit_sec(settings)
    logger.emit("custom_team_detected", f"[カスタム] チームの対戦の開始を検出しました（{settings.summary()}）",
                msg_key="event.customTeamDetected", map=settings.map_match)
    ctx.set_status("loading", "チームのカスタムマッチを検出しました。開始を待っています", "status.customLoading")
    timeline = TeamTimeline(None, None, match_sec=limit)

    banner = _watch_until_banner(ctx, started, timeline, limit)
    if banner == SPECTATING:
        return _record_spectated(ctx, settings, played_at, started, timeline, limit), True
    if banner is None:
        return None, False
    ended_at = datetime.now().astimezone()
    ctx.set_status("post_match", "チームのカスタムマッチが終了しました。結果を保存しています", "status.customPostMatch")
    # 帯のあとのリザルト画面（チームのランクマッチと同じ画面・同じ読み取り）
    result = _wait_result_screen(ctx)
    _wait_room_return(ctx)

    hud = timeline.hud_totals()
    if result is not None:
        for note in result.settle_points(hud.get("own"), hud.get("opp")):
            log(f"[カスタム] リザルト画面の合計を直しました: {note}")
    payload = team_match.build_payload(played_at, ended_at, result, banner, timeline.hud_side, None, None, _app_version(ctx))
    # リザルト画面を読めなかったときは、試合中の表示で最後に読めた値で補う
    if payload["own_side"] is not None:
        for key, field in (("own_points", "own"), ("opp_points", "opp"), ("own_score", "score")):
            if payload[key] is None:
                payload[key] = hud.get(field)
    payload["match_kind"] = "custom"
    payload["settings_raw"] = {"format_raw": settings.format_raw, "time_limit_raw": settings.time_limit_raw,
                               "swap_mode_raw": settings.swap_mode_raw, "map_raw": settings.map_raw}
    # マップ: リザルト画面で照合できなければ、ルームの設定から
    if payload.get("map_hint") is None and settings.map_match:
        payload["map_raw"] = settings.map_raw or None
        payload["map_hint"] = {"match": settings.map_match, "score": settings.read_confidence.get("map", 0.0), "lang": settings.game_lang}
    detail = timeline.finish(payload["own_side"], payload["own_points"], payload["opp_points"], payload["own_score"])
    if detail is not None:
        payload.update(detail)
    # 試合の流れの 0 秒目の時刻（試合のクリップの再生位置と合わせる。記録には入れない）
    if (start_epoch := timeline.start_epoch()) is not None:
        ctx.hooks.on_team_match_clock(start_epoch)
    path = matchlog.write_team_match(ctx.config.logs_dir, payload, ended_at)
    if ctx.hooks.on_team_match(payload, path):
        own, opp = payload.get("own_points"), payload.get("opp_points")
        score = f"{own}-{opp}" if own is not None and opp is not None else "ポイント不明"
        logger.emit("custom_team_saved", f"チームのカスタムマッチを記録しました: {payload['result'].upper()} {score} / "
                    f"{settings.map_match or settings.map_raw or 'マップ不明'}",
                    result=payload["result"], own_points=own, opp_points=opp, msg_key="event.customTeamSaved")
    return payload, False


def _record_spectated(ctx: MonitorContext, settings, started_at: datetime, started: float, timeline: TeamTimeline,
                      limit: int) -> dict | None:
    """観戦したチームの対戦 1 回を記録する（カメラのマークを確認した直後に呼ぶ）。記録した payload を返す"""
    ctx.set_status("in_match", "チームのカスタムマッチを観戦中です", "status.customTeamSpectating")
    log("[カスタム] チームの対戦を観戦しています。観戦の記録として、試合の終わりまで読みます。")
    result = None
    last_mode_check = 0.0
    while True:
        if ctx.stop_requested() or not is_process_running(ctx.trigger_exe):
            log("[カスタム] AC6 が閉じられた（または監視を止めた）ため、観戦の記録を中断します。")
            return None
        now = time.monotonic()
        if now - started > limit + LOAD_ALLOWANCE_SEC:
            log("[カスタム] 観戦した対戦の終わりを確認できないまま時間が過ぎました。")
            break
        frame = _capture(ctx)
        if frame is not None:
            if team_match.read_team_result(frame, with_map=False) is not None:
                result = _read_result_screen(ctx, frame)
                break
            timeline.observe(now, frame)
        near_end = timeline.started and (limit - timeline.last_t <= SPECTATE_END_REMAINING_SEC or not timeline.timer_visible)
        # リザルト画面を見逃してルームへ戻ったか（試合の終わりぎわから、ときどき確かめる）
        if near_end and now - last_mode_check >= 3.0:
            last_mode_check = now
            if detect_current_screen_mode(ctx.trigger_exe, in_match=True).mode == "CUSTOM":
                log("[カスタム] 観戦した対戦のリザルト画面を読めないまま、ルームへ戻りました。")
                break
        time.sleep(SPECTATE_END_POLL_SEC if near_end else POLL_SEC)
    ended_at = datetime.now().astimezone()
    ctx.set_status("post_match", "観戦したチームの対戦が終了しました。結果を保存しています", "status.customPostMatch")
    _wait_room_return(ctx)

    alpha = result.points.get("ALPHA") if result is not None else None
    beta = result.points.get("BETA") if result is not None else None
    hud = timeline.hud_totals()
    if (alpha is None or beta is None) and timeline.started and limit - timeline.last_t <= SPECTATE_HUD_FALLBACK_REMAINING_SEC:
        alpha = alpha if alpha is not None else hud.get("own")
        beta = beta if beta is not None else hud.get("opp")
    if alpha is None or beta is None:
        logger.emit("custom_team_spectated_skipped", "[カスタム] 観戦したチームの対戦の結果を読み取れなかったため、記録しませんでした",
                    level="warn", msg_key="event.customTeamSpectatedSkipped")
        return None
    payload = team_match.build_spectated_payload(started_at, ended_at, alpha, beta, result, _app_version(ctx))
    payload["settings_raw"] = {"format_raw": settings.format_raw, "time_limit_raw": settings.time_limit_raw,
                               "swap_mode_raw": settings.swap_mode_raw, "map_raw": settings.map_raw}
    if payload.get("map_hint") is None and settings.map_match:
        payload["map_raw"] = settings.map_raw or None
        payload["map_hint"] = {"match": settings.map_match, "score": settings.read_confidence.get("map", 0.0), "lang": settings.game_lang}
    detail = timeline.finish("ALPHA", alpha, beta, None)
    if detail is not None:
        payload.update(team_match.spectated_timeline(detail))
    # 試合の流れの 0 秒目の時刻（観戦のクリップの再生位置と合わせる。記録には入れない）
    if (start_epoch := timeline.start_epoch()) is not None:
        ctx.hooks.on_team_match_clock(start_epoch)
    path = matchlog.write_team_match(ctx.config.logs_dir, {**payload, "played_at": payload["started_at"], "spectated": True,
                                                            "own_points": alpha, "opp_points": beta}, ended_at)
    if ctx.hooks.on_custom_match(CUSTOM_TEAM_SPECTATED, payload, path):
        map_name = (payload.get("map_hint") or {}).get("match") or payload.get("map_raw") or "マップ不明"
        logger.emit("custom_team_spectated_saved", f"観戦したチームのカスタムマッチを記録しました: ALPHA {alpha}-{beta} BETA / {map_name}",
                    alpha_points=alpha, beta_points=beta, msg_key="event.customTeamSpectatedSaved")
    return payload


def _watch_until_banner(ctx: MonitorContext, started: float, timeline: TeamTimeline, limit: int) -> str | None:
    """試合の終わりの帯（YOU WIN / YOU LOSE）まで、試合中の表示を読み続ける。帯の勝敗を返す（記録しないなら None）"""
    camera = 0
    last_mode_check = 0.0
    while True:
        if ctx.stop_requested() or not is_process_running(ctx.trigger_exe):
            log("[カスタム] AC6 が閉じられた（または監視を止めた）ため、チームの対戦の記録を中断します。")
            return None
        now = time.monotonic()
        elapsed = now - started
        if elapsed > limit + LOAD_ALLOWANCE_SEC:
            log("[カスタム] チームの対戦の終わりを確認できないまま時間が過ぎました。記録しません。")
            return None
        frame = _capture(ctx)
        searching = False
        if frame is not None:
            had_timer = timeline.started
            timeline.observe(now, frame)
            # 観戦（カメラのマーク）か: 試合の画面になる前と、試合の最初の CAMERA_SEARCH_SEC 秒の間に探す
            if not timeline.started or timeline.last_t <= CAMERA_SEARCH_SEC:
                camera = camera + 1 if custom_match.has_spectator_camera(frame, team=True) else 0
                if camera >= CAMERA_FRAMES:
                    return SPECTATING
            if timeline.started:
                if not had_timer:
                    ctx.set_status("in_match", "チームのカスタムマッチの対戦中です", "status.customInMatch")
                    log("[カスタム] チームの対戦の画面を確認しました。")
                remaining = limit - timeline.last_t
                # 残りが少ない・残り時間が読めなくなった（試合が止まった）ときだけ、帯を読む
                searching = remaining <= BANNER_SEARCH_REMAINING_SEC or not timeline.timer_visible
                if searching:
                    b = match_end.read_banner(frame)
                    if b is not None and b[0] == "final":
                        log(f"[カスタム] チームの対戦の終わりの帯を読みました: YOU {b[2].upper()}")
                        return b[2]
        # 試合の画面にならないままルームへ戻った（中止）・ほかのモードへ移ったかを、ときどき確かめる
        if now - last_mode_check >= 10.0 and elapsed >= START_GRACE_SEC:
            last_mode_check = now
            mode = detect_current_screen_mode(ctx.trigger_exe, in_match=True).mode
            if mode in ("SINGLE", "TEAM", "CUSTOM"):
                log(f"[カスタム] チームの対戦の終わりを確認しないまま {mode} の画面に戻りました。記録しません。")
                return None
        time.sleep(BANNER_POLL_SEC if searching else POLL_SEC)


def _wait_result_screen(ctx: MonitorContext):
    """帯のあと、リザルト画面を待って読む（monitor/team.py と同じ読み方）。出なければ None"""
    end = time.monotonic() + RESULT_WAIT_AFTER_BANNER_SEC
    while time.monotonic() < end:
        if not is_process_running(ctx.trigger_exe):
            return None
        frame = _capture(ctx)
        if frame is not None and team_match.read_team_result(frame, with_map=False) is not None:
            return _read_result_screen(ctx, frame)
        time.sleep(RESULT_POLL_SEC)
    log("[カスタム] 帯のあと、リザルト画面を読めませんでした。帯の勝敗と試合中の表示で記録します。")
    return None


def _wait_room_return(ctx: MonitorContext) -> bool:
    """ルーム画面（CUSTOM MATCH）に戻るまで待つ（次の対戦の開始の合図を、この試合の帯の名残と取り違えないため）"""
    start = time.monotonic()
    while time.monotonic() - start <= ROOM_RETURN_TIMEOUT_SEC:
        if not is_process_running(ctx.trigger_exe):
            return False
        mode = detect_current_screen_mode(ctx.trigger_exe).mode
        if mode == "CUSTOM":
            return True
        if mode in ("SINGLE", "TEAM"):
            return False
        time.sleep(1.0)
    log("[カスタム] チームの対戦のあと、ルーム画面への復帰を確認できませんでした。")
    return False


def _app_version(ctx: MonitorContext) -> str:
    from ravens_record_core import __version__
    return ctx.config.app_version or __version__
