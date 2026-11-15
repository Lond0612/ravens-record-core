"""監視の各段階で共通に使うもの（監視の文脈 MonitorContext・ログ・ゲームの起動の確認）"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any, Callable

import psutil

from ravens_record_core import logger
from ravens_record_core.config import Config
from ravens_record_core.hooks import Hooks, Status
from ravens_record_core.system import gamewindow

if TYPE_CHECKING:
    from ravens_record_core.detectors.screen_state_watcher import ScreenStateWatcher
    from ravens_record_core.monitor.children import LoadingWatcher

# 画面の見張り（ScreenStateWatcher）の結果を確かめる間隔（秒）。読み取りは見張りが約 1 秒ごとに行う
WATCH_POLL_INTERVAL_SEC = 0.3


def log(message: str, **fields) -> None:
    """ログを 1 行出す（logger.log と同じ。fields は、そのままイベントに付ける）"""
    logger.log(message, **fields)


def log_block(lines: list[str]) -> None:
    logger.log_block(lines)


class _GuardedHooks:
    """差し込み口の呼び出しで例外が出ても、監視を止めない（ログに残す）"""

    def __init__(self, hooks: Hooks):
        self._hooks = hooks

    @property
    def inner(self) -> Hooks:
        return self._hooks

    def __getattr__(self, name: str):
        fn = getattr(self._hooks, name)

        def call(*args: Any, **kwargs: Any):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                log(f"[警告] 差し込み口 {name} で例外が発生しました（監視は続けます）: {e!r}", level="warn")
                return _FALLBACK.get(name)

        return call


# 差し込み口が例外を投げたときの戻り値
_FALLBACK = {"is_ready": False, "wants_frames": False, "on_custom_match": True, "not_ready_status": None}


class MonitorContext:
    """監視の 1 回分の状態（設定・差し込み口・監視状態・子プロセス・画面の見張り）。

    監視の各段階は、これを受け取って使う（モジュールの変数に状態を持たない）。
    """

    def __init__(self, config: Config, hooks: Hooks | None = None):
        self.config = config
        self.hooks = _GuardedHooks(hooks or Hooks())
        self._status: tuple[str, str | None] | None = None
        self.screen: ScreenStateWatcher | None = None              # 画面の見張り（screen.screen_watcher で作る）
        self.loading_watcher: LoadingWatcher | None = None         # ロード画面の監視プロセス
        self.custom_detector: subprocess.Popen | None = None       # カスタムマッチのスコアの監視プロセス
        self.status_unreadable_reported = False                    # status_unreadable は、監視の 1 回につき 1 回だけ知らせる
        self.stop_requested: Callable[[], bool] = lambda: False    # 監視を止めるよう求められたか（Recorder.stop）

    # ---- 監視対象 ------------------------------------------------------------

    @property
    def trigger_exe(self) -> str:
        return self.config.trigger_exe

    def game_running(self) -> bool:
        return is_process_running(self.config.trigger_exe)

    # ---- 記録を進めてよいか ----------------------------------------------------

    def is_ready(self) -> bool:
        return bool(self.hooks.is_ready())

    # ---- 監視状態 ------------------------------------------------------------

    def current_status(self) -> str | None:
        """最後に知らせた監視状態（set_status の state）"""
        return self._status[0] if self._status else None

    def set_status(self, state: str, message: str, msg_key: str | None = None) -> None:
        """監視状態が変わったときだけ status イベントを出す。

        state: waiting_ready / waiting_ac6 / ac6_detected / matchmaking / loading / map_detection / in_match / post_match
               （is_ready が False の間は、Hooks.not_ready_status の state）
        msg_key: 画面に出すときの文言のキー（使う側のアプリが、画面の言語で表示するため。message はログ用の日本語）
        """
        if self._status == (state, msg_key):
            return
        self._status = (state, msg_key)
        logger.emit("status", message, state=state, msg_key=msg_key)

    def set_not_ready_status(self) -> None:
        status: Status | None = self.hooks.not_ready_status()
        if status is None:
            from ravens_record_core.hooks import WAITING_READY
            status = WAITING_READY
        self.set_status(status.state, status.message, status.msg_key)


_title_fallback_logged = False


def is_process_running(target_file: str) -> bool:
    """監視するゲーム（target_file）が実行中か。

    プロセス名で見つからないときは、プロセス名を取得できない「ARMORED CORE」のウィンドウがあれば、実行中とみなす
    （環境によって、ゲームのプロセス名を取得できないことがあるため。gamewindow を参照）。
    """
    target_name = os.path.basename(target_file).lower()

    try:
        for proc in psutil.process_iter(['name', 'exe']):
            try:
                pname = proc.info['name']
                if pname and pname.lower() == target_name:
                    return True
                pexe = proc.info['exe']
                if pexe and os.path.basename(pexe).lower() == target_name:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception as e:
        log(f"[エラー] プロセス一覧の取得中にエラーが発生しました: {e}")

    # 予備: プロセス名を取得できない「ARMORED CORE」のウィンドウ（テストで監視対象を差し替えているときは使わない）
    if target_name == "armoredcore6.exe" and gamewindow.find_game_window_by_title(target_name) is not None:
        global _title_fallback_logged
        if not _title_fallback_logged:
            log("[情報] ゲームのプロセス名を取得できないため、ウィンドウのタイトル（ARMORED CORE）で AC6 を検出しました")
            _title_fallback_logged = True
        return True
    return False
