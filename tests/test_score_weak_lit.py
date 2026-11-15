"""
スコアのマスの「弱い点灯」（明るい背景の前の点灯マス。ScoreDetector.judge_boxes / WeakLitTracker）のテスト

ゲームを使わない。各枠の (白さ, 差) を偽物に差し替え、0.2 秒ごとの観測として渡す。
実測（2026-10 の録画）: 明るい空の前の点灯マスは 白さ 210〜225 / 差 34〜53 で数秒以上続く。
背景の光の筋・爆発による誤った点灯は、最長 0.4 秒。
実行: python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ravens_record_core.detectors import score_detector as SD  # noqa: E402

OFF = (90.0, 10.0)      # 消灯
STRONG = (215.0, 120.0)  # 点灯（暗い背景）
WEAK = (214.0, 40.0)     # 点灯（明るい背景。差が小さい）
FLASH = (237.0, 33.0)    # 背景の光の筋（消灯のマスの後ろ。過去の試合の画像の値）


def run(seq_for_box2, use_weak=True, interval=0.2):
    """枠2（自分・内側）だけ seq の値、ほかは消灯。戻り値: 各時刻の current_scores[1]"""
    cur, pend, stab, weak = [0] * 4, [0] * 4, [0] * 4, SD.WeakLitTracker()
    out = []
    for n, metric in enumerate(seq_for_box2):
        values = [OFF, metric, OFF, OFF]
        with mock.patch.object(SD, "measure_bar", side_effect=lambda f, w, h, i: values[i]):
            SD.judge_boxes(None, 1920, 1080, pend, stab, cur,
                           weak=weak if use_weak else None, now=n * interval if use_weak else None)
        out.append(cur[1])
    return out


class WeakLitTests(unittest.TestCase):
    def test_strong_lit_is_confirmed_after_three_frames_as_before(self):
        self.assertEqual(run([STRONG] * 4), [0, 0, 1, 1])

    def test_weak_lit_is_confirmed_when_it_lasts(self):
        states = run([OFF] * 2 + [WEAK] * 12)
        first = states.index(1)
        self.assertEqual(first, 2 + 6)  # 1.2 秒（0.2 秒ごとの 7 回目）で確定
        self.assertTrue(all(states[first:]))

    def test_weak_lit_is_ignored_without_the_tracker(self):
        # 弱い点灯は、続いた長さを見ないときは従来どおり消灯（check_image など）
        self.assertEqual(run([WEAK] * 12, use_weak=False), [0] * 12)

    def test_short_flashes_are_not_lit(self):
        # 背景の光の筋（0.4 秒）が何度か出ても、点灯とはみなさない
        seq = ([FLASH] * 2 + [OFF] * 3) * 5
        self.assertEqual(run(seq), [0] * len(seq))

    def test_mixed_strong_and_weak_frames_count_as_one_run(self):
        # 背景の明るさが変わり、強い点灯と弱い点灯が交互に出ても、続いていれば確定する
        seq = [STRONG, WEAK] * 6
        self.assertEqual(run(seq)[-1], 1)

    def test_slow_loop_needs_the_same_seconds(self):
        # 1 回の処理が遅い（0.5 秒ごと）ときも、1.2 秒続けば確定（3 回以上）
        states = run([WEAK] * 6, interval=0.5)
        self.assertEqual(states.index(1), 3)  # 0, 0.5, 1.0, 1.5 秒 → 1.5 秒の時点


if __name__ == "__main__":
    unittest.main()
