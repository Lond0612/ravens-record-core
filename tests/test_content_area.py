"""黒い帯の内側（ゲームの絵の範囲）を求める: screencap.find_content_rect / ContentLocator / game_area（v1.7.2〜）"""

import unittest
from unittest import mock

import numpy as np

from ravens_record_core.system import screencap


def picture(w: int, h: int, seed: int = 0) -> np.ndarray:
    """ゲームの絵の代わり（端まで明るい画素がある、まだらな画像）"""
    rng = np.random.default_rng(seed)
    return rng.integers(40, 220, size=(h, w, 3), dtype=np.uint8)


def boxed(w: int, h: int, rect: tuple) -> np.ndarray:
    """w x h の真っ黒な画像の rect の範囲に、絵を置く"""
    x0, y0, x1, y1 = rect
    img = np.zeros((h, w, 3), np.uint8)
    img[y0:y1, x0:x1] = picture(x1 - x0, y1 - y0)
    return img


def near(a, b, px=4) -> bool:
    return a is not None and all(abs(x - y) <= px for x, y in zip(a, b))


class FindContentRectTest(unittest.TestCase):
    def test_bands_on_all_sides(self):
        # モニター 2560x1440・ゲーム 2560x1080 のボーダーレス: 中央に 1920x1080
        img = boxed(2560, 1440, (320, 180, 2240, 1260))
        self.assertTrue(near(screencap.find_content_rect(img), (320, 180, 2240, 1260)))

    def test_left_and_right_only(self):
        img = boxed(2560, 1080, (320, 0, 2240, 1080))
        self.assertTrue(near(screencap.find_content_rect(img), (320, 0, 2240, 1080)))

    def test_top_and_bottom_only(self):
        img = boxed(1920, 1200, (0, 60, 1920, 1140))
        self.assertTrue(near(screencap.find_content_rect(img), (0, 60, 1920, 1140)))

    def test_no_bands(self):
        self.assertEqual(screencap.find_content_rect(picture(1920, 1080)), (0, 0, 1920, 1080))

    def test_full_width_ultrawide_is_kept(self):
        # 横いっぱいに描いている 21:9（帯が黒くない）は、そのまま（中央の 16:9 は menu_area が切る）
        self.assertEqual(screencap.find_content_rect(picture(3440, 1440)), (0, 0, 3440, 1440))

    def test_black_frame_is_undecided(self):
        self.assertIsNone(screencap.find_content_rect(np.zeros((1440, 2560, 3), np.uint8)))

    def test_dark_scene_with_small_light_is_not_band(self):
        img = np.zeros((1080, 1920, 3), np.uint8)
        img[980:1000, 1500:1800] = 200  # ロード画面の進行バーのような明かりだけ
        self.assertIsNone(screencap.find_content_rect(img))

    def test_off_centre_picture_is_not_band(self):
        img = boxed(2560, 1440, (0, 100, 1920, 1180))  # 16:9 だが中央に無い
        self.assertIsNone(screencap.find_content_rect(img))

    def test_non_16_9_picture_is_not_band(self):
        img = boxed(2560, 1440, (640, 180, 1920, 1260))  # 中央だが 16:9 ではない
        self.assertIsNone(screencap.find_content_rect(img))


class ContentLocatorTest(unittest.TestCase):
    def setUp(self):
        self.t = 0.0
        self.loc = screencap.ContentLocator(clock=lambda: self.t)

    def feed(self, img, times: int, step: float = 0.5):
        out = None
        for _ in range(times):
            out = self.loc.rect(img)
            self.t += step
        return out

    def test_locks_after_repeated_detection_and_keeps_through_dark_frames(self):
        img = boxed(2560, 1440, (320, 180, 2240, 1260))
        self.assertIsNone(self.loc.rect(img))  # 1 回目ではまだ使わない
        locked = self.feed(img, 5)
        self.assertTrue(near(locked, (320, 180, 2240, 1260)))
        dark = np.zeros_like(img)
        for _ in range(20):
            self.assertEqual(self.loc.rect(dark), locked)  # 暗転では確定を保つ
            self.t += 0.5

    def test_unlocks_when_resolution_changes_in_same_window(self):
        self.feed(boxed(2560, 1440, (320, 180, 2240, 1260)), 6)
        self.assertEqual(self.feed(picture(2560, 1440), 40), (0, 0, 2560, 1440))

    def test_sizes_are_kept_separately(self):
        a = boxed(2560, 1440, (320, 180, 2240, 1260))
        b = picture(1920, 1080)
        for _ in range(6):
            self.loc.rect(a)
            self.loc.rect(b)
            self.t += 0.5
        self.assertTrue(near(self.loc.rect(a), (320, 180, 2240, 1260)))
        self.assertEqual(self.loc.rect(b), (0, 0, 1920, 1080))


class GameAreaTest(unittest.TestCase):
    def setUp(self):
        self.t = 0.0
        patcher = mock.patch.object(screencap, "_locator", screencap.ContentLocator(clock=lambda: self.t))
        patcher.start()
        self.addCleanup(patcher.stop)

    def settle(self, img):
        for _ in range(6):
            screencap.game_area(img)
            self.t += 0.5

    def test_bands_on_all_sides_are_trimmed_once_settled(self):
        img = boxed(2560, 1440, (320, 180, 2240, 1260))
        area, trimmed = screencap.game_area(img)
        self.assertFalse(trimmed)  # 比率は 16:9 なので、確定するまでは切らない
        self.settle(img)
        area, trimmed = screencap.game_area(img)
        self.assertTrue(trimmed)
        self.assertTrue(abs(area.shape[1] - 1920) <= 4 and abs(area.shape[0] - 1080) <= 4)

    def test_menu_area_uses_the_trimmed_picture(self):
        img = boxed(2560, 1440, (320, 180, 2240, 1260))
        self.settle(img)
        area, trimmed = screencap.menu_area(img)
        self.assertTrue(trimmed)
        self.assertTrue(abs(area.shape[1] - 1920) <= 4)

    def test_left_right_bands_are_trimmed_before_settled(self):
        # 比率が 16:9 でなければ、確定する前から比率で切る（これまでどおり）
        area, trimmed = screencap.game_area(boxed(2560, 1080, (320, 0, 2240, 1080)))
        self.assertTrue(trimmed)
        self.assertEqual(area.shape[:2], (1080, 1920))

    def test_plain_16_9_is_untouched(self):
        img = picture(1920, 1080)
        self.settle(img)
        area, trimmed = screencap.game_area(img)
        self.assertFalse(trimmed)
        self.assertIs(area, img)


class FrameBlackCheckTest(unittest.TestCase):
    def test_is_black_accepts_none(self):
        # 帯の判定用の関数が同じ名前で上書きしていて、None で例外になっていた
        self.assertTrue(screencap._is_black(None))
        self.assertTrue(screencap._is_black(np.zeros((90, 160, 3), np.uint8)))
        self.assertFalse(screencap._is_black(picture(160, 90)))


if __name__ == "__main__":
    unittest.main()
