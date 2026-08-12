from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "task-metrics"


class FakeContext:
    def __init__(self):
        self.hooks = {}
        self.commands = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, description=""):
        self.commands[name] = (handler, description)


def load_plugin(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TaskMetricsPluginTests(unittest.TestCase):
    def test_turn_and_kanban_lifecycle_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "profile"
            home.mkdir()
            db = Path(tmp) / "metrics.sqlite3"
            old = dict(os.environ)
            os.environ["HERMES_HOME"] = str(home)
            os.environ["HERMES_TASK_TIME_DB"] = str(db)
            try:
                plugin = load_plugin("task_metrics_test_one")
                ctx = FakeContext()
                plugin.register(ctx)
                self.assertIn("pre_api_request", ctx.hooks)
                self.assertIn("post_api_request", ctx.hooks)
                self.assertIn("post_tool_call", ctx.hooks)
                self.assertIn("on_session_end", ctx.hooks)
                self.assertIn("kanban_task_claimed", ctx.hooks)
                self.assertIn("task-metrics", ctx.commands)

                ctx.hooks["pre_api_request"](
                    session_id="session-1",
                    task_id="turn-1",
                    model="gpt-test",
                    provider="test-provider",
                    platform="cli",
                    api_mode="openai",
                    api_call_count=1,
                )
                ctx.hooks["post_api_request"](
                    session_id="session-1",
                    task_id="turn-1",
                    model="gpt-test",
                    provider="test-provider",
                    platform="cli",
                    api_mode="openai",
                    api_duration=0.25,
                    usage={"input_tokens": 100, "output_tokens": 20},
                    finish_reason="stop",
                )
                ctx.hooks["post_tool_call"](
                    session_id="session-1",
                    task_id="turn-1",
                    function_name="terminal",
                    duration_ms=50,
                    status="success",
                    result={"success": True},
                )
                ctx.hooks["pre_verify"](
                    session_id="session-1",
                    task_id="turn-1",
                    attempt=1,
                    coding=True,
                    changed_paths=["a.py"],
                )
                ctx.hooks["on_session_end"](
                    session_id="session-1",
                    task_id="turn-1",
                    completed=True,
                    failed=False,
                    interrupted=False,
                    model="gpt-test",
                    platform="cli",
                )

                ctx.hooks["kanban_task_claimed"](
                    task_id="kb-1",
                    profile_name="prompt_maitre",
                    board="main",
                    assignee="worker",
                    run_id="run-1",
                )
                ctx.hooks["kanban_task_completed"](
                    task_id="kb-1",
                    profile_name="worker",
                    board="main",
                    assignee="worker",
                    run_id="run-1",
                    summary="do not store this summary",
                )

                conn = sqlite3.connect(db)
                try:
                    tasks = conn.execute(
                        "SELECT source, status, outcome FROM tasks ORDER BY source"
                    ).fetchall()
                    self.assertEqual(len(tasks), 2)
                    self.assertIn(("agent", "completed", "success"), tasks)
                    self.assertIn(("kanban", "completed", "success"), tasks)
                    kanban_profile = conn.execute(
                        "SELECT profile FROM tasks WHERE source='kanban'"
                    ).fetchone()[0]
                    self.assertEqual(kanban_profile, "worker")
                    observations = conn.execute(
                        "SELECT kind, name, duration_ms, metadata_json FROM observations"
                    ).fetchall()
                    self.assertTrue(any(row[0] == "llm_api" and row[2] == 250.0 for row in observations))
                    self.assertTrue(any(row[0] == "tool" and row[1] == "terminal" for row in observations))
                    self.assertTrue(any(row[0] == "rework" for row in observations))
                    blob = "\n".join(str(row) for row in observations)
                    self.assertNotIn("do not store this summary", blob)
                    self.assertNotIn("session-1", blob)
                finally:
                    conn.close()
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_block_reason_is_classified_not_stored_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "metrics.sqlite3"
            old = dict(os.environ)
            os.environ["HERMES_HOME"] = str(Path(tmp) / "profile")
            os.environ["HERMES_TASK_TIME_DB"] = str(db)
            try:
                plugin = load_plugin("task_metrics_test_two")
                ctx = FakeContext()
                plugin.register(ctx)
                raw_reason = "Secret credential token missing for service"
                ctx.hooks["kanban_task_claimed"](
                    task_id="kb-2", profile_name="infra_ops", board="ops"
                )
                ctx.hooks["kanban_task_blocked"](
                    task_id="kb-2",
                    profile_name="infra_ops",
                    board="ops",
                    reason=raw_reason,
                )
                text = db.read_bytes().decode("utf-8", errors="ignore")
                self.assertNotIn(raw_reason, text)
                conn = sqlite3.connect(db)
                try:
                    metadata = conn.execute(
                        "SELECT metadata_json FROM observations WHERE kind='blocked'"
                    ).fetchone()[0]
                    self.assertIn("auth", metadata)
                finally:
                    conn.close()
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_kanban_retry_creates_a_new_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "metrics.sqlite3"
            old = dict(os.environ)
            os.environ["HERMES_HOME"] = str(Path(tmp) / "profile")
            os.environ["HERMES_TASK_TIME_DB"] = str(db)
            try:
                plugin = load_plugin("task_metrics_test_retry")
                ctx = FakeContext(); plugin.register(ctx)
                ctx.hooks["kanban_task_claimed"](task_id="kb", profile_name="dev", board="b", run_id="run1")
                ctx.hooks["kanban_task_blocked"](task_id="kb", profile_name="dev", board="b", run_id="run1", reason="dependency")
                ctx.hooks["kanban_task_claimed"](task_id="kb", profile_name="dev", board="b", run_id="run2")
                ctx.hooks["kanban_task_completed"](task_id="kb", profile_name="dev", board="b", run_id="run2")
                conn = sqlite3.connect(db)
                try:
                    rows = conn.execute("SELECT outcome FROM tasks WHERE source='kanban' ORDER BY started_at").fetchall()
                    self.assertEqual(sorted(row[0] for row in rows), ["partial", "success"])
                finally:
                    conn.close()
            finally:
                os.environ.clear(); os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
