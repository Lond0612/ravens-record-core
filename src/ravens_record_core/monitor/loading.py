"""対戦の開始の合図のあと、ロードの終わり（暗転 → ロード画面 → 再暗転）を待つ"""

from __future__ import annotations

import time
from enum import Enum

from ravens_record_core.detectors.loading_sequence_detector import LoadingSequenceDetector, SequenceState, capture_frame_with_fallback
from ravens_record_core.detectors.match_status_detector import detect_current_match_status
from ravens_record_core.hooks import ISSUE_LOADING_TIMEOUT
from ravens_record_core.monitor.base import MonitorContext, is_process_running, log
from ravens_record_core.monitor.children import LoadingWatcher
from ravens_record_core.system import aspect_probe

# LoadingSequenceDetector のフレームポーリング間隔（秒）
LOADING_POLL_INTERVAL_SEC = 0.2
# マッチング状態確認間隔（秒）
MATCH_STATUS_CHECK_INTERVAL_SEC = 0.5
# ロード画面出現・完了タイムアウト（秒）
LOADING_TIMEOUT_SEC = 120.0
# 本体が完了と判定したあと、監視プロセスのロード画面の終わりを待つ最長の秒数
WATCHER_GRACE_SEC = 30.0


class LoadingOutcome(Enum):
    """ロードの待ちの結果"""

    COMPLETE = "complete"          # ロードの終わりを確かめた（マップ判定へ進む）
    MAP_SKIPPED = "map_skipped"    # ロード画面は出たが、終わりを確かめられないまま時間切れ（マップ判定を飛ばして、スコアの監視へ進む）
    ABORTED = "aborted"            # 対戦が流れた・ロード画面が出ない・AC6 が終了した（この対戦は記録しない）

    @property
    def done(self) -> bool:
        """スコアの監視へ進んでよいか"""
        return self is not LoadingOutcome.ABORTED


