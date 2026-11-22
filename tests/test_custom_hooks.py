"""カスタムマッチの対戦の始まりと終わりを、使う側のアプリに知らせる（試合のクリップの録画に使う）"""

import unittest
from unittest import mock

from ravens_record_core.hooks import CUSTOM_PARTICIPANT, Hooks
from ravens_record_core.monitor import custom


class Recorder(Hooks):
    def __init__(self):
        self.calls = []

    def on_custom_match_started(self):
        self.calls.append(("started",))

    def on_custom_match_ended(self, kind, payload):
        self.calls.append(("ended", kind, payload))


class CustomHooksTest(unittest.TestCase):
    def setUp(self):
        self.hooks = Recorder()
        self.ctx = mock.Mock(hooks=self.hooks)

    def test_started_and_ended_with_the_recorded_match(self):
        with mock.patch.object(custom, "_run_custom_match", return_value=(CUSTOM_PARTICIPANT, {"own_wins": 2})):
            custom.run_custom_match(self.ctx, None)
        self.assertEqual(self.hooks.calls, [("started",), ("ended", CUSTOM_PARTICIPANT, {"own_wins": 2})])

    def test_ended_is_called_even_when_monitoring_fails(self):
        with mock.patch.object(custom, "_run_custom_match", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                custom.run_custom_match(self.ctx, None)
        self.assertEqual(self.hooks.calls, [("started",), ("ended", None, None)])


if __name__ == "__main__":
    unittest.main()
