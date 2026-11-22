"""
ランクマッチ（SINGLE）以外の対戦（カスタムマッチ等）を記録しないことのテスト

ゲームを使わない。画面の判定（モード・マッチング状態）と時計を差し替えて、待機ループと画面の見張り（ScreenStateWatcher）の判断だけを確かめる。
実行: python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import importlib  # noqa: E402

match_start = importlib.import_module("ravens_record_core.monitor.match_start")
from ravens_record_core.config import Config  # noqa: E402
from ravens_record_core.detectors.mode_detector import MODE_ROI_PERCENT, _crop_roi_percent, detect_mode  # noqa: E402
from ravens_record_core.detectors.screen_state_watcher import ScreenStateWatcher  # noqa: E402
from ravens_record_core.monitor.base import MonitorContext  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def mode(m, raw=""):
    return SimpleNamespace(mode=m, raw_text=raw or (m or ""))


def status(s):
    return SimpleNamespace(status=s, raw_text=s or "")


class FakeClock:
    """time.monotonic / time.sleep の代わり。sleep した分だけ時計を進める。
    画面の見張り（ScreenStateWatcher）のスレッドの代わりに、1 秒たつごとに見張りの読み取りを 1 回行う"""

    def __init__(self):
        self.now = 1000.0
        self.watcher = None
        self._next_tick = self.now

    def monotonic(self):
        return self.now

    def sleep(self, sec):
        self.now += sec
        while self.watcher is not None and self.now >= self._next_tick:
            self.watcher.tick()
            self._next_tick += 1.0


class WaitForMatchStartTests(unittest.TestCase):
    def run_wait(self, modes, statuses, initial_context="SINGLE", running_for=None, lobby="SINGLE"):
        """modes / statuses: 見張りの読み取り 1 回（1 秒）ごとの値（足りなくなったら最後の値を使い続ける）。
        initial_context: 待機を始める前に読めていた見出し（呼び出し元は、SINGLE を読んでから待機に入る）"""
        clock = FakeClock()
        mode_iter, status_iter = list(modes), list(statuses)

        def next_mode():
            return mode_iter.pop(0) if len(mode_iter) > 1 else mode_iter[0]

        def next_status():
            return status_iter.pop(0) if len(status_iter) > 1 else status_iter[0]

        watcher = ScreenStateWatcher(mode_fn=next_mode, status_fn=next_status, clock=clock.monotonic)
        watcher.note_mode(initial_context)
        clock.watcher = watcher
        running = (lambda *_: True) if running_for is None else (lambda *_: clock.now - 1000.0 < running_for)
        ctx = MonitorContext(Config(trigger_exe="nonexistent_rrc_test.exe"))
        with mock.patch.object(match_start, "is_process_running", side_effect=running), \
             mock.patch.object(match_start.time, "monotonic", side_effect=clock.monotonic), \
             mock.patch.object(match_start.time, "sleep", side_effect=clock.sleep), \
             mock.patch.object(ctx, "set_status"), \
             mock.patch.object(match_start, "log"), \
             mock.patch.object(match_start.logger, "emit"):
            return match_start.wait_for_match_start(ctx, watcher, mode=lobby), clock

    def test_ranked_lobby_then_match_start_is_recorded(self):
        result, _ = self.run_wait(
            modes=[mode("SINGLE")] * 5,
            statuses=[status("MATCHING"), status("MATCHING"), status("MATCH_START")],
        )
        self.assertTrue(result)

    def test_long_unreadable_heading_does_not_stop_waiting(self):
        # マッチング（5 分、見出しが読めない） → キャンセル → チャット（3 分、見出しが読めない） → マッチング → 対戦開始。
        # 以前は 45 秒で待機をやめ、この試合は記録されなかった
        modes = [mode("SINGLE")] + [mode(None)] * 600
        statuses = [status(None)] + [status("MATCHING")] * 300 + [status(None)] * 180 + [status("MATCHING")] * 60 + [status("MATCH_START")]
        result, clock = self.run_wait(modes=modes, statuses=statuses)
        self.assertTrue(result)
        self.assertGreater(clock.now - 1000.0, 500)

    def test_moving_to_custom_match_stops_waiting(self):
        # カスタムマッチのルームの見出しを読んだら、待機をやめる（カスタムマッチの対戦は、ランクマッチとして記録しない）
        modes = [mode("SINGLE")] * 3 + [mode(None)] * 5 + [mode("CUSTOM")] + [mode(None)]
        statuses = [status(None)] * 12 + [status("MATCH_START")]
        result, _ = self.run_wait(modes=modes, statuses=statuses)
        self.assertFalse(result)

    def test_match_start_after_a_custom_heading_is_not_ranked(self):
        # 対戦開始と同じ回に、カスタムマッチの見出しを読んだ（最後に読めた見出しが CUSTOM）
        result, _ = self.run_wait(modes=[mode("CUSTOM")], statuses=[status("MATCH_START")])
        self.assertFalse(result)

    def test_brief_ocr_dropouts_in_ranked_lobby_are_tolerated(self):
        # ランクマッチのロビーで、たまにモードが読めなくても、待機は続き、開始を記録する
        modes = [mode("SINGLE"), mode(None), mode(None), mode("SINGLE"), mode(None), mode("SINGLE")]
        statuses = [status("MATCHING")] * 5 + [status("MATCH_START")]
        result, _ = self.run_wait(modes=modes, statuses=statuses)
        self.assertTrue(result)

    def test_team_mode_stops_waiting(self):
        result, _ = self.run_wait(modes=[mode("TEAM")], statuses=[status(None)])
        self.assertFalse(result)

    def test_team_lobby_then_match_start_is_recorded_as_team(self):
        # チーム戦（v1.8〜）: チーム戦のロビーから待つときは、TEAM の見出しのまま対戦開始を見つけたら記録する
        result, _ = self.run_wait(modes=[mode("TEAM")] * 5, statuses=[status("MATCHING"), status("MATCH_START")],
                                  initial_context="TEAM", lobby="TEAM")
        self.assertTrue(result)

    def test_team_lobby_stops_waiting_on_single_heading(self):
        result, _ = self.run_wait(modes=[mode("SINGLE")], statuses=[status(None)], initial_context="TEAM", lobby="TEAM")
        self.assertFalse(result)

    def test_game_exit_stops_waiting(self):
        result, clock = self.run_wait(modes=[mode(None)], statuses=[status(None)], running_for=30)
        self.assertFalse(result)
        self.assertLess(clock.now - 1000.0, 32)


class ScreenStateWatcherTests(unittest.TestCase):
    def make(self, modes, statuses):
        clock = FakeClock()
        mode_iter, status_iter = list(modes), list(statuses)
        w = ScreenStateWatcher(mode_fn=lambda: mode_iter.pop(0) if len(mode_iter) > 1 else mode_iter[0],
                               status_fn=lambda: status_iter.pop(0) if len(status_iter) > 1 else status_iter[0],
                               clock=clock.monotonic)
        return w, clock

    def run_ticks(self, w, clock, n):
        events = []
        for _ in range(n):
            w.tick()
            e = w.take_match_start()
            if e is not None:
                events.append((round(clock.now - 1000.0), e.context_mode))
            clock.now += 1.0
        return events

    def test_one_event_per_match_start_display(self):
        # 「対戦を開始します」が 3 秒表示 → 1 回だけ知らせる。消えてから 3 秒たって、また表示されたら、もう 1 回
        statuses = [status("MATCH_START")] * 3 + [status(None)] * 4 + [status("MATCH_START")] * 2 + [status(None)]
        w, clock = self.make([mode("SINGLE")], statuses)
        self.assertEqual(self.run_ticks(w, clock, 12), [(0, "SINGLE"), (7, "SINGLE")])

    def test_flicker_of_the_match_start_text_is_one_event(self):
        # 読み取りが一瞬途切れても（3 秒未満）、同じ表示として扱う
        statuses = [status("MATCH_START"), status(None), status("MATCH_START"), status(None)]
        w, clock = self.make([mode("SINGLE")], statuses)
        self.assertEqual(len(self.run_ticks(w, clock, 6)), 1)

    def test_context_is_the_last_definitive_heading(self):
        w, clock = self.make([mode("SINGLE"), mode(None), mode(None)], [status(None)])
        self.run_ticks(w, clock, 3)
        self.assertEqual(w.context_mode, "SINGLE")   # 読めない（None）では変わらない
        self.assertIsNone(w.last_mode)
        w.note_mode("TEAM")
        self.assertEqual(w.context_mode, "TEAM")
        w.reset_context()
        self.assertIsNone(w.context_mode)

    def test_thread_reads_only_while_active(self):
        # 実際のスレッドで動かす: 動かしている間だけ読み、止めると読まない
        import threading
        import time as real_time
        calls = []
        done = threading.Event()

        def read_mode():
            calls.append(1)
            if len(calls) >= 3:
                done.set()
            return mode("SINGLE")

        w = ScreenStateWatcher(mode_fn=read_mode, status_fn=lambda: status(None), interval=0.02)
        w.start()
        real_time.sleep(0.15)
        self.assertEqual(calls, [])          # 動かす前は読まない
        w.set_active(True)
        self.assertTrue(done.wait(2.0))      # 動かすと読む
        w.set_active(False)
        real_time.sleep(0.1)
        count = len(calls)
        real_time.sleep(0.15)
        self.assertLessEqual(len(calls), count + 1)  # 止めたあとは（読みかけの 1 回を除き）読まない
        w.stop()
        self.assertEqual(w.context_mode, "SINGLE")

    def test_match_start_after_a_pause_is_reported(self):
        # 1 試合目の表示を読んだ直後に止め（試合中）、数分後に動かすと、次の試合の表示がすぐ出ていた。
        # 以前は「消えたのを 3 秒見届ける」まで受け付けず、2 試合目を取りこぼした（2026-10-12）
        w, clock = self.make([mode("SINGLE")], [status("MATCH_START")])
        w.tick()
        self.assertIsNotNone(w.take_match_start())
        clock.now += 240.0  # 試合中（止めている）
        self.assertIsNotNone(self.run_ticks(w, clock, 1) or None)

    def test_stale_event_is_dropped(self):
        w, clock = self.make([mode("SINGLE")], [status("MATCH_START")])
        w.tick()
        clock.now += 25.0  # 本体が受け取らないまま 20 秒を過ぎた（ロード画面が始まっている）
        self.assertIsNone(w.take_match_start())


def render_heading(text):
    """ランクマッチの見出しに似た画像（暗い背景に白い文字）を作る"""
    img = np.full((86, 480, 3), (110, 85, 70), np.uint8)
    cv2.putText(img, text, (60, 62), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (255, 255, 255), 2, cv2.LINE_AA)
    return img


class ModeHeadingTests(unittest.TestCase):
    def test_real_rank_match_screenshot_is_single(self):
        path = ROOT / "rankdata" / "match.jpg"
        if not path.exists():
            self.skipTest("rankdata/match.jpg がありません")
        # 日本語を含むパスは cv2.imread で読めないため、バイト列から読む
        img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        roi = _crop_roi_percent(img, MODE_ROI_PERCENT)
        self.assertEqual(detect_mode(roi).mode, "SINGLE")

    def test_rendered_rank_match_heading_is_single(self):
        self.assertEqual(detect_mode(render_heading("RANK MATCH: SINGLE")).mode, "SINGLE")

    def test_single_without_rank_match_is_not_single(self):
        for text in ("SINGLE", "CUSTOM: SINGLE", "ROOM SINGLE"):
            with self.subTest(text=text):
                self.assertNotEqual(detect_mode(render_heading(text)).mode, "SINGLE")


if __name__ == "__main__":
    unittest.main()
