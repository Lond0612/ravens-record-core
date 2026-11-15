"""画面の状態の見張り（ScreenStateWatcher）を、監視ループから使うための入口"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ravens_record_core.detectors.loading_sequence_detector import capture_frame_with_fallback
from ravens_record_core.detectors.match_status_detector import detect_current_match_status
from ravens_record_core.detectors.mode_detector import detect_current_screen_mode
from ravens_record_core.detectors.screen_state_watcher import ScreenStateWatcher

if TYPE_CHECKING:
    from ravens_record_core.monitor.base import MonitorContext


def screen_watcher(ctx: "MonitorContext") -> ScreenStateWatcher:
    """画面の状態の見張り（ScreenStateWatcher）。初めて使うときに作り、スレッドを始める（止めた状態で）。

    読み取りは、このモジュールの detect_current_screen_mode / detect_current_match_status を呼ぶ（テストで差し替えられるように）。
    """
    if ctx.screen is None:
        ctx.screen = ScreenStateWatcher(
            mode_fn=lambda: detect_current_screen_mode(ctx.trigger_exe),
            status_fn=lambda: _match_status_and_lobby(ctx),
        )
        ctx.screen.start()
    return ctx.screen


def _match_status_and_lobby(ctx: "MonitorContext"):
    """「マッチングしています / 対戦を開始します」の読み取り。マッチング中の画面なら、差し込み口（on_lobby）に知らせる
    （差し込み口で何が起きても、読み取りの結果は変えない）"""
    result = detect_current_match_status(ctx.trigger_exe)
    if getattr(result, "status", None) in ("MATCHING", "MATCH_START"):
        ctx.hooks.on_lobby(lambda: capture_frame_with_fallback(ctx.trigger_exe))
    return result


def stop_screen_watcher(ctx: "MonitorContext") -> None:
    watcher, ctx.screen = ctx.screen, None
    if watcher is not None:
        watcher.stop()
