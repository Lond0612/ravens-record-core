"""
いまいる対戦の種類（on_screen_mode。自分用オーバーレイの切り替え・隠すのに使う）の決め方のテスト
ゲームを使わない。Monitor の _update_screen_mode に、見出しの読み取りの結果を順に渡す。
実行: python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ravens_record_core.config import Config  # noqa: E402
from ravens_record_core.hooks import Hooks  # noqa: E402
from ravens_record_core.monitor import base as monitor_base  # noqa: E402
from ravens_record_core.monitor import loop as monitor_loop  # noqa: E402


class Recorder(Hooks):
    def __init__(self) -> None:
        self.modes = []

    def on_screen_mode(self, mode):
        self.modes.append(mode)


class ScreenModeTests(unittest.TestCase):
    def setUp(self):
        self.hooks = Recorder()
        ctx = monitor_base.MonitorContext(Config(trigger_exe="nonexistent_rrc_test.exe"), hooks=self.hooks)
        self.monitor = monitor_loop.Monitor(ctx)

    def feed(self, now, header, idle=True):
        self.monitor._update_screen_mode(now, header, idle)

    def test_lobbies_switch_mode(self):
        self.feed(0, "SINGLE")
        self.feed(5, "TEAM")
        self.feed(10, "CUSTOM")
        self.assertEqual(self.hooks.modes, ["single", "team", None])

    def test_short_gap_keeps_mode_long_gap_hides(self):
        self.feed(0, "SINGLE")
        self.feed(10, None)   # マッチング中・チャット中など、少しの間読めない
        self.assertEqual(self.hooks.modes, ["single"])
        self.feed(10 + monitor_loop.SCREEN_MODE_LOST_SEC, None)  # メニューなどへ移った
        self.assertEqual(self.hooks.modes, ["single", None])

    def test_match_in_progress_keeps_mode(self):
        self.feed(0, "SINGLE")
        self.feed(100, None, idle=False)  # 試合の途中は、見出しが無くても変えない
        self.assertEqual(self.hooks.modes, ["single"])

    def test_same_mode_is_reported_once(self):
        for t in range(5):
            self.feed(t, "TEAM")
        self.assertEqual(self.hooks.modes, ["team"])


if __name__ == "__main__":
    unittest.main()
