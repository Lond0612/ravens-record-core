"""ロビーで「マッチングしています / 対戦を開始します」を読めないときの知らせ（status_unreadable）: 誤報を減らす決まり（画面は使わない）"""

import unittest
from unittest import mock

import numpy as np

from ravens_record_core.detectors import match_status_detector as sd
from ravens_record_core.monitor import match_start


def unread(band: bool):
    with mock.patch.object(sd, "band_visible", return_value=band), mock.patch.object(sd, "keep_unread_strips", False):
        sd._note_read(np.zeros((4, 4, 3), np.uint8), sd.StatusResult(status=None, raw_text=""))


class StreakTest(unittest.TestCase):
    def setUp(self):
        sd.reset_band_streak()

    def test_streak_survives_short_gaps_and_resets_on_long_ones(self):
        for _ in range(5):
            unread(True)
        unread(False)
        unread(False)            # 2 回までの切れ目は続いているとみなす（帯の点滅）
        for _ in range(3):
            unread(True)
        self.assertEqual(sd.health_snapshot()["band_unread_streak_max"], 8)
        for _ in range(3):
            unread(False)        # 3 回切れたら数え直す
        unread(True)
        self.assertEqual(sd.health_snapshot()["band_unread_streak_max"], 8)

    def test_recognized_read_resets_streak(self):
        for _ in range(4):
            unread(True)
        sd._note_read(np.zeros((4, 4, 3), np.uint8), sd.StatusResult(status="MATCHING", raw_text="マッチング"))
        unread(True)
        self.assertEqual(sd._streak["n"], 1)


class ReportTest(unittest.TestCase):
    def setUp(self):
        sd.reset_band_streak()
        self.ctx = mock.Mock(status_unreadable_reported=False)
        self.watcher = mock.Mock(status_read_count=0, context_mode="SINGLE", last_mode=None)

    def test_scattered_band_reads_are_not_reported(self):
        for _ in range(70):      # 60 回以上読めなかったが、続いてはいない（ロビーのほかの表示の黄色）
            unread(True)
            for _ in range(3):
                unread(False)
        match_start._maybe_report_status_unreadable(self.ctx, self.watcher, 1000.0, 70)
        self.ctx.hooks.on_issue.assert_not_called()

    def test_sustained_unreadable_band_is_reported(self):
        for _ in range(70):
            unread(True)
        match_start._maybe_report_status_unreadable(self.ctx, self.watcher, 1000.0, 70)
        self.ctx.hooks.on_issue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
