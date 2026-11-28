"""撮影方式の順番（画面からはみ出したウィンドウは、画面から切り出さない）"""
import unittest

from ravens_record_core.system import screencap
from ravens_record_core.system.screencap import WindowState

MON = (0, 0, 1920, 1080)


def state(client, foreground=True, mode="windowed"):
    return WindowState(1, client, 1, MON, foreground, False, mode)


class OffMonitorTest(unittest.TestCase):
    def test_inside(self):
        self.assertFalse(screencap.client_off_monitor(state(MON, mode="borderless")))
        self.assertFalse(screencap.client_off_monitor(state((100, 100, 1380, 820))))

    def test_off(self):
        # 1920x1080 のウィンドウ表示を 1920x1080 の画面に置くと、枠と題名の分だけ右下がはみ出す
        self.assertTrue(screencap.client_off_monitor(state((8, 31, 1928, 1111))))
        self.assertTrue(screencap.client_off_monitor(state((-10, 0, 1270, 720))))

    def test_order(self):
        cap = screencap.FrameSource.__new__(screencap.FrameSource)
        self.assertEqual(cap._order(state((100, 100, 1380, 820)))[0], "dxgi")
        self.assertEqual(cap._order(state((8, 31, 1928, 1111))), ["printwindow"])
        self.assertEqual(cap._order(state(MON, foreground=False)), ["printwindow"])


if __name__ == "__main__":
    unittest.main()
