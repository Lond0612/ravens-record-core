"""
ロード画面の監視（別プロセス。python -m ravens_record_core --loading-watcher）

本体（monitor パッケージ）の LoadingSequenceDetector と同時に動かし、ロード画面の始まりと終わりを本体へ知らせる。
本体は、どちらか早いほうでマップ判定へ進む（main.wait_for_loading_complete）。

■ 本体の判定との違い
  本体は「暗転 → 明るい画面が 10 枚 → 再度の暗転」で判定する。本体のループは文字の読み取り（OCR）もするため
  1 秒に約 1 枚しか見られず、1 秒前後の暗転を見逃すと段階が 1 つずれる
  （ロード画面の次の、Alpha 機の紹介の画面を「ロード画面」と確定し、マップ判定が来ない暗転を待ち続ける。2026-10 の調査）。
  こちらは暗転を数えない。ロード画面に必ず出る右下の進行バー（loadingbar.py）を、1 秒に約 10 回見る。
    - ロード画面の始まり: 進行バーが LOADING_CONFIRM_SEC 秒続いた
    - ロード画面の終わり: 進行バーが消え、そのあと暗い画面を見た（または LOADING_END_FALLBACK_SEC 秒、見えないまま）
  終わりを知らせたら、このプロセスは終わる（マップ判定は本体が行う）。

■ 撮影
  フルスクリーン・ボーダーレス・ウィンドウのどれでも撮れるよう、方式を自動で選ぶ（screencap.py）。

■ 本体への知らせ方
  標準出力に JSON Lines（logger の JSON モード）。event = "loading_watch" の行が知らせ（phase: started / loading_screen /
  loading_end / timeout）。それ以外の行は通常のログで、本体がログへ流す。
"""

import argparse
import os
import sys
import time

import psutil  # noqa: E402

from ravens_record_core import logger, paths
from ravens_record_core.detectors.loadingbar import detect_loading_bar  # noqa: E402
from ravens_record_core.system import gamewindow, screencap  # noqa: E402

EVENT = "loading_watch"

# 1 秒あたりの撮影回数の目安（間隔）
SAMPLE_INTERVAL_SEC = 0.1
# 進行バーがこの秒数続いたら、ロード画面とみなす（途中の見落としは LOADING_GAP_SEC まで許す）。
# 低い解像度では、進行バーが約 1 秒ごとに見えたり見えなかったりすることがある（720p に縮小した録画で確認）ため、見落としは長めに許す。
# ロード画面以外で進行バーと誤判定した画面は、試した範囲では無い（loadingbar.py）
LOADING_CONFIRM_SEC = 1.0
LOADING_GAP_SEC = 1.5
# ロード画面の終わり: 進行バーが見えなくなってから、この回数以上撮り、その間に暗い画面を見た
LOADING_END_MIN_SAMPLES = 3
# 暗い画面を見られなくても、進行バーがこの秒数見えなければ、終わったとみなす
LOADING_END_FALLBACK_SEC = 3.0
# 暗い画面とみなす明るさ（画面全体の平均。LoadingSequenceDetector と同じ）
DARK_THRESHOLD = 15.0
# ロード画面の終わりのあと、明るくなった（ALPHA の紹介の映像が始まった）とみなす明るさと、待つ最長の秒数
INTRO_BRIGHT_THRESHOLD = 30.0
INTRO_WAIT_SEC = 5.0
# 起動からこの秒数で、ロード画面の終わりを見られなくてもやめる（本体の待ち時間 120 秒より長く）
DEFAULT_TIMEOUT_SEC = 180.0
# この秒数ごとに、本体（親プロセス）が生きているか確かめる
PARENT_CHECK_SEC = 2.0


def _log(message: str) -> None:
    logger.log(message)


def _notify(phase: str, message: str = "", **fields) -> None:
    logger.emit(EVENT, message, phase=phase, t=round(time.time(), 3), **fields)


