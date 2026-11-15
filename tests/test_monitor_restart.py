"""
サービスとして動くとき、監視ループが予期しない例外で止まったら、少し待って始め直すテスト
（Recorder.run_with_restart。以前は監視ごと終了し、次に起動するまで記録されなかった）

実行: python -m unittest tests.test_monitor_restart
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import importlib  # noqa: E402

recorder_module = importlib.import_module("ravens_record_core.recorder")
from ravens_record_core.config import Config  # noqa: E402


class MonitorRestartTest(unittest.TestCase):
    def run_with(self, outcomes, clock=None):
        calls = []
        sleeps = []

        def run():
            calls.append(1)
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome

        recorder = recorder_module.Recorder(Config(trigger_exe="nonexistent_rrc_test.exe"))
        with mock.patch.object(recorder_module, "log"), mock.patch.object(recorder, "run", side_effect=run):
            try:
                recorder.run_with_restart(sleep=sleeps.append, clock=clock or (lambda: 0.0))
            finally:
                self.calls, self.sleeps = len(calls), sleeps

    def test_restarts_after_crash(self):
        self.run_with([RuntimeError("x"), RuntimeError("y"), None])
        self.assertEqual(self.calls, 3)
        self.assertEqual(self.sleeps, [recorder_module.RESTART_DELAY_SEC] * 2)

    def test_gives_up_after_too_many_crashes(self):
        outcomes = [RuntimeError(str(i)) for i in range(recorder_module.RESTART_MAX + 1)]
        with self.assertRaises(RuntimeError):
            self.run_with(outcomes)
        self.assertEqual(self.calls, recorder_module.RESTART_MAX + 1)

    def test_old_crashes_are_forgotten(self):
        times = iter(i * recorder_module.RESTART_WINDOW_SEC for i in range(100))
        outcomes = [RuntimeError(str(i)) for i in range(recorder_module.RESTART_MAX * 2)] + [None]
        self.run_with(outcomes, clock=lambda: next(times))
        self.assertEqual(self.calls, recorder_module.RESTART_MAX * 2 + 1)

    def test_keyboard_interrupt_is_not_restarted(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_with([KeyboardInterrupt()])
        self.assertEqual(self.calls, 1)


if __name__ == "__main__":
    unittest.main()
