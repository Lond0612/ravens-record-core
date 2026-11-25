"""チーム戦（RANK MATCH: TEAM）の試合を記録する（v1.8〜。team_match.py）

流れ（シングルのランクマッチと同じところは、同じ部品を使う）:
  ロビーで「対戦を開始します」を待つ（match_start）→ 対戦前のレート（S ランクだけ。rate_detector）→
  試合の終わりまで画面を見る（ロード・紹介・3 分の試合）→ 試合の終わりの帯（YOU WIN / YOU LOSE）と、リザルト画面を読む →
  ロビーへの復帰を待って、対戦後のレート（S ランクだけ）→ 保存（matchlog.write_team_match）

チーム戦のロードはシングルより長い（約 1 分）ので、シングルのロードの判定（loading・map_detection）は使わず、
リザルト画面が出るまで待つ。マップは、リザルト画面に出る名前を読む。
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path

import cv2

from ravens_record_core import logger, matchlog
from ravens_record_core.detectors import match_end, team_hud, team_match
from ravens_record_core.detectors.team_timeline import TeamTimeline
from ravens_record_core.detectors.loading_sequence_detector import capture_frame_with_fallback
from ravens_record_core.detectors.mode_detector import detect_current_screen_mode
from ravens_record_core.detectors.rate_detector import capture_stable_rate
from ravens_record_core.monitor.base import MonitorContext, is_process_running, log
from ravens_record_core.monitor.children import start_loading_watcher, stop_loading_watcher
from ravens_record_core.monitor.match_start import wait_for_match_start
from ravens_record_core.monitor.rate_reader import log_rate_failure, read_rate_after
from ravens_record_core.system import screencap

# 対戦の開始の合図から、リザルト画面が出るまでを待つ最長の秒数（ロード約 1 分 ＋ 紹介 ＋ 試合 3 分 ＋ 余裕）
TEAM_MATCH_MAX_SEC = 600.0
# 対戦の開始の合図から、試合の終わりの帯を探し始めるまでの秒数（試合は 3 分の時間切れで終わる。
# 試合中の爆発などの色で、帯の文字の読み取りを何度も走らせないため）
BANNER_SEARCH_AFTER_SEC = 150.0
# 画面を見る間隔（試合中・帯を探す間・リザルト画面を読む間）
POLL_SEC = 1.0
BANNER_POLL_SEC = 0.3
RESULT_POLL_SEC = 0.25
# リザルト画面を読み続ける最長の秒数（単独で見えるのは 2 秒ほど。そのあとランクポイント変動のダイアログが重なる）
RESULT_READ_SEC = 3.0
# 帯を見たあと、リザルト画面が出るまで待つ最長の秒数
RESULT_WAIT_AFTER_BANNER_SEC = 25.0
# 試合のあと、チーム戦のロビーへの復帰を待つ最長の秒数
LOBBY_RETURN_TIMEOUT_SEC = 60.0
# 試合中、自分の陣営（上の左の見出しの色）を読む間隔
SIDE_READ_INTERVAL_SEC = 5.0
# 調査用の控え（データの保存先に team_debug.flag があるときだけ。端末の中だけに残し、送らない）
DEBUG_FLAG = "team_debug.flag"
DEBUG_KEEP = 10                 # 残す試合の数（古いものから消す）
DEBUG_FRAME_EVERY_SEC = 10.0    # 試合中の画面を縮小して残す間隔
DEBUG_FRAME_WIDTH = 960


class TeamDebug:
    """調査用の控え: 1 回ごとの読み取り（samples.jsonl）、ロビーの自分のエンブレム、リザルト画面、試合中の画面（縮小）、記録した中身"""

    def __init__(self, ctx: MonitorContext) -> None:
        root = Path(ctx.config.data_dir) / "team_debug"
        self.dir = root / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.samples: list = []
        self.last_frame = 0.0
        self.result_frames = 0
        old = sorted(p for p in root.iterdir() if p.is_dir())
        for p in old[:-DEBUG_KEEP]:
            shutil.rmtree(p, ignore_errors=True)

    @staticmethod
    def enabled(ctx: MonitorContext) -> bool:
        try:
            return (Path(ctx.config.data_dir) / DEBUG_FLAG).exists()
        except Exception:
            return False

    def image(self, name: str, img, width: int | None = None) -> None:
        try:
            if img is None or img.size == 0:
                return
            if width and img.shape[1] > width:
                img = cv2.resize(img, (width, round(img.shape[0] * width / img.shape[1])), interpolation=cv2.INTER_AREA)
            params = [cv2.IMWRITE_JPEG_QUALITY, 85] if name.endswith(".jpg") else []
            cv2.imwrite(str(self.dir / name), img, params)
        except Exception as e:
            log(f"[チーム戦] [調査用] 画像を残せませんでした: {e!r}")

    def frame(self, now: float, frame) -> None:
        if now - self.last_frame >= DEBUG_FRAME_EVERY_SEC:
            self.last_frame = now
            self.image(f"frame_{len(list(self.dir.glob('frame_*.jpg'))):03d}.jpg", frame, DEBUG_FRAME_WIDTH)

    def result(self, frame) -> None:
        if self.result_frames < 6:
            self.image(f"result_{self.result_frames}.jpg", frame)
            self.result_frames += 1

    def finish(self, payload: dict | None) -> None:
        try:
            with open(self.dir / "samples.jsonl", "w", encoding="utf-8") as f:
                for s in self.samples:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")
            (self.dir / "payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            log(f"[チーム戦] [調査用] 控えを残しました: {self.dir}")
        except Exception as e:
            log(f"[チーム戦] [調査用] 控えを残せませんでした: {e!r}")


def run_team_lobby(ctx: MonitorContext, watcher) -> None:
    """チーム戦のロビー: 対戦の開始を待ち、見つけたらその試合を記録する（ロビーを離れたら戻る）"""
    if wait_for_match_start(ctx, watcher, mode="TEAM"):
        record_team_match(ctx)


def record_team_match(ctx: MonitorContext) -> dict | None:
    """チーム戦の試合 1 回を記録する。対戦の開始の合図の直後に呼ぶ。記録した payload を返す（記録しなければ None）。
    始まりと終わりを、使う側のアプリに知らせる（試合のクリップの録画などに使う）"""
    ctx.hooks.on_team_match_started()
    # ロード画面の監視（ランクマッチと同じ）: ALPHA の紹介の映像が始まった時刻を知らせる（on_intro_start。クリップの始まりに使う）
    start_loading_watcher(ctx)
    payload = None
    try:
        payload = _record(ctx)
    finally:
        stop_loading_watcher(ctx, grace_sec=0.0)
        ctx.hooks.on_team_match_ended(payload)
    return payload


def _capture(ctx: MonitorContext):
    return capture_frame_with_fallback(ctx.trigger_exe)


def _record(ctx: MonitorContext) -> dict | None:
    played_at = datetime.now().astimezone()
    started = time.monotonic()
    logger.emit("team_match_detected", "[チーム戦] 対戦の開始を検出しました", msg_key="event.teamDetected")
    ctx.set_status("loading", "チーム戦の対戦を検出。ロード中です", "status.teamLoading")

    # 自分のエンブレム（ロビーのネームプレート）: 試合中の自チームの欄と照らし合わせて、自分の番号を決める
    lobby = _capture(ctx)
    debug = TeamDebug(ctx) if TeamDebug.enabled(ctx) else None
    emblem = team_hud.lobby_emblem(lobby) if lobby is not None else None
    timeline = TeamTimeline(emblem, debug.samples if debug else None)
    if debug:
        debug.image("lobby_emblem.png", emblem)
        debug.image("lobby.jpg", lobby, DEBUG_FRAME_WIDTH)
    rate_before, rate_note = _read_rate_before(ctx)
    ctx.set_status("in_match", "チーム戦の対戦中です", "status.teamInMatch")

    ended = _watch_until_result(ctx, started, timeline, debug)
    if ended is None:
        if debug:
            debug.finish(None)
        return None
    result, banner, hud_side = ended
    ended_at = datetime.now().astimezone()
    ctx.set_status("post_match", "チーム戦の対戦が終了しました。結果を保存しています", "status.teamPostMatch")

    rate_after = None
    if (rate_before is not None or rate_note != "not_s_rank") and _wait_for_team_lobby(ctx):
        rate_after = _read_rate_after(ctx, rate_before, rate_note)

    if result is not None:
        hud = timeline.hud_totals()
        for note in result.settle_points(hud.get("own"), hud.get("opp")):
            log(f"[チーム戦] リザルト画面の合計を直しました: {note}")
    payload = team_match.build_payload(played_at, ended_at, result, banner, hud_side, rate_before, rate_after, _app_version(ctx))
    # 試合の流れ（ポイントの推移・撃破・プライオリティ・自分の撃破）。最後の合計は、リザルト画面の値で補う
    detail = timeline.finish(payload["own_side"], payload["own_points"], payload["opp_points"], payload["own_score"])
    if detail is not None:
        payload.update(detail)
    # 試合の流れの 0 秒目の時刻（試合のクリップの再生位置と合わせる。記録には入れない）
    if (start_epoch := timeline.start_epoch()) is not None:
        ctx.hooks.on_team_match_clock(start_epoch)
    if debug:
        debug.finish(payload)
    if payload["result"] is None:
        logger.emit("team_match_skipped", "[チーム戦] 試合の結果を読み取れなかったため、記録しませんでした",
                    level="warn", msg_key="event.teamSkipped")
        return None
    path = matchlog.write_team_match(ctx.config.logs_dir, payload, ended_at)
    if ctx.hooks.on_team_match(payload, path):
        own, opp = payload.get("own_points"), payload.get("opp_points")
        score = f"{own}-{opp}" if own is not None and opp is not None else "ポイント不明"
        logger.emit("team_match_saved", f"チーム戦を記録しました: {payload['result'].upper()} {score} / "
                    f"{(payload.get('map_hint') or {}).get('match') or payload.get('map_raw') or 'マップ不明'}",
                    result=payload["result"], own_points=own, opp_points=opp, msg_key="event.teamSaved")
    return payload


def _read_rate_before(ctx: MonitorContext) -> tuple[int | None, str | None]:
    """対戦前のレート（S ランクのときだけ。ロビーの MATCH INFO の配置はシングルと同じ）"""
    trace: dict = {}
    res = capture_stable_rate(lambda: _capture(ctx), trace=trace)
    if res and res.is_s_rank:
        return res.rate, None
    if res:
        log("[チーム戦] Sランクではないため、レートは記録しません。")
        return None, "not_s_rank"
    log_rate_failure("対戦前レート（チーム戦）", trace)
    return None, trace.get("reason")


def _read_rate_after(ctx: MonitorContext, rate_before: int | None, rate_note: str | None) -> int | None:
    trace: dict = {}
    res = read_rate_after(rate_before, lambda: _capture(ctx), trace=trace, require_s_rank=rate_note == "not_s_rank")
    if res:
        return res.rate
    log_rate_failure("対戦後レート（チーム戦）", trace)
    return None


def _watch_until_result(ctx: MonitorContext, started: float, timeline: TeamTimeline | None = None, debug: TeamDebug | None = None):
    """リザルト画面を読み終えるまで待つ。(リザルト画面の読み取り | None, 帯の勝敗 | None, 試合中に読んだ自分の陣営 | None)。
    試合が終わらないまま AC6 が閉じた・ほかのモードへ移った・時間切れなら None"""
    banner: str | None = None
    banner_at: float | None = None
    hud_side: str | None = None
    last_side_read = 0.0
    last_mode_check = 0.0
    while True:
        if ctx.stop_requested() or not is_process_running(ctx.trigger_exe):
            log("[チーム戦] AC6 が閉じられた（または監視を止めた）ため、記録を中断します。")
            return None
        now = time.monotonic()
        elapsed = now - started
        if elapsed > TEAM_MATCH_MAX_SEC or (banner_at is not None and now - banner_at > RESULT_WAIT_AFTER_BANNER_SEC):
            if banner is not None:
                log("[チーム戦] リザルト画面を読めないまま時間が過ぎました。試合の終わりの帯の勝敗で記録します。")
                return None, banner, hud_side
            log("[チーム戦] 試合の終わりを確認できないまま時間が過ぎました。記録しません。")
            return None
        frame = _capture(ctx)
        if frame is not None:
            result = team_match.read_team_result(frame, with_map=False)
            if result is None and timeline is not None:
                timeline.observe(time.monotonic(), frame)
            if debug and result is None:
                debug.frame(now, frame)
            if result is not None:
                return _read_result_screen(ctx, frame, debug), banner, hud_side
            if hud_side is None and now - last_side_read >= SIDE_READ_INTERVAL_SEC:
                last_side_read = now
                hud_side = match_end.read_own_side(frame)
            if elapsed >= BANNER_SEARCH_AFTER_SEC and banner is None:
                b = match_end.read_banner(frame)
                if b is not None and b[0] == "final":
                    banner, banner_at = b[2], now
                    log(f"[チーム戦] 試合の終わりの帯を読みました: YOU {banner.upper()}")
        # 試合の前（ロード中）にロビーへ戻った（対戦が流れた）・ほかのモードへ移ったかを、ときどき確かめる
        if banner is None and now - last_mode_check >= 10.0:
            last_mode_check = now
            mode = detect_current_screen_mode(ctx.trigger_exe, in_match=True).mode
            if mode in ("SINGLE", "CUSTOM") or (mode == "TEAM" and elapsed > 45.0):
                log(f"[チーム戦] 試合の終わりを確認しないまま {mode} の画面に戻りました。記録しません。")
                return None
        time.sleep(RESULT_POLL_SEC if banner is not None else BANNER_POLL_SEC if elapsed >= BANNER_SEARCH_AFTER_SEC else POLL_SEC)


def _read_result_screen(ctx: MonitorContext, first_frame, debug: TeamDebug | None = None) -> team_match.TeamResult | None:
    """リザルト画面を、見えている間（2 秒ほど）何枚か読んで、読めたところを合わせる"""
    if debug:
        debug.result(first_frame)
    merged = team_match.read_team_result(first_frame)
    end = time.monotonic() + RESULT_READ_SEC
    while merged is not None and time.monotonic() < end and not (merged.complete and merged.all_verified and merged.own_points is not None and merged.map_match):
        time.sleep(RESULT_POLL_SEC)
        frame = _capture(ctx)
        r = team_match.read_team_result(frame) if frame is not None else None
        if r is not None:
            if debug:
                debug.result(frame)
            merged.merge(r)
    if merged is not None:
        log(f"[チーム戦] リザルト画面を読みました: {merged.outcomes} {merged.points} {merged.rows} 自分の陣営 {merged.own_side} "
            f"{merged.own_points}PT / マップ {merged.map_raw!r} → {merged.map_match}")
    return merged


def _wait_for_team_lobby(ctx: MonitorContext) -> bool:
    """チーム戦のロビーへの復帰を待つ。戻れば True。
    リザルト画面にも「RANK MATCH: TEAM」の文字があるため、リザルト画面の間は戻ったとみなさない"""
    start = time.monotonic()
    while time.monotonic() - start <= LOBBY_RETURN_TIMEOUT_SEC:
        if not is_process_running(ctx.trigger_exe):
            return False
        frame = _capture(ctx)
        on_result = frame is not None and match_end.is_result_screen(screencap.menu_area(frame)[0])
        if not on_result and detect_current_screen_mode(ctx.trigger_exe).mode == "TEAM":
            return True
        time.sleep(0.3)
    log("[チーム戦] 60 秒以内にチーム戦のロビーへの復帰を確認できませんでした（対戦後のレートは読みません）。")
    return False


def _app_version(ctx: MonitorContext) -> str:
    from ravens_record_core import __version__
    return ctx.config.app_version or __version__
