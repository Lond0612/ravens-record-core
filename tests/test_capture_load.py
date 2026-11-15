"""撮影の負荷を減らす仕組み（高いリフレッシュレートでのカクつき対策。v1.5.5）"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravens_record_core.detectors import score_detector  # noqa: E402
from ravens_record_core.system import screencap


class FreshTrackerTests(unittest.TestCase):
    def test_fresh_while_changing_then_stale(self):
        tracker = score_detector.FreshTracker()
        a = np.zeros((64, 64, 3), np.uint8)
        b = np.full((64, 64, 3), 200, np.uint8)
        self.assertTrue(tracker.update(a, 0.0))
        self.assertTrue(tracker.update(b, 0.2))
        self.assertTrue(tracker.update(b, 0.2 + score_detector.DXGI_FRESH_SEC - 0.01))
        self.assertFalse(tracker.update(b, 0.2 + score_detector.DXGI_FRESH_SEC + 0.01))  # 止まっている → mss でも撮る
        self.assertTrue(tracker.update(a, 2.0))


class IdleCloseTests(unittest.TestCase):
    def test_dxgi_closed_after_idle(self):
        source = screencap.FrameSource()
        fake = mock.Mock(reopened=0)
        source._dxgi = fake
        source.last_dxgi_use = time.monotonic() - screencap.DXGI_IDLE_CLOSE_SEC - 1
        with mock.patch.object(screencap, "_shared_source", source), \
                mock.patch.object(screencap.time, "sleep", side_effect=[None, StopIteration]):
            with self.assertRaises(StopIteration):
                screencap._close_idle_dxgi()
        fake.close.assert_called_once()
        self.assertIsNone(source._dxgi)

    def test_recently_used_dxgi_kept(self):
        source = screencap.FrameSource()
        fake = mock.Mock(reopened=0)
        source._dxgi = fake
        source.last_dxgi_use = time.monotonic()
        with mock.patch.object(screencap, "_shared_source", source), \
                mock.patch.object(screencap.time, "sleep", side_effect=[None, StopIteration]):
            with self.assertRaises(StopIteration):
                screencap._close_idle_dxgi()
        fake.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class InMatchModeOcrTests(unittest.TestCase):
    """試合中は、見出しの欄を Tesseract で読む回を減らす（v1.5.5）"""

    def setUp(self):
        from ravens_record_core.detectors import mode_detector
        self.md = mode_detector
        self.md._last_in_match_ocr = 0.0
        self.shape = mock.patch.object(mode_detector.ocr_shapes, "read", return_value=None)
        self.shape.start()
        self.ocr = mock.patch.object(mode_detector.pytesseract, "image_to_string", return_value="")
        self.calls = self.ocr.start()

    def tearDown(self):
        self.ocr.stop()
        self.shape.stop()

    @staticmethod
    def text_like():
        img = np.zeros((86, 480, 3), np.uint8)
        img[30:50, 20:200] = 255  # 暗い背景に白い文字（見出しのような割合）
        return img

    def test_blank_roi_never_ocr(self):
        self.assertIsNone(self.md.detect_mode(np.zeros((86, 480, 3), np.uint8)).mode)
        self.calls.assert_not_called()

    def test_bright_background_skipped_in_match_only(self):
        bright = np.full((86, 480, 3), 255, np.uint8)
        self.md.detect_mode(bright, in_match=True)
        self.calls.assert_not_called()
        self.md.detect_mode(bright)  # 試合中でなければ、今までどおり読む
        self.assertTrue(self.calls.called)

    def test_in_match_ocr_throttled(self):
        img = self.text_like()
        self.md.detect_mode(img, in_match=True)
        first = self.calls.call_count
        self.assertGreater(first, 0)
        self.md.detect_mode(img, in_match=True)  # 間を空けずに読もうとしても読まない
        self.assertEqual(self.calls.call_count, first)
        self.md.detect_mode(img)  # 試合中でなければ、毎回読む
        self.assertGreater(self.calls.call_count, first)


class LowLoadTests(unittest.TestCase):
    def test_apply_sets_single_thread_and_priority(self):
        import os

        from ravens_record_core.system import lowload
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch("psutil.Process") as proc, mock.patch("cv2.setNumThreads") as threads:
            os.environ.pop("OMP_THREAD_LIMIT", None)
            lowload.apply()
            self.assertEqual(os.environ["OMP_THREAD_LIMIT"], "1")
        threads.assert_called_once_with(1)
        proc.return_value.nice.assert_called_once()
