"""チームのカスタムマッチ（CUSTOM MATCH の対戦形式「チーム」。v2.2〜）の試合を記録する

試合中の画面はチームのランクマッチと同じ（上のポイント・残り時間・自分の SCORE・撃破の流れ）なので、同じ部品
（team_hud / TeamTimeline / match_end の帯）で読む。ランクマッチと違うところ:
  - リザルト画面が無い（FINISH → YOU WIN / YOU LOSE の帯 → 通信中の表示 → ロード → ルームへ戻る）。
    勝敗は帯から、ポイント（両チームの合計・自分の SCORE）は試合中の表示で最後に読めた値から、自分の陣営は上の見出しの色から決める
  - 制限時間がルームの設定で変わる（読めなければ 5 分とみなす）
  - レートは無い。マップはルームの設定から
記録はチームのランクマッチと同じ形（team_match.build_payload）に match_kind = "custom" とルームの設定を足し、
同じ保存先・同じ知らせ（on_team_match_*）で使う側のアプリへ渡す（送り先の表も team_matches。match_kind で分ける）。
観戦（カメラのマーク）のときは記録しない。
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
from ravens_record_core.monitor.base import MonitorContext, is_process_running, log
from ravens_record_core.monitor.children import start_loading_watcher, stop_loading_watcher

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
    payload = None
    try:
        payload = _record(ctx, settings)
    finally:
        stop_loading_watcher(ctx, grace_sec=0.0)
        ctx.hooks.on_team_match_ended(payload)
    return payload


def _capture(ctx: MonitorContext):
    return capture_frame_with_fallback(ctx.trigger_exe)


def _record(ctx: MonitorContext, settings) -> dict | None:
    played_at = datetime.now().astimezone()
    started = time.monotonic()
    limit = time_limit_sec(settings)
    logger.emit("custom_team_detected", f"[カスタム] チームの対戦の開始を検出しました（{settings.summary()}）",
                msg_key="event.customTeamDetected", map=settings.map_match)
    ctx.set_status("loading", "チームのカスタムマッチを検出しました。開始を待っています", "status.customLoading")
    timeline = TeamTimeline(None, None, match_sec=limit)

    banner = _watch_until_banner(ctx, started, timeline, limit)
    if banner is None:
        return None
    ended_at = datetime.now().astimezone()
    ctx.set_status("post_match", "チームのカスタムマッチが終了しました。結果を保存しています", "status.customPostMatch")
    _wait_room_return(ctx)

    hud = timeline.hud_totals()
    payload = team_match.build_payload(played_at, ended_at, None, banner, timeline.hud_side, None, None, _app_version(ctx))
    if payload["own_side"] is not None:
        payload["own_points"] = hud.get("own")
        payload["opp_points"] = hud.get("opp")
        payload["own_score"] = hud.get("score")
    payload["match_kind"] = "custom"
    payload["result_basis"] = "banner"
    payload["settings_raw"] = {"format_raw": settings.format_raw, "time_limit_raw": settings.time_limit_raw,
                               "swap_mode_raw": settings.swap_mode_raw, "map_raw": settings.map_raw}
    payload["map_raw"] = settings.map_raw or None
    payload["map_hint"] = ({"match": settings.map_match, "score": settings.read_confidence.get("map", 0.0), "lang": settings.game_lang}
                           if settings.map_match else None)
    detail = timeline.finish(payload["own_side"], payload["own_points"], payload["opp_points"], payload["own_score"])
    if detail is not None:
        payload.update(detail)
    path = matchlog.write_team_match(ctx.config.logs_dir, payload, ended_at)
    if ctx.hooks.on_team_match(payload, path):
        own, opp = payload.get("own_points"), payload.get("opp_points")
        score = f"{own}-{opp}" if own is not None and opp is not None else "ポイント不明"
        logger.emit("custom_team_saved", f"チームのカスタムマッチを記録しました: {payload['result'].upper()} {score} / "
                    f"{settings.map_match or settings.map_raw or 'マップ不明'}",
                    result=payload["result"], own_points=own, opp_points=opp, msg_key="event.customTeamSaved")
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
            if not timeline.started:
                # 試合の画面になる前: 観戦（カメラのマーク）なら記録しない
                camera = camera + 1 if custom_match.has_spectator_camera(frame) else 0
                if camera >= CAMERA_FRAMES:
                    log("[カスタム] チームの対戦を観戦しています。観戦は記録しません。")
                    _wait_room_return(ctx)
                    return None
            else:
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
