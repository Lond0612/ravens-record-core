"""
公開する入口（Config・Hooks・ログの書き出し・子プロセスの起動）のテスト

ゲームを使わない。実行: python -m unittest discover -s tests
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ravens_record_core import matchlog  # noqa: E402
from ravens_record_core.config import Config  # noqa: E402
from ravens_record_core.hooks import CUSTOM_PARTICIPANT, CUSTOM_SPECTATED, Hooks, Status  # noqa: E402
from ravens_record_core.match_session import MatchSession  # noqa: E402
from ravens_record_core.monitor import children  # noqa: E402
from ravens_record_core.monitor.base import MonitorContext  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_env_round_trip(self):
        config = Config(trigger_exe="game.exe", data_dir=Path("D:/rr"), tesseract_dir=Path("D:/tess"), json_logs=True)
        self.assertEqual(Config.from_env(config.to_env()), config)

    def test_overrides_win_over_env(self):
        config = Config.from_env({"RRC_TRIGGER_EXE": "a.exe", "RRC_DATA_DIR": "D:/env"}, data_dir=Path("D:/arg"))
        self.assertEqual(config.trigger_exe, "a.exe")
        self.assertEqual(config.data_dir, Path("D:/arg"))
        self.assertEqual(config.logs_dir, Path("D:/arg") / "logs")

    def test_child_env_carries_trigger_exe(self):
        # テストで差し替えた監視対象が、子プロセスにも必ず伝わる（実際に動いているゲームに触れない）
        env = children.child_env(Config(trigger_exe="nonexistent_rrc_test.exe"))
        self.assertEqual(env["RRC_TRIGGER_EXE"], "nonexistent_rrc_test.exe")

    def test_child_env_with_json_logs_and_extra(self):
        # JSON のログの設定のまま、ロード画面の監視へ RRC_JSON_LOGS を重ねて渡しても失敗しない（以前は TypeError で起動できなかった）
        env = children.child_env(Config(trigger_exe="nonexistent_rrc_test.exe", json_logs=True), RRC_JSON_LOGS="1")
        self.assertEqual(env["RRC_JSON_LOGS"], "1")

    def test_child_command(self):
        with mock.patch.object(children, "is_frozen", return_value=False):
            self.assertEqual(children.child_command("--score-detector")[1:], ["-m", "ravens_record_core", "--score-detector"])
        with mock.patch.object(children, "is_frozen", return_value=True):
            self.assertEqual(children.child_command("--loading-watcher")[1:], ["--loading-watcher"])


class HooksTest(unittest.TestCase):
    def test_hook_errors_do_not_stop_monitoring(self):
        class Broken(Hooks):
            def is_ready(self):
                raise RuntimeError("x")

            def on_issue(self, kind, **detail):
                raise RuntimeError("y")

            def on_custom_match(self, kind, payload, path):
                raise RuntimeError("z")

        ctx = MonitorContext(Config(trigger_exe="nonexistent_rrc_test.exe"), Broken())
        with mock.patch("ravens_record_core.monitor.base.log") as log:
            self.assertFalse(ctx.is_ready())               # 例外は「まだ」
            ctx.hooks.on_issue("map_timeout")              # 例外を外に出さない
            self.assertTrue(ctx.hooks.on_custom_match(CUSTOM_PARTICIPANT, {}, None))  # 「記録しました」は出す
        self.assertEqual(log.call_count, 3)

    def test_status_changes_are_emitted_once(self):
        ctx = MonitorContext(Config(trigger_exe="nonexistent_rrc_test.exe"))
        with mock.patch("ravens_record_core.logger.emit") as emit:
            ctx.set_status("waiting_ac6", "待機中")
            ctx.set_status("waiting_ac6", "待機中")
            ctx.set_status("in_match", "対戦中", "status.inMatch")
        self.assertEqual([c.kwargs["state"] for c in emit.call_args_list], ["waiting_ac6", "in_match"])
        self.assertEqual(ctx.current_status(), "in_match")

    def test_not_ready_status_comes_from_hooks(self):
        class Login(Hooks):
            def not_ready_status(self):
                return Status("needs_login", "ログインしてください", "status.needsLogin")

        ctx = MonitorContext(Config(trigger_exe="nonexistent_rrc_test.exe"), Login())
        with mock.patch("ravens_record_core.logger.emit") as emit:
            ctx.set_not_ready_status()
        self.assertEqual(emit.call_args.kwargs["state"], "needs_login")


class StopTest(unittest.TestCase):
    def test_monitor_keeps_working_after_children_are_stopped_from_another_thread(self):
        # 後始末（stop_everything）のあとに監視ループが 1 周しても落ちない（以前は ctx.screen が None になり AttributeError）
        from ravens_record_core.monitor import loop
        ctx = MonitorContext(Config(trigger_exe="nonexistent_rrc_test.exe"))
        monitor = loop.Monitor(ctx)
        calls = []

        def stop_requested():
            calls.append(1)
            if len(calls) == 1:
                monitor.stop_everything()  # 別のスレッドの後始末の代わり
            return len(calls) > 2

        with mock.patch.object(loop, "is_process_running", return_value=False), \
                mock.patch.object(loop.time, "sleep"), mock.patch.object(loop, "log"), mock.patch.object(loop, "log_block"), \
                mock.patch("ravens_record_core.logger.emit"):
            monitor.run(stop_requested=stop_requested)  # 例外を投げない
        self.assertIsNone(ctx.screen)

    def test_wait_for_match_start_returns_when_stop_is_requested(self):
        from ravens_record_core.detectors.screen_state_watcher import ScreenStateWatcher
        from ravens_record_core.monitor import match_start
        ctx = MonitorContext(Config(trigger_exe="nonexistent_rrc_test.exe"))
        ctx.stop_requested = lambda: True
        watcher = ScreenStateWatcher(mode_fn=lambda: None, status_fn=lambda: None)
        with mock.patch.object(match_start, "is_process_running", return_value=True), \
                mock.patch.object(match_start, "log"), mock.patch("ravens_record_core.logger.emit"):
            self.assertFalse(match_start.wait_for_match_start(ctx, watcher))


class MatchLogTest(unittest.TestCase):
    def test_rank_session_json_has_log_fields(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("ravens_record_core.paths.logs_dir", return_value=Path(d)), \
                mock.patch("ravens_record_core.match_session.logger.log"):
            session = MatchSession.open()
            session.score_my, session.score_enemy = 2, 1
            session.rate_before, session.rate_after = 1500, 1516
            session.score_basis = "boxes"
            session.app_version = "9.9.9"
            path = Path(session.close())
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(path.name.endswith("_2-1_session.json"))
        self.assertEqual(data["result"], "WIN")
        self.assertEqual(data["kind"], "rank")
        self.assertEqual(data["log_schema"], 1)
        self.assertEqual(data["score_basis"], "boxes")
        self.assertEqual(data["app_version"], "9.9.9")
        self.assertIsNotNone(data["ended_at"])
        self.assertNotIn("map_feature", data)  # 大きな特徴量は書かない

    def test_custom_match_json(self):
        payload = {"played_at": "2026-11-15T21:00:00+09:00", "own_wins": 2, "opp_wins": None, "map_raw": "汚染市街 - A"}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(matchlog.logger, "log"):
            path = matchlog.write_custom_match(Path(d), CUSTOM_PARTICIPANT, payload, datetime(2026, 11, 15, 21, 5).astimezone())
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path.name, "2026-11-15_21-00-00_custom_2-X.json")
        self.assertEqual(data["kind"], "custom")
        self.assertEqual(data["map_raw"], "汚染市街 - A")
        self.assertIn("ended_at", data)

    def test_spectated_json_name(self):
        payload = {"started_at": "2026-11-15T21:00:00+09:00", "ended_at": "2026-11-15T21:04:00+09:00", "alpha_wins": 1, "beta_wins": 2}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(matchlog.logger, "log"):
            path = matchlog.write_custom_match(Path(d), CUSTOM_SPECTATED, payload, datetime.now().astimezone())
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path.name, "2026-11-15_21-00-00_custom_spectated_1-2.json")
        self.assertEqual(data["ended_at"], "2026-11-15T21:04:00+09:00")

    def test_jsonl_sink(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            sink = matchlog.JsonlFileSink(path, include_logs=False)
            sink({"event": "log", "message": "ふつうのログ"})
            sink({"event": "status", "message": "対戦中", "state": "in_match"})
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["event"] for line in lines], ["status"])


class ImportTest(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        # 読み込んだだけでは、環境変数・Tesseract の設定・ファイルに触れない（別のプロセスで確かめる）
        code = (
            # OpenCV は、読み込むと自分で PATH に足す（ライブラリの外の動き）ので、先に読み込んでおく
            "import os, sys, cv2, numpy, mss, psutil, pytesseract; before = dict(os.environ); "
            "original = pytesseract.pytesseract.run_tesseract; "
            "import ravens_record_core.monitor.loop, ravens_record_core.recorder, ravens_record_core.cli; "
            "assert dict(os.environ) == before, 'environ'; "
            "assert pytesseract.pytesseract.run_tesseract is original, 'tesseract'; "
            "from ravens_record_core import runtime; assert runtime._config is None, 'config'"
        )
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, LOCALAPPDATA=d)
            env.pop("RRC_DATA_DIR", None)
            result = subprocess.run([sys.executable, "-c", code], cwd=d, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_no_product_references(self):
        # 使う側のアプリの処理（サーバー・ログインなど）に、ライブラリが依存していない
        pattern = re.compile(r"ac6_app|supabase|service_mode|local_(store|server|backup|client|rules|youtube)|offline_(cache|tags)"
                             r"|intro_collector|sync_queue|template_sync", re.IGNORECASE)
        hits = []
        for path in (ROOT / "src").rglob("*.py"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