def find_game_window(exe_name: str):
    """ゲームのウィンドウ（表示中で、いちばん大きいもの）"""
    import win32gui

    found = []

    def handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not gamewindow.is_game_window(hwnd, exe_name):
            return
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        area = max(0, r - l) * max(0, b - t)
        if area > 0:
            found.append((area, hwnd))

    try:
        win32gui.EnumWindows(handler, None)
    except Exception:
        return None  # ウィンドウを列挙できないときは、見つからなかった扱い
    return max(found)[1] if found else None


def _brightness(img) -> float:
    return float(img[::4, ::4].mean())


class LoadingTracker:
    """進行バーの有無と明るさから、ロード画面の始まり・終わりを決める（撮影と切り離した、時刻つきの判定）"""

    def __init__(self):
        self.phase = "waiting"       # waiting → loading → ended
        self.first_seen = None       # 進行バーが続いて見え始めた時刻
        self.last_seen = None        # 進行バーを最後に見た時刻
        self.hits = 0
        self.absent_samples = 0      # 進行バーが消えてから撮った回数
        self.dark_after = False      # 進行バーが消えてから、暗い画面を見たか
        self.loading_started_at = None

    def step(self, now: float, bar_found: bool, brightness: float) -> str | None:
        """1 回分の観測を渡す。状態が変わったら "loading_screen" / "loading_end" を返す"""
        if self.phase == "waiting":
            if bar_found:
                if self.last_seen is None or now - self.last_seen > LOADING_GAP_SEC:
                    self.first_seen, self.hits = now, 0
                self.last_seen = now
                self.hits += 1
                if now - self.first_seen >= LOADING_CONFIRM_SEC and self.hits >= 4:
                    self.phase = "loading"
                    self.loading_started_at = self.first_seen
                    return "loading_screen"
            return None

        if self.phase == "loading":
            if bar_found:
                self.last_seen = now
                self.absent_samples = 0
                self.dark_after = False
                return None
            self.absent_samples += 1
            if brightness < DARK_THRESHOLD:
                self.dark_after = True
            gone = now - self.last_seen
            if (self.absent_samples >= LOADING_END_MIN_SAMPLES and self.dark_after) or gone >= LOADING_END_FALLBACK_SEC:
                self.phase = "ended"
                return "loading_end"
        return None


class IntroStartTracker:
    """ロード画面の終わり（暗転の途中）のあと、暗転が明けた時刻を見つける。
    ALPHA の紹介の映像の始まりで、紹介の映像の収集が、区間の始まりに使う（本体が暗転を見逃して、マップ判定を
    始めるのが遅れたときも、ALPHA の映像を集められるように）。暗転を見ずに明るくなったときは、始まりとみなさない"""

    def __init__(self, started: float, dark_seen: bool):
        self.started = started
        self.dark_seen = dark_seen

    def step(self, now: float, brightness: float) -> str | None:
        """"intro_start"（明けた）/ "timeout"（待ちきれない）/ None"""
        if brightness < DARK_THRESHOLD:
            self.dark_seen = True
        elif self.dark_seen and brightness >= INTRO_BRIGHT_THRESHOLD:
            return "intro_start"
        if now - self.started >= INTRO_WAIT_SEC:
            return "timeout"
        return None


