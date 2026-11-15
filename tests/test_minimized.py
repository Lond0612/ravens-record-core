"""
ゲームが最小化されている試合の扱い
  - 撮影: 最小化中は、モニター全体を撮らない（デスクトップを見て判定を誤らない）
  - ScoreDetector: 監視した時間と、最小化されていた時間を数えてファイルに書く
  - 本体: 大半が最小化なら「最小化のため読めなかった」とみなす
実行: python -m unittest tests.test_minimized
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import importlib  # noqa: E402

from ravens_record_core.detectors import loading_sequence_detector as lsd  # noqa: E402
from ravens_record_core.detectors import score_detector as sd  # noqa: E402

post_match = importlib.import_module("ravens_record_core.monitor.post_match")


class CaptureWhileMinimizedTest(unittest.TestCase):
    def test_minimized_window_is_not_replaced_by_monitor(self):
        with mock.patch.object(lsd, "find_target_window", return_value=1234), \
             mock.patch.object(lsd.win32gui, "IsIconic", return_value=True), \
             mock.patch.object(lsd, "mss") as monitor:
            self.assertIsNone(lsd.capture_frame_with_fallback())
            monitor.assert_not_called()


    def test_screenshot_error_is_treated_as_no_frame(self):
        # UAC の確認・ロック画面などで撮れないとき（BitBlt: アクセスが拒否されました）、例外で止まらない
        from mss.exception import ScreenShotError
        with mock.patch.object(lsd, "find_target_window", return_value=None), \
             mock.patch.object(lsd, "mss", side_effect=ScreenShotError("BitBlt: denied")):
            self.assertIsNone(lsd.capture_frame_with_fallback())

    def test_mode_and_status_while_minimized_or_denied(self):
        from mss.exception import ScreenShotError

        from ravens_record_core.detectors import match_status_detector as ms
        from ravens_record_core.detectors import mode_detector as md
        with mock.patch.object(md, "find_target_window", return_value=1234), \
             mock.patch.object(md.win32gui, "IsIconic", return_value=True), \
             mock.patch.object(md.win32gui, "GetWindowText", return_value="AC6"), \
             mock.patch.object(md, "mss") as monitor:
            self.assertIsNone(md.detect_current_screen_mode().mode)
            monitor.assert_not_called()
        with mock.patch.object(ms, "find_target_window", return_value=None), \
             mock.patch.object(ms, "mss", side_effect=ScreenShotError("BitBlt: denied")):
            self.assertIsNone(ms.detect_current_match_status().status)


class ScoreBasisTest(unittest.TestCase):
    def test_basis_is_saved(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            sd.save_match_log(0, 0, path, basis="result_screen")
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["basis"], "result_screen")


class WindowTimeTrackerTest(unittest.TestCase):
    def test_counts_minimized_time_and_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "w.json")
            tracker = sd.WindowTimeTracker(path)
            tracker.add(0.0, minimized=False)
            tracker.add(10.0, minimized=False)   # 10 秒（映っていた）
            tracker.add(40.0, minimized=True)    # 30 秒（最小化）
            self.assertAlmostEqual(tracker.watched_sec, 40.0)
            self.assertAlmostEqual(tracker.minimized_sec, 30.0)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"watched_sec": 40.0, "minimized_sec": 30.0})

    def test_long_gap_is_capped(self):
        tracker = sd.WindowTimeTracker(None)
        tracker.add(0.0, minimized=True)
        tracker.add(500.0, minimized=True)
        self.assertAlmostEqual(tracker.minimized_sec, 60.0)


class MostlyMinimizedTest(unittest.TestCase):
    def test_ratio(self):
        self.assertTrue(post_match._is_mostly_minimized({"watched_sec": 240, "minimized_sec": 235}))
        self.assertFalse(post_match._is_mostly_minimized({"watched_sec": 240, "minimized_sec": 30}))
        self.assertFalse(post_match._is_mostly_minimized({"watched_sec": 0, "minimized_sec": 0}))
        self.assertFalse(post_match._is_mostly_minimized(None))

    def test_read_and_remove(self):
        with tempfile.TemporaryDirectory() as d:
            score_path = os.path.join(d, "temp_score_x.json")
            self.assertIsNone(post_match._read_window_time(score_path))
            with open(sd.window_time_path(score_path), "w", encoding="utf-8") as f:
                json.dump({"watched_sec": 100, "minimized_sec": 90}, f)
            self.assertEqual(post_match._read_window_time(score_path)["minimized_sec"], 90)
            post_match._remove_window_time(score_path)
            self.assertFalse(os.path.exists(sd.window_time_path(score_path)))


if __name__ == "__main__":
    unittest.main()
