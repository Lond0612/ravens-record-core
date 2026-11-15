"""
AC6 のプロセス・ウィンドウの見つけ方（gamewindow.py）のテスト

ゲームを使わない。ウィンドウとプロセスの一覧を偽物に差し替えて、判定の規則を確かめる。
実行: python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ravens_record_core.system import gamewindow  # noqa: E402

EXE = "armoredcore6.exe"

# 偽のウィンドウ: hwnd → (pid, タイトル, 表示中か)
WINDOWS = {
    1: (100, "ARMORED CORE™ VI FIRES OF RUBICON™", True),   # ゲーム（プロセス名を取得できる）
    2: (200, "ARMORED CORE VI - YouTube - Google Chrome", True),  # ブラウザ（タイトルは似ているが、ゲームではない）
    3: (300, "ARMORED CORE™ VI FIRES OF RUBICON™", True),   # ゲーム（プロセス名を取得できない）
    4: (400, "", True),                                      # タイトルの無いゲームのウィンドウ（フルスクリーン）
}
# 偽のプロセス名: pid → 名前（None は取得できない）
NAMES = {100: "armoredcore6.exe", 200: "chrome.exe", 300: None, 400: "ArmoredCore6.exe"}


def patched(windows=WINDOWS, names=NAMES):
    def enum_windows(handler, extra):
        for hwnd in windows:
            handler(hwnd, extra)

    return [
        mock.patch.object(gamewindow.win32gui, "EnumWindows", side_effect=enum_windows),
        mock.patch.object(gamewindow.win32gui, "GetWindowText", side_effect=lambda h: windows[h][1]),
        mock.patch.object(gamewindow.win32gui, "IsWindowVisible", side_effect=lambda h: windows[h][2]),
        mock.patch.object(gamewindow, "_window_pid", side_effect=lambda h: windows[h][0]),
        mock.patch.object(gamewindow, "process_name", side_effect=lambda pid: names.get(pid)),
    ]


class GameWindowTests(unittest.TestCase):
    def setUp(self):
        self.patches = patched()
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_window_is_matched_by_process_name(self):
        self.assertTrue(gamewindow.is_game_window(1, EXE))
        self.assertTrue(gamewindow.is_game_window(4, EXE))   # 大文字・小文字は区別しない。タイトルが無くてもよい

    def test_title_is_used_only_when_the_process_name_is_unavailable(self):
        self.assertTrue(gamewindow.is_game_window(3, EXE))   # 名前を取得できない → タイトルで判断
        self.assertFalse(gamewindow.is_game_window(2, EXE))  # 名前を取得できる別のプログラムは、タイトルが似ていても違う

    def test_title_fallback_finds_only_unnamed_or_matching_processes(self):
        self.assertIn(gamewindow.find_game_window_by_title(EXE), (1, 3))

    def test_title_fallback_ignores_browsers(self):
        for p in self.patches:
            p.stop()
        self.patches = patched(windows={2: WINDOWS[2]})
        for p in self.patches:
            p.start()
        self.assertIsNone(gamewindow.find_game_window_by_title(EXE))

    def test_diagnose_lists_related_windows(self):
        with mock.patch.object(gamewindow.psutil, "process_iter", return_value=[]):
            text = gamewindow.diagnose(EXE)
        self.assertIn("chrome.exe", text)
        self.assertIn("プロセス=?", text)  # 名前を取得できないウィンドウも記録する


if __name__ == "__main__":
    unittest.main()
