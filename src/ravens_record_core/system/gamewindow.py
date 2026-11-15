"""
AC6 のプロセス・ウィンドウの見つけ方（各検出器・monitor パッケージで共通）

基本は、実行ファイル名（armoredcore6.exe）で探す。
ただし、環境によっては（セキュリティソフト・権限の違いなど）、ゲームのプロセス名を取得できないことがあり、
そのときは名前で探しても見つからない（2026-10: 英語圏のユーザーで、ゲーム中もずっと「AC6の起動を待機中」になった）。
そこで、プロセス名を取得できないウィンドウに限って、タイトルが「ARMORED CORE」で始まるものを、ゲームとみなす。
名前を取得できるウィンドウ（ブラウザで AC6 の動画を開いている、など）は、タイトルが一致してもゲームとはみなさない。

AC6 が見つからないときに、原因を調べるための診断の文（diagnose）も出す。
"""

from typing import Optional

import psutil
import win32gui
import win32process

# ゲームのウィンドウのタイトル（英語版・日本語版とも「ARMORED CORE™ VI FIRES OF RUBICON™」）
GAME_TITLE_PREFIX = "ARMORED CORE"


def process_name(pid: int) -> Optional[str]:
    """プロセス名。取得できない（権限がない・終了した）ときは None。"""
    try:
        return psutil.Process(pid).name() or None
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return None


def _window_pid(hwnd) -> Optional[int]:
    try:
        return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        return None  # 閉じたばかりのウィンドウなどで失敗する（そのウィンドウは対象にしない）


def _is_game_title(title: str) -> bool:
    return (title or "").strip().upper().startswith(GAME_TITLE_PREFIX)


def is_game_window(hwnd, exe_name: str) -> bool:
    """このウィンドウが、ゲーム（exe_name）のものか。"""
    pid = _window_pid(hwnd)
    if pid is None:
        return False
    name = process_name(pid)
    if name is not None:
        return name.lower() == exe_name.lower()
    # プロセス名を取得できないときだけ、ウィンドウのタイトルで判断する
    return _is_game_title(win32gui.GetWindowText(hwnd))


def find_game_window_by_title(exe_name: str) -> Optional[int]:
    """プロセス名では見つからないときの予備: タイトルが「ARMORED CORE」で、プロセス名を取得できない表示中のウィンドウ。"""
    found: list[int] = []

    def handler(hwnd, _):
        if found or not win32gui.IsWindowVisible(hwnd):
            return
        if not _is_game_title(win32gui.GetWindowText(hwnd)):
            return
        pid = _window_pid(hwnd)
        if pid is None:
            return
        name = process_name(pid)
        if name is None or name.lower() == exe_name.lower():
            found.append(hwnd)

    try:
        win32gui.EnumWindows(handler, None)
    except Exception:
        return None  # ウィンドウを列挙できないときは、見つからなかった扱い
    return found[0] if found else None


def diagnose(exe_name: str) -> str:
    """AC6 が見つからないときの診断の文（ログ用）。関連しそうなプロセス・ウィンドウと、名前を取得できないプロセスの数。"""
    related_procs: list[str] = []
    unnamed = 0
    total = 0
    try:
        for proc in psutil.process_iter(["name"]):
            total += 1
            name = proc.info.get("name")
            if not name:
                unnamed += 1
                continue
            lowered = name.lower()
            if "armored" in lowered or "protected_game" in lowered or lowered == exe_name.lower():
                related_procs.append(f"{name}(pid={proc.pid})")
    except Exception as e:
        related_procs.append(f"一覧の取得に失敗: {e!r}")

    windows: list[str] = []

    def handler(hwnd, _):
        try:
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            return  # 題名を読めないウィンドウは、診断の一覧に載せない
        if "ARMORED" not in (title or "").upper():
            return
        pid = _window_pid(hwnd)
        name = process_name(pid) if pid is not None else None
        visible = "表示" if win32gui.IsWindowVisible(hwnd) else "非表示"
        windows.append(f"'{title}'(pid={pid}, プロセス={name or '?'}, {visible})")

    try:
        win32gui.EnumWindows(handler, None)
    except Exception as e:
        windows.append(f"ウィンドウの列挙に失敗: {e!r}")

    return (f"[診断] '{exe_name}' が見つかりません。関連するプロセス: {', '.join(related_procs) or 'なし'} / "
            f"名前を取得できないプロセス: {unnamed}/{total} 件 / "
            f"タイトルに ARMORED を含むウィンドウ: {', '.join(windows) or 'なし'}")
