"""ランクマッチのロビーで、対戦の開始（「対戦を開始します」）を待つ"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from ravens_record_core import logger
from ravens_record_core.detectors import match_status_detector as status_detector
from ravens_record_core.detectors.screen_state_watcher import ScreenStateWatcher
from ravens_record_core.hooks import ISSUE_STATUS_UNREADABLE
from ravens_record_core.monitor.base import WATCH_POLL_INTERVAL_SEC, MonitorContext, is_process_running, log
from ravens_record_core.monitor.screen import screen_watcher

# ランクマッチのロビーで対戦の開始を待つ間、「マッチングしています / 対戦を開始します」を一度も読めないまま
# この秒数が過ぎたら、読み取りの問題として知らせる（Hooks.on_issue の status_unreadable。監視の 1 回につき 1 回だけ）。
# 試合の開始にすら気づけないときは、試合の記録を閉じるときの知らせが一度も出ないため。
# ロビーで放置しているだけのこともあるので、帯（黄色い文字）が出ているのに読めなかった回数が
# STATUS_UNREADABLE_MIN_BAND_READS 回以上のときだけ聞く（帯の出ていない間・最小化中は数えない。MatchStatusDetector.band_visible）。
# 以前は画面を読めた回数（300 回）で判断していたが、ゲームを手前に出したままロビーにいるだけで知らせていた
# 待った時間も、画面を撮れていた間だけ数える（ゲームを裏に回している・最小化中の時間は数えない）。
# 以前は待ち始めからの時間で判断していて、裏に回していた 20 分ほどのあと、手前に戻して 1 分で知らせていた
STATUS_UNREADABLE_SEC = 600.0
STATUS_UNREADABLE_MIN_BAND_READS = 60
# さらに、帯が出ているのに読めなかった回が STATUS_UNREADABLE_MIN_STREAK 回（約 1 秒ごと）以上続いたときだけ聞く（v2.2〜）。
# マッチング中の帯は、マッチングしている間ずっと出ている。ロビーのほかの表示の黄色い画素が、ときどき帯と見分けられない
# だけのときは続かない（2026-11: 新しく入れた人が、ロビーで 17 分、試合をせずにいただけで知らせていた）
STATUS_UNREADABLE_MIN_STREAK = 20

# 以前は「RANK MATCH: SINGLE が 45 秒読めなければ待機をやめる」時間切れがあったが、マッチング中・チャット中などで
# 見出しが読めない間に待機をやめ、そのまま試合に入ると記録されなかったため、なくした（2026-10）。
# いまは「対戦を開始します」が出たときに、最後に読めた見出しが SINGLE かどうかで、ランクマッチかを決める（ScreenStateWatcher）。


def wait_for_match_start(ctx: MonitorContext, watcher: ScreenStateWatcher | None = None, mode: str = "SINGLE") -> bool:
    """ランクマッチ（mode: SINGLE / TEAM）のロビーで、「対戦開始」を検出するまで待機する。

    画面は、見張り（ScreenStateWatcher）が並行して約 1 秒ごとに読む。ここではその結果を確かめるだけ。
    時間切れは無い（マッチング中・キャンセル後・チャット中などで見出しが読めない間も待ち続ける）。

    Returns:
        True  : 最後に読めた見出しが mode の状態で、「対戦を開始します」を検出した。
        False : AC6プロセスが終了／ほかの見出し（SINGLE・TEAM・カスタムマッチ）を読んだため、待機を中断すべき状態。
    """
    watcher = watcher or screen_watcher(ctx)
    if mode == "TEAM":
        ctx.set_status("matchmaking", "ロビー（チーム戦）。対戦の開始を待機中です", "status.teamLobby")
    else:
        ctx.set_status("matchmaking", "ロビー（ランクマッチ）。対戦の開始を待機中です", "status.rankedLobby")
    log("[対戦待機] MATCH_START（対戦を開始します）を待機中...")
    watcher.set_active(True)
    status_detector.keep_unread_strips = True  # 読めなかったときの帯を、端末に数枚残す
    try:
        return _wait_for_match_start(ctx, watcher, mode)
    finally:
        status_detector.keep_unread_strips = False


def _no_frame_reads() -> int:
    """状態の読み取りで、画面を撮れなかった（裏に回している・最小化中）回数"""
    return int(status_detector.health_snapshot().get("no_frame", 0))


def _band_unread_reads() -> int:
    """状態の読み取りで、帯（黄色い文字）が出ているのに読めなかった回数"""
    return int(status_detector.health_snapshot().get("band_unread", 0))


def _maybe_report_status_unreadable(ctx: MonitorContext, watcher: ScreenStateWatcher, waited_sec: float, reads: int) -> None:
    """ロビーで待つ間、状態を一度も読めないまま時間が過ぎたら、読み取りの問題として知らせる（監視の 1 回につき 1 回だけ）"""
    if ctx.status_unreadable_reported or watcher.status_read_count > 0:
        return
    if waited_sec < STATUS_UNREADABLE_SEC or reads < STATUS_UNREADABLE_MIN_BAND_READS:
        return
    if status_detector.health_snapshot().get("band_unread_streak_max", 0) < STATUS_UNREADABLE_MIN_STREAK:
        return
    ctx.status_unreadable_reported = True
    log(f"[対戦待機] [警告] ランクマッチのロビーで {waited_sec / 60:.0f} 分間、「マッチングしています / 対戦を開始します」を"
        "一度も読めていません。対戦の開始を検出できていない可能性があります。")
    detail = {
        "waited_sec": round(waited_sec),
        "context_mode": watcher.context_mode,
        "last_mode": watcher.last_mode,
        "status_reader": status_detector.health_snapshot(),
    }
    ctx.hooks.on_issue(ISSUE_STATUS_UNREADABLE, started_at=datetime.now().astimezone() - timedelta(seconds=waited_sec), detail=detail)


def _wait_for_match_start(ctx: MonitorContext, watcher: ScreenStateWatcher, mode: str = "SINGLE") -> bool:
    last_status_log = 0.0
    wait_started = time.monotonic()
    reads_at_start = _band_unread_reads()
    status_detector.reset_band_streak()
    seen_sec = 0.0  # 画面を撮れていた間の、待った時間
    last_tick = wait_started
    last_no_frame = _no_frame_reads()

    while True:
        if ctx.stop_requested():
            return False
        # AC6が起動しているかを都度確認
        if not is_process_running(ctx.trigger_exe):
            log("[対戦待機] AC6プロセスが終了したため待機を中断します。")
            return False

        event = watcher.take_match_start()
        if event is not None:
            if event.context_mode == mode:
                watcher.set_active(False)  # ここからは、ロード監視・マップ判定・スコアが画面を見る
                logger.emit("match_detected", f"[対戦開始] MATCH_START を検出しました (raw='{event.raw_text}')。ローディングシーケンスを監視します。")
                ctx.set_status("loading", "対戦を検出。ロード中です", "status.loading")
                return True
            log(f"[対戦待機] 対戦開始を検出しましたが、ランクマッチ（{mode}）のロビーからではないため記録しません "
                f"(最後に読めた見出し={event.context_mode})。")
            return False

        # ランクマッチ以外の見出しを読んだら、待機をやめる（呼び出し元が、TEAM・カスタムマッチの処理に移る）
        context = watcher.context_mode
        if context in ("SINGLE", "TEAM") and context != mode:
            log(f"[対戦待機] {context}モードへ遷移したため待機を中断します。")
            return False
        if context == "CUSTOM":
            log("[対戦待機] カスタムマッチのルームへ移ったため待機を中断します。")
            return False

        now = time.monotonic()
        no_frame = _no_frame_reads()
        if no_frame == last_no_frame:
            seen_sec += now - last_tick
        last_no_frame, last_tick = no_frame, now
        if watcher.last_status == "MATCHING":
            if now - last_status_log >= 30.0:
                log(f"[対戦待機] マッチング中... (raw='{watcher.last_status_raw}')")
                last_status_log = now
        else:
            if now - last_status_log >= 60.0:
                log(f"[対戦待機] 状態未検出（マッチング待機前 or 判定不能）。継続中... (raw='{watcher.last_status_raw[:40]}')")
                last_status_log = now
        _maybe_report_status_unreadable(ctx, watcher, seen_sec, _band_unread_reads() - reads_at_start)

        time.sleep(WATCH_POLL_INTERVAL_SEC)
