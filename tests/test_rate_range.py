"""
レートの読み誤りの除外（rate_detector.is_plausible_rate・rate_reader.read_rate_after）のテスト

ゲームを使わない。OCR の結果を差し替えて確かめる。
実行: python -m unittest discover -s tests -v
"""

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ravens_record_core.detectors import rate_detector  # noqa: E402

rate_reader = importlib.import_module("ravens_record_core.monitor.rate_reader")


FRAME = np.zeros((1080, 1920, 3), np.uint8)


def fake_read_voted(values):
    """複数の読み方の多数決（rate_detector.read_voted）を差し替える。読んだ値を順に返し、ありえない値は読めなかった扱い"""
    it = iter(values)

    def read(_image, valid=lambda v: True):
        value = next(it)
        return value if value is not None and valid(value) else None

    return read


class PlausibleRateTests(unittest.TestCase):
    def test_range(self):
        for rate in (1150, 1500, 2999):
            self.assertTrue(rate_detector.is_plausible_rate(rate), rate)
        for rate in (None, 7, 183, 300, 1000, 1074, 1149, 3000, 6600, 12000, 17418):
            self.assertFalse(rate_detector.is_plausible_rate(rate), rate)

    def test_out_of_range_rate_is_not_read(self):
        # 順位 12・レート 12000（読み誤り）→ レートは読めなかった扱い
        with mock.patch.object(rate_detector, "read_voted", side_effect=fake_read_voted([12, 12000])):
            result = rate_detector.detect_rate_and_rank(FRAME)
        self.assertTrue(result.is_s_rank)
        self.assertIsNone(result.rate)

    def test_stable_rate_needs_plausible_value(self):
        # 試合前: 順位とレートが続けて読めても、レートがありえない値なら採用しない
        with mock.patch.object(rate_detector, "read_voted", side_effect=fake_read_voted([5, 183] * 15)), \
                mock.patch.object(rate_detector.time, "sleep"):
            self.assertIsNone(rate_detector.capture_stable_rate(lambda: FRAME))


class FailureReasonTests(unittest.TestCase):
    """読めなかった理由（trace）。経路ごとの読みは差し替える（Tesseract を使わない）"""

    def _capture(self, values, frame=FRAME):
        trace = {}
        with mock.patch.object(rate_detector, "read_voted", side_effect=fake_read_voted(values)), \
                mock.patch.object(rate_detector, "_read_paths", return_value={"x": None}), \
                mock.patch.object(rate_detector.time, "sleep"):
            result = rate_detector.capture_stable_rate(lambda: frame, trace=trace)
        return result, trace

    def test_ok(self):
        result, trace = self._capture([5, 1500] * 3)
        self.assertEqual(result.rate, 1500)
        self.assertEqual(trace["reason"], "ok")
        self.assertNotIn("rate_paths", trace)

    def test_not_s_rank(self):
        result, trace = self._capture([None, None] * 3)   # PLACE 欄が読めない → S ランク以外とみなす
        self.assertFalse(result.is_s_rank)
        self.assertEqual((trace["reason"], trace["not_s_rank"]), ("not_s_rank", 3))
        self.assertIn("place_paths", trace)

    def test_read_error(self):
        result, trace = self._capture([5, 183] * 15)       # 順位は読めるが、レートがありえない値
        self.assertIsNone(result)
        self.assertEqual((trace["reason"], trace["read_error"]), ("read_error", 15))

    def test_unstable(self):
        values = []
        for i in range(15):
            values += [5, 1500 + i]                        # 毎回ちがう値
        result, trace = self._capture(values)
        self.assertIsNone(result)
        self.assertEqual(trace["reason"], "unstable")

    def test_no_frame(self):
        trace = {}
        with mock.patch.object(rate_detector.time, "sleep"):
            self.assertIsNone(rate_detector.capture_stable_rate(lambda: None, trace=trace))
        self.assertEqual((trace["reason"], trace["no_frame"]), ("no_frame", 15))


class RateAfterTests(unittest.TestCase):
    def _read(self, rates, rate_before=1500):
        results = iter(rates)

        def fake_stable(*args, **kwargs):
            rate = next(results)
            return None if rate is None else rate_detector.RateRankResult(is_s_rank=True, s_rank_position=10, rate=rate)

        with mock.patch.object(rate_reader, "capture_stable_rate", side_effect=fake_stable):
            return rate_reader.read_rate_after(rate_before, lambda: FRAME, sleep=lambda s: None)

    def test_normal_change_is_kept(self):
        self.assertEqual(self._read([1512]).rate, 1512)
        self.assertEqual(self._read([1350]).rate, 1350)
        self.assertEqual(self._read([1100], rate_before=1500).rate, 1100)  # ちょうど 400 下がった（範囲の確認は読み取りの側）

    def test_far_value_is_reread(self):
        self.assertEqual(self._read([1950, 1483]).rate, 1483)  # 読み直したら正しく読めた

    def test_far_value_after_rereads_is_dropped(self):
        self.assertIsNone(self._read([1950, 1950, 1950]))
        trace = {}
        with mock.patch.object(rate_reader, "capture_stable_rate",
                               return_value=rate_detector.RateRankResult(is_s_rank=True, s_rank_position=10, rate=1950)):
            rate_reader.read_rate_after(1500, lambda: FRAME, sleep=lambda s: None, trace=trace)
        self.assertEqual(trace["reason"], "too_far")

    def test_read_failure(self):
        self.assertIsNone(self._read([None]))

    def test_read_without_rate_before(self):
        # 対戦前が読めなくても、続けて同じ値が読めたら使う（比べる相手が無いので、離れすぎの確認はしない）
        self.assertEqual(self._read([1950], rate_before=None).rate, 1950)
        self.assertIsNone(self._read([None], rate_before=None))

    def test_not_s_rank_before_needs_place(self):
        # 対戦前に S ランク以外とみなしたときは、PLACE 欄も読めたときだけ使う
        calls = []

        def fake_stable(*args, **kwargs):
            calls.append(kwargs.get("ignore_s_rank"))
            return rate_detector.RateRankResult(is_s_rank=False, s_rank_position=None, rate=None)

        with mock.patch.object(rate_reader, "capture_stable_rate", side_effect=fake_stable):
            self.assertIsNone(rate_reader.read_rate_after(None, lambda: FRAME, sleep=lambda s: None, require_s_rank=True))
        self.assertEqual(calls, [False])


if __name__ == "__main__":
    unittest.main()
