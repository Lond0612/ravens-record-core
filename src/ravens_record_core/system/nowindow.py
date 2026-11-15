"""
Windows で、子プロセスにコンソールウィンドウを作らせない。

pytesseract は OCR のたびに tesseract.exe（コンソールアプリ）を子プロセスとして起動する。
pytesseract 自身は「ウィンドウを隠す」指定（STARTF_USESHOWWINDOW + SW_HIDE）しかしないため、
コンソールを持たない実行ファイル（PyInstaller の窓なし版など）から起動すると、新しいコンソールが作られる。Windows 11 で既定のターミナルが
「Windows ターミナル」の場合、この隠す指定が効かず、対戦中（カウントダウン中の OCR など）にターミナルが一瞬前面に出て、
ゲームの入力フォーカスを奪っていた。

install() を呼ぶと、以後このプロセスで起動するすべての子プロセスに CREATE_NO_WINDOW を付ける
（コンソール自体を作らない。標準入出力のパイプは、これまでどおり使える）。
新しいコンソールを明示的に求めた起動（CREATE_NEW_CONSOLE / DETACHED_PROCESS）には、手を加えない。
"""

import subprocess
import sys

_installed = False


def with_no_window(kwargs: dict) -> dict:
    """Popen に渡す引数に、CREATE_NO_WINDOW を足して返す（新しいコンソールを明示的に求めた起動には、手を加えない）。"""
    flags = kwargs.get("creationflags") or 0
    if flags & (subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS):
        return kwargs
    return {**kwargs, "creationflags": flags | subprocess.CREATE_NO_WINDOW}


def install() -> None:
    """subprocess.Popen に CREATE_NO_WINDOW を既定で付ける（Windows のみ。何度呼んでも 1 回だけ効く）。"""
    global _installed
    if _installed or sys.platform != "win32":
        return

    original_init = subprocess.Popen.__init__

    def _init_without_console(self, *args, **kwargs):
        original_init(self, *args, **with_no_window(kwargs))

    subprocess.Popen.__init__ = _init_without_console
    _installed = True
