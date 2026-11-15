"""
ロード監視・マップ判定の段階（LoadingSequenceDetector / PreMatchSequenceDetector）を、枚数ではなく秒数で確定することのテスト

ゲームを使わない。明るい・暗い画面と撮影の時刻を渡し、撮影の間隔（処理の速さ）が違っても、同じ秒数で段階が進むことを確かめる。
実行: python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ravens_record_core.detectors import loading_sequence_detector as lsd  # noqa: E402

DARK = np.zeros((9, 16, 3), np.uint8)
BRIGHT = np.full((9, 16, 3), 60, np.uint8)

# 実際の試合前の流れ（2026-10 の録画）: Alpha 8 秒 → 暗転 → Beta 8 秒 → 暗転 → カウントダウン
# 暗転は、遅い撮影でも必ず 1 回は撮れる長さにしてある（暗転の見逃しは、このテストの対象外）
PRE_MATCH = [(8.0, BRIGHT), (1.6, DARK), (8.0, BRIGHT), (1.6, DARK), (10.0, BRIGHT)]


def frames(timeline, interval):
    t, end_of = 0.0, []
    for duration, img in timeline:
        end_of.append((t + duration, img))
        t += duration
    now = 0.0
    while now < t:
        yield now, next(img for until, img in end_of if now < until)
        now += interval


class PreMatchSecondsTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(lsd, "_log")
        p.start()
        self.addCleanup(p.stop)

    def run_pre_match(self, interval):
        d = lsd.PreMatchSequenceDetector()
        changes = []
        for now, img in frames(PRE_MATCH, interval):
            r = d.step(img, now=now)
            if r.just_transitioned:
                changes.append((r.state.name, round(now, 2)))
            if r.state == lsd.PreMatchSequenceState.COMPLETE:
                break
        return changes, d

    def test_completes_at_any_capture_interval(self):
        # 以前の「10 枚」では、0.8 秒ごとだと 8 秒の画面で 10 枚がそろわず止まった
        for interval in (0.2, 0.25, 0.5, 0.8, 1.0):
            with self.subTest(interval=interval):
                changes, d = self.run_pre_match(interval)
                self.assertEqual([c for c, _ in changes],
                                 ["WAITING_DARK_1", "WAITING_INITIAL_BETA", "WAITING_DARK_2", "COLLECTING_COUNTDOWN", "COMPLETE"])
                alpha_at = changes[0][1]
                self.assertTrue(2.0 <= alpha_at <= 3.0, alpha_at)  # 明るい画面が 2 秒続いた時点
                self.assertGreaterEqual(len(d.collected_countdown_frames), 3)

    def test_view_is_not_confirmed_by_a_short_bright_flash(self):
        d = lsd.PreMatchSequenceDetector()
        for now, img in frames([(1.5, BRIGHT), (0.5, DARK), (1.5, BRIGHT)], 0.2):
            d.step(img, now=now)
        self.assertEqual(d.state, lsd.PreMatchSequenceState.WAITING_INITIAL_ALPHA)

    def test_timeout_uses_the_capture_time(self):
        d = lsd.PreMatchSequenceDetector(timeout_sec=5.0)
        d.step(DARK, now=100.0)
        self.assertEqual(d.step(DARK, now=105.0).state, lsd.PreMatchSequenceState.TIMEOUT)


GREY_DARK = np.full((9, 16, 3), 25, np.uint8)   # 明るさの設定を上げた環境の暗転（真っ黒にならない、一様な灰色）
TEXTURED_DARK = np.random.default_rng(0).integers(0, 50, (36, 64, 3)).astype(np.uint8)  # 暗いが模様のある場面（平均 25 ほど）
GREY_BLANK = np.full((9, 16, 3), 50, np.uint8)  # 暗転を撮り逃した代わりに来る、ふつうの画面


class PreMatchDarkTests(unittest.TestCase):
    """v1.4.1: 一様な暗い画面も暗転とみなす・暗転を見逃したときの時間での補い"""

    def setUp(self):
        p = mock.patch.object(lsd, "_log")
        p.start()
        self.addCleanup(p.stop)

    def run_timeline(self, timeline, interval=0.2):
        d = lsd.PreMatchSequenceDetector()
        states = []
        for now, img in frames(timeline, interval):
            r = d.step(img, now=now)
            if r.just_transitioned:
                states.append(r.state.name)
            if r.state in (lsd.PreMatchSequenceState.COMPLETE, lsd.PreMatchSequenceState.TIMEOUT):
                break
        return states, d

    def test_uniform_grey_is_dark(self):
        d = lsd.PreMatchSequenceDetector()
        self.assertEqual(d.is_dark(GREY_DARK, 25.0), (True, True))
        self.assertEqual(d.is_dark(TEXTURED_DARK, float(TEXTURED_DARK.mean())), (False, False))
        self.assertEqual(d.is_dark(BRIGHT, 60.0), (False, False))  # 明るい一様な画面は、暗転ではない
        self.assertEqual(d.is_dark(DARK, 0.0), (True, False))

    def test_lifted_black_sequence_completes(self):
        timeline = [(8.0, BRIGHT), (1.0, GREY_DARK), (8.0, BRIGHT), (0.6, GREY_DARK), (10.0, BRIGHT)]
        states, d = self.run_timeline(timeline)
        self.assertEqual(states, ["WAITING_DARK_1", "WAITING_INITIAL_BETA", "WAITING_DARK_2", "COLLECTING_COUNTDOWN", "COMPLETE"])

    def test_missed_first_dark_falls_back(self):
        # 利用者のログの形: 1 回目の暗転が撮れず、Alpha のあとそのまま Beta。2 回目の暗転は撮れた
        timeline = [(8.0, BRIGHT), (1.0, GREY_BLANK), (8.0, BRIGHT), (1.0, DARK), (10.0, BRIGHT)]
        states, d = self.run_timeline(timeline)
        self.assertEqual(states, ["WAITING_DARK_1", "WAITING_DARK_2", "COLLECTING_COUNTDOWN", "COMPLETE"])
        self.assertGreaterEqual(len(d.collected_countdown_frames), 3)

    def test_gives_up_early_when_no_dark_comes(self):
        timeline = [(8.0, BRIGHT), (1.0, DARK), (8.0, BRIGHT), (60.0, BRIGHT)]
        states, d = self.run_timeline(timeline)
        self.assertEqual(states[-1], "TIMEOUT")
        # Beta の確定（約 11 秒）から 20 秒ほどで打ち切る（90 秒は待たない）
        self.assertLess(d._view_confirmed_at, 15.0)

    def test_hud_starts_countdown_when_the_dark_is_missed(self):
        # 利用者のログの形: 2 回目の暗転を撮れないまま、対戦の画面になった。上の見出し（陣営の色の縦棒）でカウントダウンに入る
        from ravens_record_core.detectors import match_end
        big = lambda v: np.full((1080, 1920, 3), v, np.uint8)
        hud = big(60)
        x0, y0, x1, y1 = match_end.SIDE_BAR_BOX
        hud[int(y0 * 1080) + 1:int(y1 * 1080) - 1, int(x0 * 1920) + 3:int(x0 * 1920) + 7] = (60, 230, 60)
        timeline = [(8.0, big(60)), (1.0, big(0)), (8.0, big(60)), (1.0, big(50)), (10.0, hud)]
        states, d = self.run_timeline(timeline)
        self.assertEqual(states, ["WAITING_DARK_1", "WAITING_INITIAL_BETA", "WAITING_DARK_2", "COLLECTING_COUNTDOWN", "COMPLETE"])
        self.assertGreaterEqual(len(d.collected_countdown_frames), 3)

    def test_waiting_for_dark(self):
        d = lsd.PreMatchSequenceDetector()
        self.assertFalse(d.waiting_for_dark)
        for now, img in frames([(3.0, BRIGHT)], 0.2):
            d.step(img, now=now)
        self.assertTrue(d.waiting_for_dark)


class LoadingSecondsTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(lsd, "_log")
        p.start()
        self.addCleanup(p.stop)
        self.tmp = Path(__file__).resolve().parent  # テンプレートは読み書きしない（auto_collect_templates=False）

    def test_loading_screen_is_confirmed_by_seconds(self):
        # 7 秒のロード画面。以前の「10 枚」では、1 秒ごとの撮影だと 7 枚しかそろわなかった
        timeline = [(1.0, BRIGHT), (1.0, DARK), (7.0, BRIGHT), (1.2, DARK), (5.0, BRIGHT)]
        for interval in (0.2, 0.5, 1.0):
            with self.subTest(interval=interval):
                d = lsd.LoadingSequenceDetector(template_dir=str(self.tmp / "no_templates"), auto_collect_templates=False)
                done_at = None
                for now, img in frames(timeline, interval):
                    if d.step(img, now=now).state == lsd.SequenceState.COMPLETE:
                        done_at = now
                        break
                self.assertIsNotNone(done_at)
                self.assertTrue(9.0 <= done_at <= 10.2, done_at)  # ロード画面のあとの暗転で完了


if __name__ == "__main__":
    unittest.main()
