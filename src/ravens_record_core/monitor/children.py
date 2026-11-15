"""子プロセス（スコアの監視 ScoreDetector・ロード画面の監視 LoadingScreenWatcher）の起動と停止

子プロセスは、次のどちらかで起動する（child_command）:
  - 1 つの実行ファイルにまとめたとき（PyInstaller など）: 自分自身を --score-detector / --loading-watcher 付きで起動する。
    まとめた側の入口は、その引数を受けたら ravens_record_core.cli.child_main を呼ぶこと
  - それ以外: python -m ravens_record_core --score-detector / --loading-watcher
設定（監視対象・保存先など）は、環境変数で渡す（Config.to_env。子プロセスは Config.from_env で受け取る）。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

from ravens_record_core import logger
from ravens_record_core.config import Config, is_frozen
from ravens_record_core.monitor.base import log

if TYPE_CHECKING:
    from ravens_record_core.monitor.base import MonitorContext

SCORE_DETECTOR_FLAG = "--score-detector"
LOADING_WATCHER_FLAG = "--loading-watcher"

# ロード完了のあと、監視プロセスを動かしておく最長の秒数（監視プロセスの側は INTRO_WAIT_SEC で自分で終わる）
LOADING_WATCHER_INTRO_GRACE_SEC = 7.0


def child_command(flag: str) -> list[str]:
    """子プロセスを起動するコマンド（flag は SCORE_DETECTOR_FLAG か LOADING_WATCHER_FLAG）"""
    if is_frozen():
        return [sys.executable, flag]
    return [sys.executable, "-m", "ravens_record_core", flag]


def child_env(config: Config, **extra: str) -> dict[str, str]:
    return dict(os.environ, **config.to_env(), **extra)


def stop_process(proc: subprocess.Popen | None, timeout: float = 5) -> None:
    """子プロセスが動いていれば止める（終わるまで待つ。終わらなければ強制終了）"""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_score_detector(config: Config, output_path: str | None = None) -> subprocess.Popen | None:
    """ScoreDetector を子プロセスとして起動する。

    標準出力を使わない構成（logger.set_stdout_enabled(False)）では、子プロセスの標準出力を親が読み取り、
    ログの出力先（logger.add_sink）へ流す。それ以外は、子プロセスの標準出力は親と同じ出力先に流れる。
    """
    cmd = child_command(SCORE_DETECTOR_FLAG)
    log(f"ScoreDetector を起動します（{' '.join(cmd[1:])}）")
    if output_path:
        cmd.extend(["--output", output_path])

    forward = logger.captures_child_output()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if forward else None,
            stderr=subprocess.DEVNULL if forward else None,
            env=child_env(config),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )
    except OSError as e:
        log(f"[エラー] ScoreDetector を起動できませんでした: {e!r}")
        return None
    if forward:
        threading.Thread(target=_forward_child_output, args=(proc,), daemon=True, name="rrc-score-output").start()
    return proc


def _forward_child_output(proc: subprocess.Popen) -> None:
    """ScoreDetector の標準出力（JSON Lines）を 1 行ずつ、ログの出力先へ流す。"""
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, b""):
        logger.publish_line(raw.decode("utf-8", errors="replace"))


class LoadingWatcher:
    """ロード画面の監視プロセス（loading_screen_watcher.py）の操作と、そこからの知らせの受け取り。

    「対戦を開始します」を検出した直後に起動し、本体の LoadingSequenceDetector と同時に動かす。
    知らせ（event = loading_watch）は poll() で読み、ほかの行はログへ流す。
    """

    def __init__(self, proc: subprocess.Popen, on_intro_start=None):
        self.proc = proc
        self._on_intro_start = on_intro_start
        self._events: "queue.Queue[dict]" = queue.Queue()
        self.loading_screen = False   # ロード画面を確認した
        self.loading_end = False      # ロード画面の終わりを確認した
        self.finished = False         # 監視プロセスが終わった（終わりの確認・時間切れ・異常終了）
        threading.Thread(target=self._read, daemon=True, name="rrc-loading-watcher").start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                obj = None
            if not isinstance(obj, dict) or "event" not in obj:
                logger.log(line)
                continue
            if obj["event"] == "loading_watch":
                self._events.put(obj)
                if obj.get("phase") == "intro_start" and self._on_intro_start is not None:
                    # ALPHA の紹介の映像が始まった時刻（暗転が明けた時刻）
                    try:
                        self._on_intro_start(float(obj["at"]))
                    except (KeyError, TypeError, ValueError):
                        pass
            fields = {k: v for k, v in obj.items() if k not in ("ts", "level", "event", "message")}
            if obj.get("message"):
                logger.emit(obj["event"], obj["message"], level=obj.get("level", "info"), **fields)
        self._events.put({"event": "loading_watch", "phase": "exited"})

    def poll(self) -> None:
        """届いた知らせを反映する（待たない）"""
        while True:
            try:
                obj = self._events.get_nowait()
            except queue.Empty:
                return
            phase = obj.get("phase")
            if phase == "loading_screen":
                self.loading_screen = True
            elif phase == "loading_end":
                self.loading_end = True
                self.finished = True
            elif phase in ("timeout", "exited"):
                self.finished = True

    def watching_loading_screen(self) -> bool:
        """監視プロセスがロード画面を表示中と見ているか（終わりはまだ）"""
        return self.loading_screen and not self.finished

    def stop_later(self, delay_sec: float) -> None:
        """少し待ってから止める（その間に自分で終われば、何もしない）"""
        timer = threading.Timer(delay_sec, self.stop)
        timer.daemon = True
        timer.start()

    def stop(self) -> None:
        stop_process(self.proc, timeout=3)


def start_loading_watcher(ctx: "MonitorContext") -> LoadingWatcher | None:
    """ロード画面の監視プロセスを起動する（起動に失敗しても、本体の判定だけで続ける）。"""
    stop_loading_watcher(ctx)
    cmd = child_command(LOADING_WATCHER_FLAG) + ["--parent-pid", str(os.getpid())]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_env(ctx.config, RRC_JSON_LOGS="1"),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        log(f"[ロード監視] [警告] ロード画面の監視プロセスを起動できませんでした（本体の判定だけで続けます）: {e!r}")
        return None
    ctx.loading_watcher = LoadingWatcher(proc, on_intro_start=ctx.hooks.on_intro_start)
    return ctx.loading_watcher


def stop_loading_watcher(ctx: "MonitorContext", grace_sec: float = 0.0) -> None:
    """監視プロセスを止める。grace_sec を渡すと、その秒数までは動かしておく
    （ロード画面の終わりのあと、ALPHA の紹介の映像が始まった時刻を知らせてから、自分で終わるため）"""
    watcher, ctx.loading_watcher = ctx.loading_watcher, None
    if watcher is None:
        return
    if grace_sec > 0:
        watcher.stop_later(grace_sec)
    else:
        watcher.stop()
