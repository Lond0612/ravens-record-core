"""
試合の終わりの読み取り（match_end.py）のテスト

ゲームを使わない。帯の表示・リザルト画面・自分の陣営の読み取りと、最終スコアの決め方を確かめる。
運営者の録画（1920x1080）での確認: 帯 4 種類（PHASE 1/2・YOU WIN/LOSE）をすべて読み、誤検出なし。
リザルト画面は 2 試合とも、自分の陣営と合わせて 2-1・0-2 を正しく読んだ（録画は個人のものなので、リポジトリには入れない）。
実行: python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ravens_record_core.detectors import match_end as M  # noqa: E402

CYAN = (255, 255, 130)    # BGR: 帯の「YOU WIN」の色
ORANGE = (90, 120, 245)   # BGR: 帯の「YOU LOSE」の色


def blank(h=1080, w=1920, value=40):
    return np.full((h, w, 3), value, np.uint8)


def with_banner(text, color, h=1080):
    """帯の位置に、ゲームと同じ色の文字を描いた画面"""
    w = h * 16 // 9
    frame = blank(h, w, 40)
    x0, y0, x1, y1 = M.BANNER_BOX
    cv2.rectangle(frame, (0, int(y0 * h) - 10), (w, int(y1 * h) + 10), (25, 25, 25), -1)
    scale = 2.2 * h / 1080
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, scale, max(2, int(4 * h / 1080)))
    org = ((w - tw) // 2, int((y0 + y1) / 2 * h + th / 2))
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_DUPLEX, scale, color, max(2, int(4 * h / 1080)), cv2.LINE_AA)
    return frame


def with_side_bar(color, h=1080):
    w = h * 16 // 9
    frame = blank(h, w, 120)
    x0, y0, x1, y1 = M.SIDE_BAR_BOX
    cv2.rectangle(frame, (int(x0 * w) + 2, int(y0 * h) + 1), (int(x0 * w) + 6, int(y1 * h) - 1), color, -1)
    return frame


class BannerTests(unittest.TestCase):
    def test_reads_drawn_banners(self):
        cases = [
            ("YOU WIN", CYAN, ("final", None, "win")),
            ("YOU LOSE", ORANGE, ("final", None, "lose")),
            ("PHASE 2: YOU WIN", CYAN, ("phase", 2, "win")),
            ("PHASE 1: YOU LOSE", ORANGE, ("phase", 1, "lose")),
        ]
        for text, color, expected in cases:
            self.assertEqual(M.read_banner(with_banner(text, color)), expected, text)

    def test_other_resolution(self):
        self.assertEqual(M.read_banner(with_banner("YOU WIN", CYAN, h=1440)), ("final", None, "win"))

    def test_no_banner_skips_reading(self):
        with mock.patch.object(M, "_ocr") as ocr:
            self.assertIsNone(M.read_banner(blank()))
            ocr.assert_not_called()  # 色の数え上げだけで終わる

    def test_parsing(self):
        frame = with_banner("YOU WIN", CYAN)
        for text, expected in [("PHASE 3: YOU WIN", ("phase", 3, "win")), ("YOU L0SE", ("final", None, "lose")),
                               ("PHASE YOU WIN", None), ("UNIT-ALPHA WINS", None), ("", None)]:
            with mock.patch.object(M, "_ocr", return_value=text):
                self.assertEqual(M.read_banner(frame), expected, text)


class SideTests(unittest.TestCase):
    def test_bar_color(self):
        self.assertEqual(M.read_own_side(with_side_bar((60, 230, 60))), "ALPHA")    # 緑
        self.assertEqual(M.read_own_side(with_side_bar((230, 80, 235))), "BETA")    # ピンク
        self.assertEqual(M.read_own_side(with_side_bar((60, 230, 60), h=1440)), "ALPHA")
        self.assertIsNone(M.read_own_side(blank()))
        # 空などの広い範囲が同じ色でも、見出しの縦棒とはみなさない（ピンクの空のマップの紹介の映像）
        self.assertIsNone(M.read_own_side(np.full((1080, 1920, 3), (230, 80, 235), np.uint8)))


class ResultScreenTests(unittest.TestCase):
    def _result_frame(self, alpha_color, beta_color):
        frame = blank(1080, 1920, 15)
        for side, color in (("ALPHA", alpha_color), ("BETA", beta_color)):
            x0, y0, x1, y1 = M.RESULT_ROWS[side]
            cx = int((x0 + x1) / 2 * 1920)
            cv2.putText(frame, "WIN", (cx - 40, int(y1 * 1080) - 10), cv2.FONT_HERSHEY_DUPLEX, 1.0, color, 2)
        return frame

    def test_rows(self):
        frame = self._result_frame((40, 40, 200), (170, 160, 40))   # ALPHA 赤（LOSE）、BETA 緑がかった水色（WIN）
        points = iter(["1PT", "2PT"])
        with mock.patch.object(M, "is_result_screen", return_value=True), \
                mock.patch.object(M, "_ocr", side_effect=lambda *a: next(points)):
            result = M.read_result_screen(frame)
        self.assertEqual(result, {"ALPHA": ("lose", 1), "BETA": ("win", 2)})
        self.assertEqual(M.final_score("BETA", result), (2, 1))
        self.assertEqual(M.final_score("ALPHA", result), (1, 2))
        self.assertIsNone(M.final_score(None, result))

    def test_not_result_screen(self):
        self.assertIsNone(M.read_result_screen(blank(value=200)))  # 明るい画面は、見出しを読みもしない


class EndWatcherTests(unittest.TestCase):
    def run_frames(self, readings, side="BETA"):
        """readings: 各回の (帯, リザルト画面) の読み取り"""
        watcher = M.EndWatcher()
        banners = iter([r[0] for r in readings])
        results = iter([r[1] for r in readings])
        with mock.patch.object(M, "read_own_side", return_value=side), \
                mock.patch.object(M, "read_banner", side_effect=lambda f: next(banners)), \
                mock.patch.object(M, "read_result_screen", side_effect=lambda f: next(results)):
            for i in range(len(readings)):
                watcher.observe(None, now=i * 1.0)
        return watcher

    def test_final_banner_completes_a_missed_last_point(self):
        # 利用者のログの形: マスは 1-0 のまま。試合の終わりの帯「YOU WIN」で 2-0
        w = self.run_frames([(None, None)] * 3 + [(("final", None, "win"), None)] * 2)
        self.assertEqual(w.decided_score(1, 0), (2, 0, "試合の終わりの帯"))

    def test_phase_banners_count(self):
        w = self.run_frames([(("phase", 1, "lose"), None)] * 2 + [(None, None)] + [(("final", None, "win"), None)] * 2)
        self.assertEqual(w.decided_score(0, 0), (2, 1, "試合の終わりの帯"))

    def test_loss(self):
        w = self.run_frames([(None, None)] * 3 + [(("final", None, "lose"), None)] * 2)
        self.assertEqual(w.decided_score(1, 1), (1, 2, "試合の終わりの帯"))

    def test_one_reading_is_not_enough(self):
        w = self.run_frames([(None, None)] * 3 + [(("final", None, "win"), None), (None, None)])
        self.assertIsNone(w.decided_score(1, 0))

    def test_result_screen_wins_over_banner(self):
        res = {"ALPHA": ("lose", 1), "BETA": ("win", 2)}
        w = self.run_frames([(None, None)] * 3 + [(("final", None, "win"), None)] * 2 + [(None, res)] * 2)
        self.assertEqual(w.decided_score(1, 0), (2, 1, "リザルト画面"))

    def test_result_screen_needs_own_side(self):
        res = {"ALPHA": ("lose", 0), "BETA": ("win", 2)}
        watcher = M.EndWatcher()
        with mock.patch.object(M, "read_own_side", return_value=None), \
                mock.patch.object(M, "read_banner", return_value=None), \
                mock.patch.object(M, "read_result_screen", return_value=res):
            for i in range(4):
                watcher.observe(None, now=i * 1.0)
        self.assertIsNotNone(watcher.result)
        self.assertIsNone(watcher.decided_score(0, 0))  # 自分の陣営が分からなければ、リザルト画面だけでは決めない

    def test_nothing(self):
        self.assertIsNone(self.run_frames([(None, None)] * 5).decided_score(1, 0))


if __name__ == "__main__":
    unittest.main()
