"""チーム戦の試合の流れ（team_timeline）: 読み取りの結果を差し替えて、出来事と集計のまとめ方を確かめる（画面は使わない）"""

import unittest
from unittest import mock

import numpy as np

from ravens_record_core.detectors import team_timeline
from ravens_record_core.detectors.team_timeline import TeamTimeline


class Script:
    """1 回の observe ごとに、読み取りの結果を順に返す"""

    def __init__(self, steps):
        self.steps = steps
        self.i = -1

    def next(self):
        self.i += 1
        return self.steps[min(self.i, len(self.steps) - 1)]


def run(steps, emblem_scores=None, own_side="ALPHA", finals=(None, None, None)):
    """steps: [{"timer": 残り秒, "own":, "opp":, "score":, "feed": [(種類, 陣営, 番号)], "reboot": bool}]（1 秒ごと）"""
    s = Script(steps)
    cur = {}
    hud = team_timeline.team_hud
    patches = [
        mock.patch.object(hud, "available", return_value=True),
        mock.patch.object(hud, "to_1080", side_effect=lambda f: (cur.clear(), cur.update(s.next()), f)[2]),
        mock.patch.object(hud, "read_timer", side_effect=lambda f: cur.get("timer")),
        mock.patch.object(hud, "read_field", side_effect=lambda f, k: cur.get(k)),
        mock.patch.object(hud, "read_feed", side_effect=lambda f: cur.get("feed", [])),
        mock.patch.object(hud, "is_rebooting", side_effect=lambda f: cur.get("reboot", False)),
        mock.patch.object(hud, "emblem_scores", return_value=emblem_scores or [0.0, 0.0, 0.0]),
        mock.patch.object(team_timeline.match_end, "read_own_side", return_value=own_side),
    ]
    for p in patches:
        p.start()
    try:
        tl = TeamTimeline(emblem=np.zeros((10, 10, 3), np.uint8) if emblem_scores else None)
        for i in range(len(steps)):
            tl.observe(1000.0 + i, np.zeros((4, 4, 3), np.uint8))
        return tl.finish(own_side, *finals)
    finally:
        for p in patches:
            p.stop()


def step(timer, own=0, opp=0, score=0, feed=(), reboot=False):
    return {"timer": timer, "own": own, "opp": opp, "score": score, "feed": list(feed), "reboot": reboot}


class TimelineTest(unittest.TestCase):
    def test_points_are_confirmed_by_two_reads_and_split_into_self_and_mates(self):
        steps = [step(180 - i) for i in range(5)]
        steps += [step(175, own=141, score=124), step(174, own=141, score=124), step(173, own=141, score=124)]
        steps += [step(172, own=41, score=124), step(171, own=141, score=124)]  # 一度だけの読み誤り（桁の欠け）は使わない
        out = run(steps)
        pts = [e for e in out["timeline"] if e[0] == "p"]
        self.assertEqual(pts, [["p", 5.0, 141, 0, 141, 124, 0]])  # 自チーム +141（自分 +124・味方 +17）

    def test_feed_rows_counted_once_while_visible(self):
        kill = ("destroyed", "BETA", 3)
        prio = ("priority", "ALPHA", 2)
        steps = [step(180 - i) for i in range(3)]
        steps += [step(177, feed=[kill]), step(176, feed=[]), step(175, feed=[kill, prio]), step(174, feed=[kill, prio])]
        steps += [step(173 - i) for i in range(8)]
        steps += [step(165, feed=[kill])]  # 5 秒より後に、また同じ機体が撃破された
        out = run(steps)
        self.assertEqual([e for e in out["timeline"] if e[0] in "dq"],
                         [["d", 3.0, "b", 3], ["q", 5.0, "a", 2], ["d", 15.0, "b", 3]])
        self.assertEqual((out["summary"]["team_kills"], out["summary"]["team_deaths"]), (2, 0))

    def test_own_slot_from_emblem(self):
        out = run([step(180 - i) for i in range(3)], emblem_scores=[0.1, 0.79, 0.2])
        self.assertEqual((out["summary"]["own_slot"], out["summary"]["own_slot_basis"]), (2, "emblem"))

    def test_same_emblem_falls_back_to_own_death(self):
        # 番号 1 と 3 が同じエンブレム: 自分が撃破された時刻の、自チームの「撃破」の番号で決める
        steps = [step(180 - i) for i in range(10)]
        steps += [step(170, reboot=True, feed=[("destroyed", "ALPHA", 3)]), step(169, reboot=True)]
        steps += [step(168 - i) for i in range(3)]
        out = run(steps, emblem_scores=[0.8, 0.1, 0.8])
        self.assertEqual((out["summary"]["own_slot"], out["summary"]["own_slot_basis"]), (3, "death"))
        self.assertEqual(out["summary"]["own_deaths"], 1)
        self.assertEqual(out["summary"]["team_deaths"], 1)
        self.assertIn(["r", 10.0], out["timeline"])
        self.assertIn(["u", 12.0], out["timeline"])

    def test_own_death_at_the_end_without_reboot_is_counted(self):
        # 試合の終わりぎわに撃破された（REBOOTING が出る前に試合が終わった）: 自分の番号の「撃破」でも数える
        steps = [step(180 - i) for i in range(10)]
        steps += [step(170, feed=[("destroyed", "ALPHA", 2)])] + [step(169 - i) for i in range(4)] + [step(165, reboot=True)]
        steps += [step(164 - i) for i in range(10)] + [step(154, feed=[("destroyed", "ALPHA", 2)])]
        out = run(steps, emblem_scores=[0.1, 0.8, 0.1])
        self.assertEqual(out["summary"]["own_deaths"], 2)

    def test_same_emblem_without_death_stays_unknown(self):
        out = run([step(180 - i) for i in range(3)], emblem_scores=[0.8, 0.1, 0.8])
        self.assertIsNone(out["summary"]["own_slot"])

    def test_priority_seconds_per_side_and_slot(self):
        steps = [step(180 - i) for i in range(10)]
        steps += [step(170, feed=[("priority", "BETA", 1)])] + [step(169 - i) for i in range(50)]
        steps += [step(119, feed=[("priority", "BETA", 2)])] + [step(118 - i) for i in range(3)]
        out = run(steps)
        self.assertEqual(out["summary"]["priority_sec"], {"b": {"1": 51, "2": 119}})  # 10〜61 秒目は 1 番、61 秒目から最後（180）までは 2 番

    def test_final_points_from_result_screen(self):
        out = run([step(180 - i, own=0) for i in range(3)], finals=(610, 847, 335))
        self.assertEqual([e for e in out["timeline"] if e[0] == "p"], [["p", 180.0, 610, 847, 610, 335, 847]])

    def test_nothing_before_hud(self):
        self.assertIsNone(run([{"timer": None}] * 3))


if __name__ == "__main__":
    unittest.main()
