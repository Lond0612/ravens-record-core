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

    def on_custom_role(self, role, own_side):
        self.calls.append(("role", role, own_side))


class CustomHooksTest(unittest.TestCase):
    def setUp(self):
        self.hooks = Recorder()
        self.ctx = mock.Mock(hooks=self.hooks)
        patches = [mock.patch.object(custom, "start_loading_watcher"), mock.patch.object(custom, "stop_loading_watcher")]
        self.start_watcher, self.stop_watcher = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)

    def test_started_and_ended_with_the_recorded_match(self):
        with mock.patch.object(custom, "_run_custom_match", return_value=(CUSTOM_PARTICIPANT, {"own_wins": 2})):
            custom.run_custom_match(self.ctx, None)
        self.assertEqual(self.hooks.calls, [("started",), ("ended", CUSTOM_PARTICIPANT, {"own_wins": 2})])
        # ALPHA の紹介の始まりを知るため、ランクマッチと同じロード画面の監視を動かし、終わったら止める
        self.start_watcher.assert_called_once_with(self.ctx)
        self.stop_watcher.assert_called_once_with(self.ctx)

    def test_ended_is_called_even_when_monitoring_fails(self):
        with mock.patch.object(custom, "_run_custom_match", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                custom.run_custom_match(self.ctx, None)
        self.assertEqual(self.hooks.calls, [("started",), ("ended", None, None)])



class CustomRoleTest(unittest.TestCase):
    """参加者か観戦かが決まったら、自分の陣営（参加者のとき）と一緒に知らせる（クリップの表紙に使う）"""

    def setUp(self):
        self.hooks = Recorder()
        self.ctx = mock.Mock(hooks=self.hooks, trigger_exe="x.exe")
        for p in (mock.patch.object(custom, "is_process_running", return_value=True),
                  mock.patch.object(custom, "capture_frame_with_fallback", return_value=object()),
                  mock.patch.object(custom.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def role(self, ap=False, camera=False, sides=()):
        with mock.patch.object(custom.custom_match, "has_spectator_camera", return_value=camera), \
             mock.patch.object(custom.custom_match, "has_ap_hud", return_value=ap), \
             mock.patch.object(custom, "read_own_side", side_effect=list(sides) or [None] * 10):
            return custom._wait_custom_role(self.ctx, custom.time.monotonic())

    def test_participant_with_own_side(self):
        self.assertEqual(self.role(ap=True, sides=["BETA"]), "participant")
        self.assertEqual(self.hooks.calls, [("role", "participant", "BETA")])

    def test_participant_side_is_read_again_until_visible(self):
        self.assertEqual(self.role(ap=True, sides=[None, None, "ALPHA"]), "participant")
        self.assertEqual(self.hooks.calls, [("role", "participant", "ALPHA")])

    def test_participant_side_unknown(self):
        self.assertEqual(self.role(ap=True), "participant")
        self.assertEqual(self.hooks.calls, [("role", "participant", None)])

    def test_spectator_has_no_side(self):
        clock = iter(range(0, 1000))
        with mock.patch.object(custom.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertEqual(self.role(camera=True), "spectator")
        self.assertEqual(self.hooks.calls, [("role", "spectator", None)])


if __name__ == "__main__":
    unittest.main()