def wait_for_loading_complete(ctx: MonitorContext, watcher: LoadingWatcher | None = None) -> LoadingOutcome:
    """MATCH_START 検出後、LoadingSequenceDetector でシーケンス完了まで待機する。

    LoadingSequenceDetector の状態機械に画面フレームを渡し続け、
    COMPLETE（暗転→ロード画面確定→再暗転）を検出したら、完了とする。

    ロード画面の監視プロセス（watcher）と同時に動かし、どちらか早いほうで完了とする:
      - 監視プロセスがロード画面の終わりを確認した → 本体の状態に関係なく完了（本体は暗転の見逃しで遅れることがあるため）
      - 本体が COMPLETE になったが、監視プロセスはロード画面を表示中と見ている → 本体の誤りとみなし、
        監視プロセスの終わりの確認を待つ（最長 WATCHER_GRACE_SEC 秒。過ぎたら本体の判定を採る）
      - 監視プロセスがロード画面を確認できていない・止まった → 従来どおり本体の判定だけ

    以下の条件では、MATCH_START の処理を破棄する（ABORTED）:
    1. 「マッチングしています（他言語含む）」が検出された場合（対戦キャンセル/再マッチング）
    2. LOADING_TIMEOUT_SEC の間、ロード画面が現れなかった場合
    3. AC6プロセスが終了した場合
    ロード画面は出たのに、終わりを確かめられないまま時間切れになったとき（画面が固まって見えた・暗転の見逃しなど）は、
    対戦が始まっている見込みが高いので、マップ判定を飛ばしてスコアの監視へ進む（MAP_SKIPPED。以前は破棄して、試合を記録できなかった）。
    """
    log("[ロード監視] LoadingSequenceDetector を起動します。暗転→ロード→再暗転を待機中...")

    detector = LoadingSequenceDetector()

    start_time = time.monotonic()
    last_state_log = 0.0
    last_status_check = 0.0

    loading_appeared = False  # 暗転移行以降（ロード画面関連）が開始したかどうかのフラグ
    main_complete_at = None   # 本体が COMPLETE になった時刻（監視プロセスの確認待ちの間）

    while True:
        now = time.monotonic()
        elapsed = now - start_time

        # ロード画面の監視プロセスからの知らせ
        if watcher is not None:
            watcher.poll()
            if watcher.loading_screen:
                loading_appeared = True
            if watcher.loading_end:
                log(f"[ロード監視] ロード画面の監視プロセスが、ロード画面の終わりを確認しました"
                    f"（本体の状態={detector.state.name}）。マップ判定へ進みます。")
                return LoadingOutcome.COMPLETE
            if main_complete_at is not None and (not watcher.watching_loading_screen()
                                                  or now - main_complete_at >= WATCHER_GRACE_SEC):
                log("[ロード監視] 本体の判定（シーケンス完了）を採ります。")
                return LoadingOutcome.COMPLETE

        # AC6プロセス生存確認
        if not is_process_running(ctx.trigger_exe):
            log("[ロード監視] AC6プロセスが終了したため待機を中断します。")
            return LoadingOutcome.ABORTED

        # 30秒タイムアウト判定（ロード画面が現れない / 30秒を超過した場合）
        if elapsed >= LOADING_TIMEOUT_SEC:
            if not loading_appeared:
                log(f"[ロード監視] タイムアウト: {LOADING_TIMEOUT_SEC:.0f}秒間ロード画面が現れませんでした。MATCH_START 処理を破棄し、対戦待機に戻ります。")
                ctx.hooks.on_issue(ISSUE_LOADING_TIMEOUT)  # 試合が記録されない
                return LoadingOutcome.ABORTED
            # ロード画面は出ていた: 対戦は始まっている見込みが高い。マップ判定を飛ばして、スコアの監視へ進む
            # （対戦が流れていたら、スコアが無くレートも変わらないので、試合が流れたとみなされる）
            log(f"[ロード監視] タイムアウト: {LOADING_TIMEOUT_SEC:.0f}秒以内にロードの終わりを確かめられませんでしたが、"
                "ロード画面は出ていたため、マップ判定を飛ばしてスコアの監視へ進みます。")
            return LoadingOutcome.MAP_SKIPPED

        # マッチング状態の判定（他言語対応「マッチングしています」の復帰検出）
        if now - last_status_check >= MATCH_STATUS_CHECK_INTERVAL_SEC:
            last_status_check = now
            status_result = detect_current_match_status(ctx.trigger_exe)
            if status_result.status == "MATCHING":
                log(f"[ロード監視] 「マッチングしています」を検出しました (raw='{status_result.raw_text}')。MATCH_START 処理を破棄し、対戦待機に戻ります。")
                return LoadingOutcome.ABORTED

        if main_complete_at is not None:
            # 本体は完了済み。監視プロセスの確認を待つ。この間に ALPHA の紹介の映像が始まることがあるので、
            # 差し込み口が画面を求めるときだけ撮って渡す（判定には使わない）
            if ctx.hooks.wants_frames():
                frame = capture_frame_with_fallback(ctx.trigger_exe)
                if frame is not None:
                    ctx.hooks.on_frame(frame, "WAITING_INITIAL_ALPHA")
            time.sleep(LOADING_POLL_INTERVAL_SEC)
            continue

        frame = capture_frame_with_fallback(ctx.trigger_exe)
        if frame is None:
            time.sleep(LOADING_POLL_INTERVAL_SEC)
            continue
        aspect_probe.observe(frame, "loading")

        result = detector.step(frame)

        # 再度の暗転（ALPHA の紹介の映像の直前）の画面を、差し込み口にも渡す（判定には使わない）
        if result.state == SequenceState.COMPLETE and result.just_transitioned and ctx.hooks.wants_frames():
            ctx.hooks.on_frame(frame, None)

        # 暗転移行以降に達した場合はロードが開始されたと記録
        if result.state != SequenceState.WAITING_FIRST_DARK:
            loading_appeared = True

        if result.state == SequenceState.COMPLETE:
            if watcher is not None:
                watcher.poll()
                if watcher.watching_loading_screen():
                    log("[ロード監視] 本体はシーケンス完了と判定しましたが、ロード画面の監視プロセスはロード画面を表示中と見ています。"
                        "ロード画面の終わりを待ちます。")
                    main_complete_at = time.monotonic()
                    continue
            log("[ロード監視] シーケンス完了（COMPLETE）を検出しました。ScoreDetector を起動します。")
            return LoadingOutcome.COMPLETE

        # 状態遷移時とデバッグ用の定期ログ
        if result.just_transitioned:
            log(f"[ロード監視] 状態遷移: {result.state.name} "
                f"(輝度={result.brightness:.1f}, "
                f"ストリーク={result.loading_streak})")
        elif now - last_state_log >= 30.0:
            log(f"[ロード監視] 待機中... 状態={result.state.name}, "
                f"輝度={result.brightness:.1f}, "
                f"ストリーク={result.loading_streak}")
            last_state_log = now

        time.sleep(LOADING_POLL_INTERVAL_SEC)
