"""チームのカスタムマッチ（v2.2〜）: 制限時間の読み取りと、試合の長さに合わせた試合の流れ。観戦の記録"""

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


# ---- 観戦（v2.2〜） ----------------------------------------------------------

from datetime import datetime  # noqa: E402
from unittest import mock  # noqa: E402

from ravens_record_core.detectors import team_match  # noqa: E402
from ravens_record_core.hooks import CUSTOM_TEAM_SPECTATED  # noqa: E402
from ravens_record_core.monitor import custom_team  # noqa: E402


class FakeTimeline:
    def __init__(self, last_t, hud, timer_visible=True):
        self.started, self.last_t, self.timer_visible, self._hud = True, last_t, timer_visible, hud

    def observe(self, now, frame):
        pass

    def hud_totals(self):
        return dict(self._hud)

    def finish(self, own_side, own, opp, score):
        return {"timeline": [["p", 10.0, 109, 0, 109, 42, 0], ["r", 20.0], ["d", 21.0, "b", 2], ["u", 30.0]],
                "summary": {"team_kills": 1, "own_slot": 3, "priority_sec": {"a": {"3": 100}}, "targets": {"own": ["0"] * 3, "opp": ["1"] * 3}}}


class SpectatedTest(unittest.TestCase):
    def run_spectated(self, timeline, results):
        ctx = mock.Mock()
        ctx.stop_requested.return_value = False
        ctx.config.app_version = "2.2.0"
        settings = SimpleNamespace(format_raw="チーム", time_limit_raw="5分", swap_mode_raw="固定", map_raw="汚染市街",
                                   map_match="汚染市街 - B", read_confidence={"map": 0.9}, game_lang="ja")
        reads = iter(results)
        with mock.patch.object(custom_team, "is_process_running", return_value=True), \
                mock.patch.object(custom_team, "_capture", return_value=object()), \
                mock.patch.object(custom_team.team_match, "read_team_result", side_effect=lambda f, with_map=True: next(reads, None)), \
                mock.patch.object(custom_team, "_read_result_screen", side_effect=lambda c, f: f_result), \
                mock.patch.object(custom_team, "detect_current_screen_mode", return_value=SimpleNamespace(mode="CUSTOM")), \
                mock.patch.object(custom_team, "_wait_room_return", return_value=True), \
                mock.patch.object(custom_team.time, "sleep"), \
                mock.patch.object(custom_team.matchlog, "write_team_match", return_value=None), \
                mock.patch.object(custom_team.logger, "emit"):
            f_result = next((r for r in results if r is not None), None)
            payload = custom_team._record_spectated(ctx, settings, datetime.now().astimezone(), custom_team.time.monotonic(), timeline, 300)
        return payload, ctx

    def test_result_screen_gives_alpha_beta_points(self):
        result = team_match.TeamResult(points={"ALPHA": 1476, "BETA": 969}, map_raw="汚染市街- B", map_match="汚染市街 - B")
        payload, ctx = self.run_spectated(FakeTimeline(120, {"own": 1000, "opp": 900}), [None, None, result])
        self.assertEqual((payload["alpha_points"], payload["beta_points"]), (1476, 969))
        self.assertEqual(payload["map_hint"]["match"], "汚染市街 - B")
        ctx.hooks.on_custom_match.assert_called_once()
        self.assertEqual(ctx.hooks.on_custom_match.call_args.args[0], CUSTOM_TEAM_SPECTATED)
        # 観戦では意味の無いもの（自分の SCORE の増え・REBOOTING・自分の番号）は除く
        self.assertEqual(payload["timeline"], [["p", 10.0, 109, 0, 109, None, 0], ["d", 21.0, "b", 2]])
        self.assertEqual(set(payload["summary"]), {"priority_sec", "targets"})

    def test_missed_result_screen_uses_hud_only_when_watched_to_the_end(self):
        payload, _ = self.run_spectated(FakeTimeline(299, {"own": 1476, "opp": 969}), [None] * 5)
        self.assertEqual((payload["alpha_points"], payload["beta_points"]), (1476, 969))
        self.assertEqual(payload["map_hint"]["match"], "汚染市街 - B")  # ルームの設定から
        # 途中でルームへ戻った（残り時間が読めなくなり、ルームの画面）: 結果が分からないので記録しない
        payload, ctx = self.run_spectated(FakeTimeline(200, {"own": 800, "opp": 700}, timer_visible=False), [None] * 5)
        self.assertIsNone(payload)
        ctx.hooks.on_custom_match.assert_not_called()


class TeamCameraTest(unittest.TestCase):
    def test_camera_mark_on_slot_three_is_found_only_for_team(self):
        import numpy as np
        from ravens_record_core.detectors import custom_match as cm
        f = np.full((1080, 1920, 3), 80, np.uint8)
        x, y, _, _ = cm.CAMERA_ROIS[1]
        tx, ty = int(x * 1920) + 10, int((y + cm.TEAM_CAMERA_SLOT_PITCH[1] * 2) * 1080) + 8   # 右（BETA）の 3 番の欄
        t = cm._CAMERA_TEMPLATE
        f[ty:ty + t.shape[0], tx:tx + t.shape[1]] = t[:, :, None]
        self.assertTrue(cm.has_spectator_camera(f, team=True))
        self.assertFalse(cm.has_spectator_camera(f))   # シングルの観戦は 1 番の欄だけ


if __name__ == "__main__":
    unittest.main()
