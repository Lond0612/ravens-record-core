"""
対戦後のレートの読み直しと、読めなかったときの中身（rate_*_detail）のテスト

ゲームを使わない（読み取りは差し替える）。実行: python -m unittest tests.test_rate_retry
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ravens_record_core.config import Config  # noqa: E402
from ravens_record_core.detectors import rate_detector  # noqa: E402
from ravens_record_core.detectors.rate_detector import RateRankResult  # noqa: E402
from ravens_record_core.match_session import MatchSession  # noqa: E402
from ravens_record_core.monitor import post_match  # noqa: E402
from ravens_record_core.monitor.base import MonitorContext  # noqa: E402
from ravens_record_core.monitor.rate_reader import rate_detail  # noqa: E402

READ_ERROR = {"reason": "read_error", "frames": 15, "no_frame": 0, "not_s_rank": 0, "read_error": 15, "values": [],
              "rate_paths": {"template": None, "tesseract": None, "tesseract_binary": None},
              "place_paths": {"template": None, "tesseract": None, "tesseract_binary": None},
              "brightness": {"frame": 40.0, "rate": 20.0, "rate_min": 5, "rate_max": 60}}


def fake_reads(*outcomes):
    """read_rate_after の代わり。outcomes を順に返す（dict なら trace に書いて None、そうでなければその結果）"""
    calls = list(outcomes)

    def read(rate_before, capture, trace=None, require_s_rank=False):
        outcome = calls.pop(0)
        if isinstance(outcome, dict):
            trace.update(outcome)
            return None
        return outcome

    return read


class RateRetryTest(unittest.TestCase):
    def run_read(self, *outcomes):
        ctx = MonitorContext(Config(trigger_exe="nonexistent_rrc_test.exe"))
        session = MatchSession()
        session.rate_before = 1400
        with mock.patch.object(post_match, "read_rate_after", side_effect=fake_reads(*outcomes)) as read, \
                mock.patch.object(post_match, "capture_frame_with_fallback", return_value=None), \
                mock.patch.object(post_match.aspect_probe, "observe"), \
                mock.patch.object(post_match.time, "sleep"), mock.patch.object(post_match, "log"), \
                mock.patch.object(post_match, "log_rate_failure"):
            post_match._read_rate_after(ctx, session)
        return session, read.call_count

    def test_reads_again_when_nothing_was_readable(self):
        # 1 回目は 1 枚も読めず、2 回目で読めた: レートを使い、読み直したことだけを残す
        session, calls = self.run_read(READ_ERROR, RateRankResult(is_s_rank=True, s_rank_position=900, rate=1410))
        self.assertEqual(calls, 2)
        self.assertEqual(session.rate_after, 1410)
        self.assertIsNone(session.rate_after_note)
        self.assertEqual(session.rate_after_detail, {"reason": "ok", "first_reason": "read_error", "retried": True})

    def test_keeps_detail_when_retry_also_fails(self):
        session, calls = self.run_read(READ_ERROR, READ_ERROR)
        self.assertEqual(calls, 2)
        self.assertIsNone(session.rate_after)
        self.assertEqual(session.rate_after_note, "read_error")
        self.assertEqual(session.rate_after_detail["brightness"]["rate_max"], 60)
        self.assertEqual(session.rate_after_detail["first_reason"], "read_error")
        self.assertTrue(session.rate_after_detail["retried"])

    def test_does_not_retry_when_values_were_unstable_or_too_far(self):
        # 読めてはいた（unstable・too_far）: 読み直しても同じなので、1 回だけ
        for reason in ("unstable", "too_far"):
            session, calls = self.run_read({"reason": reason, "values": [1410, 1411]})
            self.assertEqual(calls, 1)
            self.assertEqual(session.rate_after_note, reason)
            self.assertNotIn("retried", session.rate_after_detail)

    def test_no_detail_when_read_first_time(self):
        session, calls = self.run_read(RateRankResult(is_s_rank=True, s_rank_position=900, rate=1410))
        self.assertEqual(calls, 1)
        self.assertIsNone(session.rate_after_detail)
        self.assertNotIn("rate_after_detail", {k for k, v in session.to_dict().items() if v is not None})


class RateDetailTest(unittest.TestCase):
    def test_keeps_only_known_keys(self):
        detail = rate_detail({**READ_ERROR, "extra_debug": "x"})
        self.assertNotIn("extra_debug", detail)
        self.assertEqual(detail["reason"], "read_error")
        self.assertIsNone(rate_detail({"reason": "ok"}))
        self.assertIsNone(rate_detail({}))

    def test_brightness_tells_black_and_white_frames(self):
        black = rate_detector.brightness(np.zeros((1080, 1920, 3), np.uint8))
        white = rate_detector.brightness(np.full((1080, 1920, 3), 240, np.uint8))
        self.assertEqual((black["frame"], black["rate_max"]), (0.0, 0))
        self.assertEqual((white["frame"], white["rate_min"]), (240.0, 240))

    def test_failure_reason_includes_brightness(self):
        frame = np.zeros((1080, 1920, 3), np.uint8)
        with mock.patch.object(rate_detector, "_read_paths", return_value={"template": None}):
            info = rate_detector.failure_reason(["READ_ERROR"], 0, frame, None)
        self.assertEqual(info["reason"], "read_error")
        self.assertEqual(info["brightness"]["frame"], 0.0)


if __name__ == "__main__":
    unittest.main()
