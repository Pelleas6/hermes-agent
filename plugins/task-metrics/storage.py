"""Durable, content-minimised storage for the task-metrics Hermes plugin.

The plugin intentionally writes to the same SQLite database as the
``task-time-metrics`` skill.  It adds observational tables without changing the
meaning of the skill's existing ``tasks``, ``segments`` and ``marks`` tables.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

_SCHEMA_VERSION = 2
_INIT_LOCK = threading.RLock()
_INITIALIZED: set[str] = set()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def stable_hash(value: Any, *, length: int = 20) -> str:
    raw = str(value or "").encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:length]


def active_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        value = str(get_active_profile_name() or "").strip()
        if value:
            return value
    except Exception:
        pass

    home = Path(os.environ.get("HERMES_HOME", "/opt/data")).expanduser()
    if home.parent.name == "profiles" and home.name:
        return home.name
    return "default"


def shared_root() -> Path:
    explicit = os.environ.get("HERMES_SHARED_ROOT")
    if explicit:
        return Path(explicit).expanduser()

    home = Path(os.environ.get("HERMES_HOME", "/opt/data")).expanduser()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def default_db_path() -> Path:
    explicit = os.environ.get("HERMES_TASK_TIME_DB")
    if explicit:
        return Path(explicit).expanduser()

    root = shared_root()
    candidate = root / "metrics" / "task_time.sqlite3"
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if os.access(candidate.parent, os.W_OK):
            return candidate
    except OSError:
        pass

    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    return home / "metrics" / "task_time.sqlite3"


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Return bounded, JSON-safe metadata with no arbitrary object repr dumps."""
    if depth > 3:
        return "<nested>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key, item in list(value.items())[:32]:
            key_text = str(key)[:80]
            result[key_text] = _json_safe(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:32]]
    return f"<{type(value).__name__}>"


def _json_dump(value: Optional[Mapping[str, Any]]) -> str:
    return json.dumps(_json_safe(dict(value or {})), ensure_ascii=False, sort_keys=True)


def _merge_json(current: str, extra: Optional[Mapping[str, Any]]) -> str:
    try:
        base = json.loads(current or "{}")
        if not isinstance(base, dict):
            base = {}
    except Exception:
        base = {}
    base.update(dict(_json_safe(dict(extra or {}))))
    return json.dumps(base, ensure_ascii=False, sort_keys=True)


def classify_block_reason(reason: Any) -> str:
    text = str(reason or "").lower()
    buckets = (
        ("auth", ("auth", "credential", "token", "permission", "401", "403")),
        ("rate_limited", ("rate", "quota", "429", "limit")),
        ("network", ("network", "dns", "timeout", "connection", "unreachable")),
        ("dependency", ("dependency", "depends", "waiting for", "parent")),
        ("human_input", ("human", "input", "approval", "decision", "confirm")),
        ("capability", ("capability", "unavailable", "missing tool", "not configured")),
        ("test_failure", ("test", "lint", "build", "compile", "ci")),
    )
    for bucket, needles in buckets:
        if any(needle in text for needle in needles):
            return bucket
    return "unknown"


class TaskMetricsStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or default_db_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_once()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        return conn

    def _initialize_once(self) -> None:
        key = str(self.path.resolve())
        if key in _INITIALIZED:
            return
        with _INIT_LOCK:
            if key in _INITIALIZED:
                return
            conn = self._connect()
            try:
                with conn:
                    conn.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS tasks (
                            task_id TEXT PRIMARY KEY,
                            title TEXT NOT NULL,
                            project TEXT,
                            profile TEXT,
                            model TEXT,
                            source TEXT,
                            started_at TEXT NOT NULL,
                            finished_at TEXT,
                            status TEXT NOT NULL DEFAULT 'running',
                            outcome TEXT,
                            metadata_json TEXT NOT NULL DEFAULT '{}'
                        );

                        CREATE TABLE IF NOT EXISTS segments (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                            kind TEXT NOT NULL,
                            label TEXT,
                            started_at TEXT NOT NULL,
                            ended_at TEXT,
                            metadata_json TEXT NOT NULL DEFAULT '{}'
                        );

                        CREATE TABLE IF NOT EXISTS marks (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                            name TEXT NOT NULL,
                            ts TEXT NOT NULL,
                            metadata_json TEXT NOT NULL DEFAULT '{}'
                        );

                        CREATE TABLE IF NOT EXISTS task_context (
                            context_key TEXT PRIMARY KEY,
                            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                            profile TEXT,
                            source TEXT,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS observations (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                            kind TEXT NOT NULL,
                            name TEXT,
                            ts TEXT NOT NULL,
                            duration_ms REAL,
                            status TEXT,
                            profile TEXT,
                            model TEXT,
                            provider TEXT,
                            metadata_json TEXT NOT NULL DEFAULT '{}'
                        );

                        CREATE TABLE IF NOT EXISTS metrics_schema (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );

                        CREATE INDEX IF NOT EXISTS idx_segments_task_id ON segments(task_id);
                        CREATE INDEX IF NOT EXISTS idx_marks_task_id ON marks(task_id);
                        CREATE INDEX IF NOT EXISTS idx_tasks_started_at ON tasks(started_at);
                        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                        CREATE INDEX IF NOT EXISTS idx_tasks_profile ON tasks(profile);
                        CREATE INDEX IF NOT EXISTS idx_context_task_id ON task_context(task_id);
                        CREATE INDEX IF NOT EXISTS idx_observations_task_ts ON observations(task_id, ts);
                        CREATE INDEX IF NOT EXISTS idx_observations_kind_ts ON observations(kind, ts);
                        """
                    )
                    conn.execute(
                        """INSERT INTO metrics_schema(key, value, updated_at)
                           VALUES('schema_version', ?, ?)
                           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                        (str(_SCHEMA_VERSION), utc_now_iso()),
                    )
            finally:
                conn.close()
            _INITIALIZED.add(key)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def ensure_task(
        self,
        task_id: str,
        *,
        title: str = "Hermes task",
        project: Optional[str] = None,
        profile: Optional[str] = None,
        model: Optional[str] = None,
        source: str = "hermes",
        started_at: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> str:
        now = started_at or utc_now_iso()
        explicit_profile = profile
        insert_profile = profile or active_profile_name()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status, metadata_json FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO tasks(
                           task_id, title, project, profile, model, source,
                           started_at, status, metadata_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)""",
                    (
                        task_id,
                        str(title or "Hermes task")[:160],
                        str(project)[:160] if project else None,
                        insert_profile,
                        str(model)[:160] if model else None,
                        str(source or "hermes")[:80],
                        now,
                        _json_dump(metadata),
                    ),
                )
            else:
                merged = _merge_json(row["metadata_json"], metadata)
                conn.execute(
                    """UPDATE tasks SET
                           project=COALESCE(?, project),
                           profile=COALESCE(?, profile),
                           model=COALESCE(?, model),
                           metadata_json=?
                       WHERE task_id=?""",
                    (project, explicit_profile, model, merged, task_id),
                )
        return task_id

    def bind_context(
        self,
        context_key: str,
        task_id: str,
        *,
        profile: Optional[str] = None,
        source: Optional[str] = None,
    ) -> None:
        now = utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO task_context(
                       context_key, task_id, profile, source, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(context_key) DO UPDATE SET
                       task_id=excluded.task_id,
                       profile=excluded.profile,
                       source=excluded.source,
                       updated_at=excluded.updated_at""",
                (context_key, task_id, profile, source, now, now),
            )

    def resolve_context(self, context_key: str) -> Optional[str]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT task_id FROM task_context WHERE context_key=?", (context_key,)
            ).fetchone()
            return str(row["task_id"]) if row else None
        finally:
            conn.close()

    def unbind_context(self, context_key: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM task_context WHERE context_key=?", (context_key,))

    def task_row(self, task_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def record_observation(
        self,
        task_id: str,
        *,
        kind: str,
        name: Optional[str] = None,
        duration_ms: Optional[float] = None,
        status: Optional[str] = None,
        profile: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        ts: Optional[str] = None,
    ) -> None:
        self.ensure_task(task_id, profile=profile, model=model)
        safe_duration: Optional[float]
        try:
            safe_duration = max(0.0, float(duration_ms)) if duration_ms is not None else None
        except (TypeError, ValueError):
            safe_duration = None
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO observations(
                       task_id, kind, name, ts, duration_ms, status,
                       profile, model, provider, metadata_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    str(kind or "event")[:80],
                    str(name)[:160] if name else None,
                    ts or utc_now_iso(),
                    safe_duration,
                    str(status)[:80] if status else None,
                    profile or active_profile_name(),
                    str(model)[:160] if model else None,
                    str(provider)[:120] if provider else None,
                    _json_dump(metadata),
                ),
            )
            conn.execute(
                """UPDATE tasks SET
                       model=COALESCE(?, model),
                       profile=COALESCE(?, profile)
                   WHERE task_id=?""",
                (model, profile, task_id),
            )

    def mark(
        self,
        task_id: str,
        name: str,
        *,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.ensure_task(task_id)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO marks(task_id, name, ts, metadata_json) VALUES (?, ?, ?, ?)",
                (task_id, str(name)[:120], utc_now_iso(), _json_dump(metadata)),
            )

    def finish_task(
        self,
        task_id: str,
        *,
        outcome: str,
        metadata: Optional[Mapping[str, Any]] = None,
        finished_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        outcome = outcome if outcome in {"success", "partial", "failed", "cancelled"} else "partial"
        now = finished_at or utc_now_iso()
        self.ensure_task(task_id)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status, metadata_json FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            merged = _merge_json(row["metadata_json"] if row else "{}", metadata)
            conn.execute(
                """UPDATE tasks SET
                       finished_at=COALESCE(finished_at, ?),
                       status='completed',
                       outcome=COALESCE(outcome, ?),
                       metadata_json=?
                   WHERE task_id=?""",
                (now, outcome, merged, task_id),
            )
        return self.summary(task_id)

    def summary(self, task_id: str) -> Dict[str, Any]:
        conn = self._connect()
        try:
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                return {"task_id": task_id, "missing": True}
            observations = conn.execute(
                """SELECT kind, duration_ms, status FROM observations
                   WHERE task_id=? ORDER BY id""",
                (task_id,),
            ).fetchall()
            end = parse_iso(task["finished_at"]) if task["finished_at"] else datetime.now(timezone.utc)
            wall = max(0.0, (end - parse_iso(task["started_at"])).total_seconds())
            by_kind: Dict[str, float] = {}
            failures = 0
            for obs in observations:
                duration = float(obs["duration_ms"] or 0.0) / 1000.0
                by_kind[obs["kind"]] = by_kind.get(obs["kind"], 0.0) + duration
                if str(obs["status"] or "").lower() in {"failed", "failure", "error"}:
                    failures += 1
            wait_kinds = {"llm_api", "tool", "ci_wait", "deploy_wait", "external_wait", "user_wait", "blocked"}
            observed_wait = sum(value for kind, value in by_kind.items() if kind in wait_kinds)
            return {
                "task_id": task_id,
                "status": task["status"],
                "outcome": task["outcome"],
                "profile": task["profile"],
                "model": task["model"],
                "source": task["source"],
                "started_at": task["started_at"],
                "finished_at": task["finished_at"],
                "wall_seconds": round(wall, 3),
                "observed_wait_seconds": round(observed_wait, 3),
                "estimated_active_seconds": round(max(0.0, wall - observed_wait), 3),
                "observation_count": len(observations),
                "failure_observation_count": failures,
                "by_kind_seconds": {k: round(v, 3) for k, v in sorted(by_kind.items())},
            }
        finally:
            conn.close()

    def recent_summary(self, *, days: int = 7) -> Dict[str, Any]:
        cutoff = datetime.now(timezone.utc).timestamp() - max(1, int(days)) * 86400
        conn = self._connect()
        try:
            rows = conn.execute("SELECT task_id, started_at, status, outcome FROM tasks ORDER BY started_at DESC").fetchall()
            selected = []
            for row in rows:
                try:
                    if parse_iso(row["started_at"]).timestamp() >= cutoff:
                        selected.append(row)
                except Exception:
                    continue
            completed = [row for row in selected if row["status"] == "completed"]
            success = [row for row in completed if row["outcome"] == "success"]
            running = [row for row in selected if row["status"] == "running"]
            return {
                "days": days,
                "task_count": len(selected),
                "completed_count": len(completed),
                "running_count": len(running),
                "success_rate": round(len(success) / len(completed), 4) if completed else None,
                "db": str(self.path),
            }
        finally:
            conn.close()
