"""チームのカスタムマッチ（v2.2〜）: 制限時間の読み取りと、試合の長さに合わせた試合の流れ"""

import unittest
from types import SimpleNamespace

from ravens_record_core.detectors.team_timeline import TeamTimeline
from ravens_record_core.monitor.custom_team import DEFAULT_TIME_LIMIT_SEC, time_limit_sec


class CustomTeamTest(unittest.TestCase):
    def test_time_limit_sec_reads_minutes(self):
        assert time_limit_sec(SimpleNamespace(time_limit_raw="5分")) == 300
        assert time_limit_sec(SimpleNamespace(time_limit_raw="10 min")) == 600
        assert time_limit_sec(SimpleNamespace(time_limit_raw="3분")) == 180


    def test_time_limit_sec_falls_back(self):
        assert time_limit_sec(SimpleNamespace(time_limit_raw="")) == DEFAULT_TIME_LIMIT_SEC
        assert time_limit_sec(SimpleNamespace(time_limit_raw="分")) == DEFAULT_TIME_LIMIT_SEC
        assert time_limit_sec(SimpleNamespace(time_limit_raw="99分")) == DEFAULT_TIME_LIMIT_SEC
        assert time_limit_sec(SimpleNamespace()) == DEFAULT_TIME_LIMIT_SEC


    def test_timeline_uses_match_length(self):
        tl = TeamTimeline(match_sec=300)
        tl.started = True
        tl.fields["own"].value = 0
        detail = tl.finish("ALPHA", 1500, 1200, 600)
        # 最後の合計を補う時刻は、試合の終わり（制限時間）
        points = [e for e in detail["timeline"] if e[0] == "p"]
        assert points and points[-1][1] == 300.0


if __name__ == "__main__":
    unittest.main()
