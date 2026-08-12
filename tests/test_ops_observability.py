from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "observability" / "ops_observability.py"
spec = importlib.util.spec_from_file_location("ops_observability_test", SCRIPT)
assert spec and spec.loader
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class OpsObservabilityTests(unittest.TestCase):
    def make_root(self, base: Path) -> Path:
        root = base / "data"
        (root / "metrics").mkdir(parents=True)
        (root / "cron").mkdir()
        (root / "scripts").mkdir()
        (root / "plugins" / "task-metrics").mkdir(parents=True)
        (root / "skills" / "productivity" / "task-time-metrics").mkdir(parents=True)
        (root / "plugins" / "task-metrics" / "plugin.yaml").write_text("name: task-metrics\n")
        (root / "plugins" / "task-metrics" / "__init__.py").write_text("# plugin\n")
        (root / "plugins" / "task-metrics" / "storage.py").write_text("# storage\n")
        (root / "skills" / "productivity" / "task-time-metrics" / "SKILL.md").write_text("---\nname: task-time-metrics\n---\n")
        tracker_dir = root / "skills" / "productivity" / "task-time-metrics" / "scripts"
        tracker_dir.mkdir()
        (tracker_dir / "task_time_tracker.py").write_text("# tracker\n")
        (root / "config.yaml").write_text("plugins:\n  enabled:\n    - task-metrics\n", encoding="utf-8")
        (root / ".env").write_text("PRIVATE_TEST_SECRET=must-not-be-backed-up\n", encoding="utf-8")
        for script in (
            "ops_observability.py",
            "ops_snapshot_cron.py",
            "ops_daily_alert_cron.py",
            "ops_weekly_report_cron.py",
        ):
            (root / "scripts" / script).write_text("# test\n")
        package = root / "scripts" / "hermes_observability"
        package.mkdir()
        for module in (
            "__init__.py", "common.py", "system_tasks.py", "cron_kanban.py",
            "integrations.py", "supabase.py", "health_render.py", "persistence.py", "app.py",
        ):
            (package / module).write_text("# test\n")

        now = datetime.now(timezone.utc)
        task_db = sqlite3.connect(root / "metrics" / "task_time.sqlite3")
        task_db.executescript(
            """
            CREATE TABLE tasks(
              task_id TEXT PRIMARY KEY,title TEXT,project TEXT,profile TEXT,model TEXT,source TEXT,
              started_at TEXT,finished_at TEXT,status TEXT,outcome TEXT,metadata_json TEXT
            );
            CREATE TABLE observations(
              id INTEGER PRIMARY KEY,task_id TEXT,kind TEXT,name TEXT,ts TEXT,duration_ms REAL,
              status TEXT,profile TEXT,model TEXT,provider TEXT,metadata_json TEXT
            );
            CREATE TABLE segments(
              id INTEGER PRIMARY KEY,task_id TEXT,kind TEXT,label TEXT,started_at TEXT,ended_at TEXT,metadata_json TEXT
            );
            """
        )
        start = (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        finish = now.isoformat().replace("+00:00", "Z")
        task_db.execute(
            "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("t1", "x", "p", "dev", "gpt", "agent", start, finish, "completed", "success", "{}"),
        )
        task_db.execute(
            "INSERT INTO observations VALUES(NULL,?,?,?,?,?,?,?,?,?,?)",
            ("t1", "llm_api", "openai", finish, 1000, "success", "dev", "gpt", "openai", '{"input_tokens":10,"output_tokens":2}'),
        )
        task_db.commit()
        task_db.close()

        state = sqlite3.connect(root / "state.db")
        state.execute(
            """CREATE TABLE sessions(
              id TEXT,source TEXT,model TEXT,started_at REAL,ended_at REAL,message_count INTEGER,
              tool_call_count INTEGER,input_tokens INTEGER,output_tokens INTEGER,cache_read_tokens INTEGER,
              cache_write_tokens INTEGER,estimated_cost_usd REAL,actual_cost_usd REAL,api_call_count INTEGER
            )"""
        )
        state.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("s", "cli", "gpt", now.timestamp() - 100, now.timestamp(), 2, 1, 10, 2, 0, 0, 0.01, None, 1),
        )
        state.commit()
        state.close()

        (root / "cron" / "jobs.json").write_text(
            json.dumps({"jobs": [
                {"id": "j1", "name": "bad job", "enabled": True, "no_agent": True},
                {"id": "obs1", "name": "Observabilite snapshot 15 min", "enabled": True, "no_agent": True},
                {"id": "obs2", "name": "Observabilite alerte quotidienne", "enabled": True, "no_agent": True},
                {"id": "obs3", "name": "Observabilite rapport hebdomadaire", "enabled": True, "no_agent": True}
            ]})
        )
        executions = sqlite3.connect(root / "cron" / "executions.db")
        executions.execute(
            """CREATE TABLE executions(
              id TEXT,job_id TEXT,source TEXT,process_id TEXT,pid INTEGER,process_started_at INTEGER,
              status TEXT,claimed_at TEXT,started_at TEXT,finished_at TEXT,error TEXT
            )"""
        )
        executions.execute(
            "INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("e1", "j1", "cron", "p", 1, 1, "failed", finish, finish, finish, "timeout waiting"),
        )
        executions.commit()
        executions.close()
        (root / "cron" / "ticker_heartbeat").write_text("x")
        (root / "cron" / "ticker_last_success").write_text("x")

        kanban = sqlite3.connect(root / "kanban.db")
        kanban.execute("CREATE TABLE tasks(id TEXT,status TEXT,created_at TEXT,updated_at TEXT)")
        kanban.execute("INSERT INTO tasks VALUES(?,?,?,?)", ("k1", "blocked", start, finish))
        kanban.execute("INSERT INTO tasks VALUES(?,?,?,?)", ("k2", "running", start, finish))
        kanban.commit()
        kanban.close()
        return root

    def test_snapshot_dashboard_alert_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_root(Path(tmp))
            config = ops.deep_merge(
                ops.DEFAULT_CONFIG,
                {"github_repositories": [], "vercel_projects": []},
            )
            snapshot = ops.build_snapshot(root, config, window=timedelta(days=7))
            self.assertTrue(snapshot["tasks"]["available"])
            self.assertEqual(snapshot["tasks"]["success_count"], 1)
            self.assertEqual(snapshot["tasks"]["first_pass_success_rate"], 1.0)
            self.assertEqual(snapshot["tasks"]["profile_kpis"]["dev"]["success_rate"], 1.0)
            self.assertEqual(snapshot["sessions_total"]["session_count"], 1)
            self.assertEqual(snapshot["cron"][0]["failure_count"], 1)
            self.assertEqual(snapshot["kanban"]["blocked_count"], 1)
            self.assertEqual(snapshot["kanban"]["running_count"], 1)
            self.assertTrue(snapshot["kanban"]["stale_running"])
            self.assertEqual(snapshot["health"]["status"], "critical")
            paths = ops.persist_snapshot(snapshot, root / "metrics")
            for value in paths.values():
                self.assertTrue(Path(value).exists())
            self.assertIn("cron", ops.render_alert(snapshot).lower())
            self.assertIn("Observabilité Hermes", ops.render_html(snapshot))
            backup = ops.create_backup(root, root / "metrics", retention_days=14)
            self.assertTrue(backup["all_integrity_ok"])
            self.assertTrue(backup["all_restore_test_ok"])
            self.assertTrue(Path(backup["backup_dir"]).exists())
            manifest_text = (Path(backup["backup_dir"]) / "manifest.json").read_text(encoding="utf-8")
            self.assertNotIn(".env", manifest_text)
            self.assertNotIn("must-not-be-backed-up", manifest_text)
            refreshed = ops.build_snapshot(root, config, window=timedelta(days=7))
            self.assertTrue(refreshed["backup"]["all_restore_test_ok"])
            doctor = ops.doctor(root, root / "metrics")
            self.assertTrue(doctor["ok"], doctor)


    def test_supabase_probe_never_exposes_credentials(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, _limit=-1): return b'{"status":"ok"}'

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            root.mkdir()
            secret = "test-secret-anon-key"
            (root / ".env").write_text(
                "SUPABASE_URL=https://project-ref.supabase.co\n"
                f"SUPABASE_ANON_KEY={secret}\n",
                encoding="utf-8",
            )
            with patch("urllib.request.urlopen", return_value=Response()):
                result = ops.collect_supabase(root, {"supabase_projects": []})
            self.assertTrue(result["available"])
            self.assertEqual(result["projects"][0]["credential_mode"], "anon")
            self.assertNotIn(secret, json.dumps(result))

    def test_parse_window(self):
        self.assertEqual(ops.parse_window("24h"), timedelta(hours=24))
        self.assertEqual(ops.parse_window("7d"), timedelta(days=7))
        with self.assertRaises(ValueError):
            ops.parse_window("bad")


if __name__ == "__main__":
    unittest.main()
