"""
ゲームのウィンドウを撮れなかったときに、モニター全体を撮って代わりにするのは、ゲームがいちばん手前にあるときだけのテスト
（ゲームが見つからない・裏にある・最小化中にモニターを撮ると、手前の別のアプリを読んでしまうため。2026-11）

ゲームの画面は使わない（ウィンドウの検出・撮影は差し替える）。
実行: python -m unittest tests.test_monitor_fallback
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import importlib  # noqa: E402

screencap = importlib.import_module("ravens_record_core.system.screencap")
ms = importlib.import_module("ravens_record_core.detectors.match_status_detector")
md = importlib.import_module("ravens_record_core.detectors.mode_detector")
ld = importlib.import_module("ravens_record_core.detectors.loading_sequence_detector")

HWND = 1234


class AllowedTest(unittest.TestCase):
    def check(self, hwnd, foreground, iconic=False):
        with mock.patch.object(screencap.win32gui, "GetForegroundWindow", return_value=foreground), \
             mock.patch.object(screencap.win32gui, "IsIconic", return_value=iconic):
            return screencap.monitor_fallback_allowed(hwnd)

    def test_only_when_game_is_foreground(self):
        self.assertTrue(self.check(HWND, HWND))
        self.assertFalse(self.check(HWND, 999))            # 裏にある（手前は別のアプリ）
        self.assertFalse(self.check(None, 999))            # ゲームが見つからない
        self.assertFalse(self.check(HWND, HWND, iconic=True))  # 最小化中


class DetectorsDoNotReadOtherAppsTest(unittest.TestCase):
    """ゲームが裏にあって撮れなかったとき、モニター全体を撮らない"""

    def setUp(self):
        self.patches = [
            mock.patch.object(screencap, "grab_client", return_value=None),
            mock.patch.object(screencap, "monitor_fallback_allowed", return_value=False),
            mock.patch.object(ms, "find_target_window", return_value=HWND),
            mock.patch.object(md, "find_target_window", return_value=HWND),
            mock.patch.object(ld, "find_target_window", return_value=HWND),
            mock.patch.object(ms.win32gui, "IsIconic", return_value=False),
            mock.patch.object(ms.win32gui, "GetWindowText", return_value="AC6"),
        ]
        for p in self.patches:
            p.start()
        self.mss = [mock.patch.object(m, "mss", side_effect=AssertionError("モニターを撮った")) for m in (ms, md, ld)]
        for p in self.mss:
            p.start()

    def tearDown(self):
        for p in self.patches + self.mss:
            p.stop()

    def test_status(self):
        before = ms._health["no_frame"]
        result = ms.detect_current_match_status()
        self.assertIsNone(result.status)
        self.assertEqual(ms._health["no_frame"], before + 1)

    def test_mode(self):
        self.assertIsNone(md.detect_current_screen_mode().mode)

    def test_loading(self):
        self.assertIsNone(ld.capture_frame_with_fallback())


if __name__ == "__main__":
    unittest.main()
