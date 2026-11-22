"""チーム戦（v1.8〜）: リザルト画面の読み取りの組み立てと、記録の流れ（画面は使わない）"""

import unittest
from datetime import datetime, timezone
from unittest import mock

from ravens_record_core.detectors import team_match
from ravens_record_core.detectors.team_match import TeamResult
from ravens_record_core.hooks import Hooks
from ravens_record_core.monitor import team


def result(own_side="ALPHA", outcomes=("lose", "win"), points=(610, 847), own_points=335, map_match="LOCステーション31"):
    r = TeamResult(outcomes=dict(zip(("ALPHA", "BETA"), outcomes)), points=dict(zip(("ALPHA", "BETA"), points)),
                   own_side=own_side, own_points=own_points)
    if map_match:
        r.map_raw, r.map_match, r.map_score, r.map_lang = map_match, map_match, 1.0, "ja"
    return r


class ParseTest(unittest.TestCase):
    def test_points(self):
        self.assertEqual(team_match.parse_points("610PT"), 610)
        self.assertEqual(team_match.parse_points("1,119PT"), 1119)
        self.assertEqual(team_match.parse_points("847 PT"), 847)
        self.assertIsNone(team_match.parse_points("PT"))
        self.assertIsNone(team_match.parse_points(""))
        self.assertIsNone(team_match.parse_points("123456PT"))  # 桁が多すぎる: 読み誤り


class TeamResultTest(unittest.TestCase):
    def test_result_from_own_side_outcome(self):
        self.assertEqual(result().result, "lose")
        self.assertEqual(result(own_side="BETA").result, "win")

    def test_result_from_points_when_outcome_unread(self):
        self.assertEqual(result(outcomes=(None, None), points=(900, 800)).result, "win")
        self.assertEqual(result(outcomes=(None, None), points=(700, 700)).result, "draw")
        self.assertIsNone(result(outcomes=(None, None), points=(700, None)).result)
        self.assertIsNone(result(own_side=None).result)

    def test_merge_fills_missing(self):
        a = result(outcomes=(None, "win"), points=(None, 847), own_points=None, map_match=None)
        a.own_side = None
        a.merge(result())
        self.assertEqual((a.outcomes, a.points, a.own_side, a.own_points, a.map_match),
                         ({"ALPHA": "lose", "BETA": "win"}, {"ALPHA": 610, "BETA": 847}, "ALPHA", 335, "LOCステーション31"))
        self.assertTrue(a.complete)


class PayloadTest(unittest.TestCase):
    at = datetime(2026, 11, 22, 12, 9, 58, tzinfo=timezone.utc)

    def payload(self, *args):
        return team_match.build_payload(self.at, self.at, *args, None, None, "1.8.0")

    def test_from_result_screen(self):
        p = self.payload(result(own_side="BETA"), None, "ALPHA")
        self.assertEqual((p["result"], p["result_basis"], p["own_side"], p["own_points"], p["opp_points"], p["own_score"]),
                         ("win", "result_screen", "BETA", 847, 610, 335))
        self.assertEqual(p["map_hint"], {"match": "LOCステーション31", "score": 1.0, "lang": "ja"})
        self.assertNotIn("match_id", p)  # 試合の ID は読まない・入れない
        self.assertNotIn("own_place", p)  # チームの中での順位は記録しない

    def test_banner_only(self):
        p = self.payload(None, "win", "BETA")
        self.assertEqual((p["result"], p["result_basis"], p["own_side"], p["own_points"], p["map_hint"]), ("win", "banner", "BETA", None, None))

    def test_nothing_read(self):
        self.assertIsNone(self.payload(None, None, None)["result"])


class Recorder(Hooks):
    def __init__(self):
        self.calls = []

    def on_team_match_started(self):
        self.calls.append("started")

    def on_team_match(self, payload, path):
        self.calls.append("saved")
        return True

    def on_team_match_ended(self, payload):
        self.calls.append(("ended", payload is not None))


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.hooks = Recorder()
        self.ctx = mock.Mock(hooks=self.hooks)
        self.ctx.config.app_version = "1.8.0"
        for p in (mock.patch.object(team, "start_loading_watcher"), mock.patch.object(team, "stop_loading_watcher"),
                  mock.patch.object(team.matchlog, "write_team_match", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def run_flow(self, ended, rate=(None, "not_s_rank")):
        with mock.patch.object(team, "_read_rate_before", return_value=rate), \
             mock.patch.object(team, "_watch_until_result", return_value=ended), \
             mock.patch.object(team, "_wait_for_team_lobby", return_value=True) as lobby, \
             mock.patch.object(team, "_read_rate_after", return_value=1810):
            return team.record_team_match(self.ctx), lobby

    def test_saved_with_result(self):
        payload, lobby = self.run_flow((result(), None, "ALPHA"))
        self.assertEqual(payload["result"], "lose")
        self.assertEqual(self.hooks.calls, ["started", "saved", ("ended", True)])
        lobby.assert_not_called()  # S ランクでなければ、対戦後のレートは読まない

    def test_s_rank_reads_rate_after(self):
        payload, lobby = self.run_flow((result(), None, "ALPHA"), rate=(1800, None))
        self.assertEqual((payload["rate_before"], payload["rate_after"]), (1800, 1810))

    def test_not_saved_when_match_did_not_finish(self):
        payload, _ = self.run_flow(None)
        self.assertIsNone(payload)
        self.assertEqual(self.hooks.calls, ["started", ("ended", False)])

    def test_ended_is_called_even_when_monitoring_fails(self):
        with mock.patch.object(team, "_record", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                team.record_team_match(self.ctx)
        self.assertEqual(self.hooks.calls, ["started", ("ended", False)])


if __name__ == "__main__":
    unittest.main()
