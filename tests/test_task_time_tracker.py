from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "productivity" / "task-time-metrics" / "scripts" / "task_time_tracker.py"
spec = importlib.util.spec_from_file_location("task_time_tracker_test", SCRIPT)
assert spec and spec.loader
tracker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracker)


class TrackerTests(unittest.TestCase):
    def call(self, args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = tracker.main(args)
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_manual_and_automatic_tasks_share_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "metrics.sqlite3")
            started = self.call([
                "--db", db, "start", "--task-id", "manual", "--title", "Manual", "--profile", "dev"
            ])
            self.assertEqual(started["task_id"], "manual")
            self.call(["--db", db, "switch", "manual", "--kind", "ci_wait", "--label", "CI"])
            finished = self.call(["--db", db, "finish", "manual", "--outcome", "success"])
            self.assertEqual(finished["timing_mode"], "manual_segments")

            conn = tracker.connect(Path(db))
            now = tracker.iso()
            conn.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("auto", "Auto", "p", "infra", "gpt", "agent", now, now, "completed", "success", "{}"),
            )
            conn.execute(
                "INSERT INTO observations(task_id,kind,name,ts,duration_ms,status,profile,model,provider,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("auto", "llm_api", "api", now, 100.0, "success", "infra", "gpt", "openai", '{"input_tokens":5}'),
            )
            conn.commit()
            summary = tracker.summarize_task(conn, "auto")
            conn.close()
            self.assertEqual(summary["timing_mode"], "automatic_observations")
            self.assertEqual(summary["tokens"]["input_tokens"], 5)

            report = self.call(["--db", db, "report", "--since", "7d"])
            self.assertEqual(report["task_count"], 2)
            self.assertEqual(report["success_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
