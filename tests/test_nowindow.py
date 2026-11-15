"""
子プロセスにコンソールウィンドウを作らせない（nowindow.install）のテスト

ゲームを使わず、画面にウィンドウも出さない（子プロセスは、自分にコンソールがあるかを調べて終わるだけ）。
実行: python -m unittest discover -s tests -v
"""

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ravens_record_core.system import nowindow  # noqa: E402

# 子プロセス: 自分にコンソールウィンドウが付いていれば 1、付いていなければ 0 で終わる
_CHECK_CONSOLE = "import ctypes, sys; sys.exit(1 if ctypes.windll.kernel32.GetConsoleWindow() else 0)"


@unittest.skipUnless(sys.platform == "win32", "Windows のみ")
class NoWindowTests(unittest.TestCase):
    def test_child_process_has_no_console_window(self):
        nowindow.install()
        result = subprocess.run([sys.executable, "-c", _CHECK_CONSOLE], timeout=30)
        self.assertEqual(result.returncode, 0, "子プロセスにコンソールウィンドウが作られています")

    def test_existing_flags_are_kept(self):
        nowindow.install()
        # すでに指定されたフラグ（新しいプロセスグループ）も残したまま、コンソールは作られない
        result = subprocess.run(
            [sys.executable, "-c", _CHECK_CONSOLE],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0)

    def test_flags(self):
        flags = nowindow.with_no_window({})["creationflags"]
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        kept = nowindow.with_no_window({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP})["creationflags"]
        self.assertTrue(kept & subprocess.CREATE_NO_WINDOW and kept & subprocess.CREATE_NEW_PROCESS_GROUP)
        explicit = {"creationflags": subprocess.CREATE_NEW_CONSOLE}
        self.assertEqual(nowindow.with_no_window(explicit), explicit)

    def test_popen_receives_no_window_flag(self):
        nowindow.install()
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=30)
        # Popen は、渡された creationflags を保持していないため、pytesseract と同じ呼び方で、例外なく動くことを確かめる
        self.assertEqual(proc.returncode, 0)

    def test_install_is_idempotent(self):
        nowindow.install()
        patched = subprocess.Popen.__init__
        nowindow.install()
        self.assertIs(subprocess.Popen.__init__, patched)

    def test_pipes_still_work(self):
        nowindow.install()
        out = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
