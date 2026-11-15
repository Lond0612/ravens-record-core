"""
画面の縦横比の記録（aspect_probe）のテスト
実行: python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ravens_record_core.system import aspect_probe  # noqa: E402


def frame(w, h, value=80):
    return np.full((h, w, 3), value, np.uint8)


class AspectProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"RRC_DATA_DIR": self.tmp.name})
        self.env.start()
        aspect_probe._logged = False

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_size_and_kind(self):
        self.assertEqual(aspect_probe.size_text(frame(1920, 1080)), "1920x1080")
        self.assertFalse(aspect_probe.is_unsupported(frame(1920, 1080)))
        self.assertFalse(aspect_probe.is_unsupported(frame(2560, 1440)))
        self.assertTrue(aspect_probe.is_unsupported(frame(3440, 1440)))   # 横長で、ゲームが横いっぱい

    def test_black_bars_are_supported(self):
        img = frame(3440, 1440, 0)                                          # 左右に黒い帯、中央が 16:9
        bar = (3440 - 2560) // 2
        img[:, bar:bar + 2560] = 80
        self.assertFalse(aspect_probe.is_unsupported(img))

    def test_samples_are_saved_once_per_scene(self):
        aspect_probe.observe(frame(1920, 1080), "loading")
        self.assertFalse(aspect_probe.samples_dir().exists())
        aspect_probe.observe(frame(3440, 1440), "loading")
        aspect_probe.observe(frame(3440, 1440, 90), "loading")
        aspect_probe.observe(frame(3440, 1440), "after_match")
        names = sorted(p.name for p in aspect_probe.samples_dir().glob("*.png"))
        self.assertEqual(names, ["after_match_3440x1440.png", "loading_3440x1440.png"])


if __name__ == "__main__":
    unittest.main()
