"""
AC6 の起動を待っている間の、10 分ごとのログ（run_monitor の待機中の分岐）のテスト

以前、このログで log() に msg_key を渡して TypeError になり、監視が止まっていた
（「log() got an unexpected keyword argument 'msg_key'」）。
ゲームを使わない。AC6 が動いていない状態と時計を偽物にして、待機中の分岐を 2 周させる。
実行: python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import importlib  # noqa: E402

monitor_loop = importlib.import_module("ravens_record_core.monitor.loop")
monitor_base = importlib.import_module("ravens_record_core.monitor.base")
logger = importlib.import_module("ravens_record_core.logger")
from ravens_record_core.config import Config  # noqa: E402


class WaitingHeartbeatTests(unittest.TestCase):
    def test_log_accepts_event_fields(self):
        with mock.patch.object(logger, "emit") as emit:
            monitor_base.log("待機中", msg_key="event.waitingAc6", exe="armoredcore6.exe")
        _, kwargs = emit.call_args
        self.assertEqual(kwargs.get("msg_key"), "event.waitingAc6")
        self.assertEqual(kwargs.get("exe"), "armoredcore6.exe")

    def test_waiting_heartbeat_does_not_crash(self):
        now = [1000.0]
        sleeps = []

        def sleep(sec):
            sleeps.append(sec)
            now[0] += 700.0  # 10 分（HEARTBEAT_INTERVAL_SEC）を超えて進める
            if len(sleeps) >= 3:
                raise KeyboardInterrupt  # 待機の分岐を何周かしたら終える（run_monitor は Ctrl+C として終了する）

        logged = []
        ctx = monitor_base.MonitorContext(Config(trigger_exe="nonexistent_rrc_test.exe"))
        with mock.patch.object(monitor_loop, "is_process_running", return_value=False), \
             mock.patch.object(monitor_loop.gamewindow, "diagnose", return_value="[調査用] テスト"), \
             mock.patch.object(monitor_loop.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(monitor_loop.time, "sleep", side_effect=sleep), \
             mock.patch.object(logger, "emit", side_effect=lambda ev, msg="", level="info", **kw: logged.append((msg, kw))):
            monitor_loop.Monitor(ctx).run()  # 以前は、ここで TypeError

        heartbeats = [kw for msg, kw in logged if msg.startswith("[待機中]")]
        self.assertGreaterEqual(len(heartbeats), 2)
        self.assertEqual(heartbeats[0].get("msg_key"), "event.waitingAc6")


if __name__ == "__main__":
    unittest.main()
