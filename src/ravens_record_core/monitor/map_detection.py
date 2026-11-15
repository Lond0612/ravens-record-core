"""ロードのあと、試合前の紹介の映像を追いながら、マップを判定する"""

from __future__ import annotations

import time

from ravens_record_core import logger
from ravens_record_core.detectors.loading_sequence_detector import capture_frame_with_fallback
from ravens_record_core.detectors.map_detector import MAP_DETECTION_POLL_INTERVAL_SEC, MapDetectionSession, PreMatchSequenceState
from ravens_record_core.hooks import ISSUE_MAP_TIMEOUT
from ravens_record_core.monitor.base import MonitorContext, is_process_running, log

# MapDetector のシーケンスタイムアウト（秒）
MAP_DETECTION_TIMEOUT_SEC = 90.0
# マップ判定で暗転を待つ間の、画面を見る間隔（ふだんは MAP_DETECTION_POLL_INTERVAL_SEC）
MAP_DETECTION_DARK_POLL_INTERVAL_SEC = 0.05


def wait_for_map_detection(ctx: MonitorContext) -> MapDetectionSession | None:
    """LoadingSequenceDetector.COMPLETE の後、試合前シーケンスを監視する。

    Alpha初期ビュー → 暗転 → Beta初期ビュー → 暗転 → カウントダウン → 暗転（試合開始）
    を検出し、カウントダウン区間のフレームでマップを自動判定する。

    Returns:
        MapDetectionSession : セッション（finalize済み）
        None                 : タイムアウト / AC6プロセス終了
    """
    log("[マップ判定] 試合前シーケンス検出を開始します（Alpha初期ビュー → Beta初期ビュー → カウントダウン）")

    session = MapDetectionSession(timeout_sec=MAP_DETECTION_TIMEOUT_SEC)
    last_state_log = 0.0

    while True:
        # AC6プロセス生存確認
        if not is_process_running(ctx.trigger_exe):
            log("[マップ判定] AC6プロセスが終了したため検出を中断します。")
            return None

        frame = capture_frame_with_fallback(ctx.trigger_exe)
        if frame is None:
            time.sleep(MAP_DETECTION_POLL_INTERVAL_SEC)
            continue

        result = session.step(frame)
        # 差し込み口へ: 既に撮った画面を渡すだけ（待たない・コピーしない。判定には影響しない）
        if ctx.hooks.wants_frames():
            ctx.hooks.on_frame(frame, result.state.name)

        if result.just_transitioned:
            log(f"[マップ判定] 状態遷移: {result.state.name} "
                f"(輝度={result.brightness:.1f}, "
                f"収集フレーム={result.countdown_frames_collected})")

        if result.state == PreMatchSequenceState.COMPLETE:
            map_result = session.finalize()
            logger.emit("map_detected", f"マップを判定しました: {map_result.auto_detected_map_id or '不明'}",
                        map=map_result.auto_detected_map_id, confidence=map_result.map_confidence,
                        msg_key="event.mapDetected" if map_result.auto_detected_map_id else "event.mapUnknown")
            return session

        if result.state == PreMatchSequenceState.TIMEOUT:
            log("[マップ判定] タイムアウト: 試合前シーケンスが完了しませんでした。判定をスキップします。")
            ctx.hooks.on_issue(ISSUE_MAP_TIMEOUT)
            return None

        now = time.monotonic()
        if now - last_state_log >= 30.0:
            log(f"[マップ判定] 待機中... 状態={result.state.name}, 輝度={result.brightness:.1f}")
            last_state_log = now

        # 暗転を待つ間は、こまめに見る（暗転は 0.4〜1 秒と短く、0.2 秒ごとでは撮れないことがある）
        time.sleep(MAP_DETECTION_DARK_POLL_INTERVAL_SEC if session.waiting_for_dark else MAP_DETECTION_POLL_INTERVAL_SEC)