def run(timeout_sec: float, parent_pid: int | None) -> int:
    screencap.enable_dpi_awareness()
    exe_name = os.path.basename(paths.trigger_exe())
    source = screencap.FrameSource(log=_log)
    tracker = LoadingTracker()
    intro: IntroStartTracker | None = None   # ロード画面の終わりのあと、暗転が明けるのを待つ間

    started = time.monotonic()
    last_parent_check = started
    frames = 0
    best_ratio = 0.0
    min_brightness = 255.0
    no_window_logged = False
    _notify("started", f"[ロード画面監視] 開始しました（PID: {os.getpid()}、対象: {exe_name}）", pid=os.getpid())

    try:
        while True:
            loop_start = time.monotonic()
            elapsed = loop_start - started
            if elapsed >= timeout_sec:
                _notify("timeout", f"[ロード画面監視] {timeout_sec:.0f}秒以内にロード画面の終わりを確認できませんでした"
                                   f"（状態={tracker.phase}、撮影={frames}枚、方式={source.backend}、"
                                   f"進行バーの一致の最大={best_ratio:.2f}、最も暗い画面={min_brightness:.1f}、"
                                   f"DXGI の作り直し{source.dxgi_reopened}回）")
                return 1
            if parent_pid and loop_start - last_parent_check >= PARENT_CHECK_SEC:
                last_parent_check = loop_start
                if not psutil.pid_exists(parent_pid):
                    return 0

            hwnd = find_game_window(exe_name)
            img = source.grab(hwnd) if hwnd is not None else None
            if hwnd is None and elapsed >= 10.0 and not no_window_logged:
                _log("[ロード画面監視] [警告] ゲームのウィンドウが見つかりません")
                no_window_logged = True

            if img is not None:
                frames += 1
                now = time.monotonic()
                captured_at = time.time()
                if intro is not None:
                    change = intro.step(now, _brightness(img))
                    if change == "intro_start":
                        # 本体は、この時刻を紹介の映像の収集に渡す（ログには出さない）
                        _notify("intro_start", at=round(captured_at, 3))
                        return 0
                    if change == "timeout":
                        return 0
                    time.sleep(max(0.0, SAMPLE_INTERVAL_SEC - (time.monotonic() - loop_start)))
                    continue
                bar = detect_loading_bar(img)
                brightness = _brightness(img)
                best_ratio = max(best_ratio, bar.run_ratio)
                min_brightness = min(min_brightness, brightness)
                change = tracker.step(now, bar.found, brightness)
                fps = frames / max(now - started, 1e-6)
                st = source.last_state
                if change == "loading_screen":
                    _notify("loading_screen",
                            f"[ロード画面監視] ロード画面を確認しました（開始から{tracker.first_seen - started:.1f}秒、"
                            f"方式={source.backend}、画面モード={st.mode if st else '?'}、{img.shape[1]}x{img.shape[0]}、"
                            f"{fps:.1f}枚/秒）",
                            backend=source.backend, mode=st.mode if st else None, fps=round(fps, 1))
                elif change == "loading_end":
                    shown = tracker.last_seen - tracker.loading_started_at
                    _notify("loading_end",
                            f"[ロード画面監視] ロード画面の終わりを確認しました（表示{shown:.1f}秒、"
                            f"{'暗転あり' if tracker.dark_after else '暗転なし'}、方式={source.backend}、{fps:.1f}枚/秒、"
                            f"方式の切り替え{source.switches}回、DXGI の作り直し{source.dxgi_reopened}回）",
                            loading_seconds=round(shown, 1), backend=source.backend, fps=round(fps, 1))
                    # すぐには終わらず、暗転が明ける（ALPHA の紹介の映像が始まる）のを少しだけ待って知らせる
                    # 明けた瞬間を見たときだけ知らせる（いま暗転の途中でなければ、始まりの時刻が正確に分からないので知らせない）
                    intro = IntroStartTracker(now, brightness < DARK_THRESHOLD)

            time.sleep(max(0.0, SAMPLE_INTERVAL_SEC - (time.monotonic() - loop_start)))
    finally:
        source.close()


def main(argv: list[str] | None = None) -> None:
    from ravens_record_core.system import lowload
    lowload.apply()  # 子プロセスとして起動されたとき、自分にも負荷を下げる設定をする
    parser = argparse.ArgumentParser(prog="ravens_record_core --loading-watcher")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--parent-pid", type=int, default=None)
    args, _ = parser.parse_known_args(argv)
    logger.set_json_mode(True)  # 本体が 1 行ずつ読む
    try:
        code = run(args.timeout, args.parent_pid)
    except KeyboardInterrupt:
        code = 0
    except Exception as e:
        _log(f"[ロード画面監視] [エラー] 予期しない例外で終了します: {e!r}")
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
