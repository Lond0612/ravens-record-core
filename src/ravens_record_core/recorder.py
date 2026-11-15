"""
記録の入口（Recorder）

    recorder = Recorder(Config(data_dir=Path("my-logs")), MyHooks())
    recorder.run()               # ブロッキング（Ctrl+C か stop() まで）
    # または
    thread = recorder.run_in_thread()
    ...
    recorder.stop()

Recorder を作ると、設定が決まる（runtime.init: ログの形式と Tesseract を整える）。
同じプロセスで同時に動かせる Recorder は 1 つだけ（設定・画面の撮影はプロセスで 1 つ）。
"""

from __future__ import annotations

import threading
import time

from ravens_record_core import runtime
from ravens_record_core.config import Config
from ravens_record_core.hooks import Hooks
from ravens_record_core.monitor.base import MonitorContext, log
from ravens_record_core.monitor.loop import Monitor

# run_with_restart: 監視ループが予期しない例外で止まったら、少し待って始め直す。
# 同じ原因で止まり続けるときに繰り返さないよう、RESTART_WINDOW_SEC の間に RESTART_MAX 回までにする
RESTART_DELAY_SEC = 15.0
RESTART_MAX = 5
RESTART_WINDOW_SEC = 3600.0


class Recorder:
    def __init__(self, config: Config | None = None, hooks: Hooks | None = None):
        self.config = runtime.init(config)
        self.ctx = MonitorContext(self.config, hooks)
        self.restarts = 0  # run_with_restart で始め直した回数
        self._stop = threading.Event()
        self._monitor: Monitor | None = None

    @property
    def status(self) -> str | None:
        """いまの監視状態（status イベントの state）"""
        return self.ctx.current_status()

    def run(self) -> None:
        """監視ループを動かす（stop() か Ctrl+C で戻る。予期しない例外はそのまま投げる）"""
        self._stop.clear()
        self._monitor = Monitor(self.ctx)
        self._monitor.run(stop_requested=self._stop.is_set)

    def run_with_restart(self, sleep=time.sleep, clock=time.monotonic) -> None:
        """run と同じ。予期しない例外で止まったら、RESTART_DELAY_SEC 秒後に始め直す（止まり続けるなら、例外を投げる）"""
        restarts: list[float] = []
        while True:
            try:
                self.run()
                return
            except KeyboardInterrupt:
                raise
            except Exception:
                # 例外のログ・差し込み口（on_crash）・子プロセスの停止は、Monitor.run が済ませている
                now = clock()
                restarts = [t for t in restarts if now - t < RESTART_WINDOW_SEC]
                if len(restarts) >= RESTART_MAX:
                    log(f"[エラー] 監視が {RESTART_WINDOW_SEC / 60:.0f} 分の間に {RESTART_MAX} 回止まったため、監視を終了します。")
                    raise
                restarts.append(now)
                self.restarts += 1
                log(f"[復帰] 監視が止まったため、{RESTART_DELAY_SEC:.0f} 秒後に始め直します（{len(restarts)}/{RESTART_MAX} 回目）。")
                sleep(RESTART_DELAY_SEC)
                if self._stop.is_set():
                    return

    def run_in_thread(self, restart: bool = False) -> threading.Thread:
        """別のスレッドで監視ループを動かす（止めるときは stop()）"""
        thread = threading.Thread(target=self.run_with_restart if restart else self.run, daemon=True, name="ravens-record-core")
        thread.start()
        return thread

    def stop(self) -> None:
        """監視ループに止まるよう知らせ、子プロセスと画面の見張りを止める（別のスレッドから呼べる。何度呼んでもよい）"""
        self._stop.set()
        self.stop_children()

    def stop_children(self) -> None:
        """子プロセス（スコア・ロード画面の監視）と画面の見張りを、いますぐ止める（プロセスを終える前の後始末）"""
        monitor = self._monitor
        if monitor is not None:
            monitor.stop_everything()


def watch(config: Config | None = None, hooks: Hooks | None = None) -> None:
    """Recorder(config, hooks).run() と同じ"""
    Recorder(config, hooks).run()
