"""
スコアを読めなかった試合の勝敗の推測と、試合が流れたとみなすことのテスト

  - レートが変わっていなければ DRAW（順位は、ほかの人の試合でも動くので手がかりにしない。2026-11 に、順位の
    302 → 303 から LOSE と記録していた）
  - レートを読めなかったときだけ、順位で推測する
  - スコアが無く、レートも変わっていない試合は、流れたとみなす（読み取りの問題にしない）

実行: python -m unittest tests.test_match_result
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import importlib  # noqa: E402

ms = importlib.import_module("ravens_record_core.match_session")
post_match = importlib.import_module("ravens_record_core.monitor.post_match")


def session(score=None, rate=(None, None), position=(None, None)):
    s = ms.MatchSession()
    if score is not None:
        s.score_my, s.score_enemy = score
    s.rate_before, s.rate_after = rate
    s.rate_position_before, s.rate_position_after = position
    return s


class ResultTest(unittest.TestCase):
    def test_score_decides(self):
        self.assertEqual(session(score=(2, 1), rate=(1500, 1490)).result, "WIN")
        self.assertEqual(session(score=(0, 2)).result, "LOSE")

    def test_rate_change_decides_without_score(self):
        self.assertEqual(session(rate=(1500, 1508)).result, "WIN")
        self.assertEqual(session(rate=(1500, 1494)).result, "LOSE")

    def test_unchanged_rate_is_draw_even_if_position_moved(self):
        self.assertEqual(session(rate=(1522, 1522), position=(302, 303)).result, "DRAW")
        self.assertEqual(session(rate=(1522, 1522), position=(303, 302)).result, "DRAW")

    def test_position_is_used_only_without_rate(self):
        self.assertEqual(session(rate=(None, None), position=(302, 300)).result, "WIN")
        self.assertEqual(session(rate=(1522, None), position=(300, 302)).result, "LOSE")
        self.assertEqual(session().result, "DRAW")


class AbortedTest(unittest.TestCase):
    def test_no_score_and_unchanged_rate_is_aborted(self):
        self.assertTrue(post_match.match_aborted(session(rate=(1522, 1522))))

    def test_not_aborted(self):
        self.assertFalse(post_match.match_aborted(session(score=(1, 2), rate=(1522, 1522))))  # スコアがある
        self.assertFalse(post_match.match_aborted(session(rate=(1522, 1516))))               # レートが動いた
        self.assertFalse(post_match.match_aborted(session(rate=(1522, None))))               # 対戦後を読めなかった
        self.assertFalse(post_match.match_aborted(session(rate=(None, None))))


if __name__ == "__main__":
    unittest.main()
