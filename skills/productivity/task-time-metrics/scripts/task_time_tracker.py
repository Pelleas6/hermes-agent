#!/usr/bin/env python3
"""Hermes task timing CLI.

Supports explicit/manual phase timing and the automatic observations written by
the ``task-metrics`` plugin.  Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

SEGMENT_KINDS = (
    "agent_work",
    "review",
    "rework",
    "tool_wait",
    "ci_wait",
    "deploy_wait",
    "external_wait",
    "user_wait",
    "blocked",
)
OUTCOMES = ("success", "partial", "failed", "cancelled")
WAIT_OBSERVATION_KINDS = {
    "llm_api",
    "tool",
    "ci_wait",
    "deploy_wait",
    "external_wait",
    "user_wait",
    "blocked",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime] = None) -> str:
    return (value or now_utc()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def default_db_path() -> Path:
    explicit = os.environ.get("HERMES_TASK_TIME_DB")
    if explicit:
        return Path(explicit).expanduser()
    candidate = Path("/opt/data/metrics/task_time.sqlite3")
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if os.access(candidate.parent, os.W_OK):
            return candidate
    except OSError:
        pass
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "metrics" / "task_time.sqlite3"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
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
        CREATE INDEX IF NOT EXISTS idx_segments_task_id ON segments(task_id);
        CREATE INDEX IF NOT EXISTS idx_marks_task_id ON marks(task_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_started_at ON tasks(started_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_profile ON tasks(profile);
        CREATE INDEX IF NOT EXISTS idx_observations_task_ts ON observations(task_id, ts);
        """
    )
    return conn


def parse_meta(values: Optional[Sequence[str]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"Invalid --meta value {item!r}; expected key=value")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit("Metadata key cannot be empty")
        result[key[:80]] = value[:240]
    return result


def get_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown task_id: {task_id}")
    return row


def close_open_segments(conn: sqlite3.Connection, task_id: str, ended_at: str) -> None:
    conn.execute(
        "UPDATE segments SET ended_at=? WHERE task_id=? AND ended_at IS NULL",
        (ended_at, task_id),
    )


def task_observations(conn: sqlite3.Connection, task_id: str) -> List[Dict[str, Any]]:
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM observations WHERE task_id=? ORDER BY id", (task_id,)
            )
        ]
    except sqlite3.Error:
        return []


def task_segments(conn: sqlite3.Connection, task_id: str) -> List[Dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM segments WHERE task_id=? ORDER BY id", (task_id,)
        )
    ]


def duration(start: Any, end: Any, now: datetime) -> float:
    try:
        start_dt = parse_iso(str(start))
        end_dt = parse_iso(str(end)) if end else now
        return max(0.0, (end_dt - start_dt).total_seconds())
    except Exception:
        return 0.0


def summarize_task(conn: sqlite3.Connection, task_id: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or now_utc()
    task = get_task(conn, task_id)
    segments = task_segments(conn, task_id)
    observations = task_observations(conn, task_id)
    wall = duration(task["started_at"], task["finished_at"], now)

    by_kind: Dict[str, float] = {}
    manual_active = 0.0
    manual_wait = 0.0
    for segment in segments:
        seconds = duration(segment.get("started_at"), segment.get("ended_at"), now)
        kind = str(segment.get("kind") or "agent_work")
        by_kind[kind] = by_kind.get(kind, 0.0) + seconds
        if kind in {"agent_work", "review", "rework"}:
            manual_active += seconds
        else:
            manual_wait += seconds

    failures = 0
    tokens: Dict[str, int] = {}
    providers: Dict[str, int] = {}
    tools: Dict[str, int] = {}
    for observation in observations:
        kind = str(observation.get("kind") or "event")
        seconds = max(0.0, float(observation.get("duration_ms") or 0.0)) / 1000.0
        by_kind[kind] = by_kind.get(kind, 0.0) + seconds
        if str(observation.get("status") or "").lower() in {"failed", "failure", "error"}:
            failures += 1
        if observation.get("provider"):
            key = str(observation["provider"])
            providers[key] = providers.get(key, 0) + 1
        if kind == "tool" and observation.get("name"):
            key = str(observation["name"])
            tools[key] = tools.get(key, 0) + 1
        try:
            metadata = json.loads(observation.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if isinstance(metadata, dict):
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "total_tokens",
            ):
                value = metadata.get(key)
                if isinstance(value, (int, float)):
                    tokens[key] = tokens.get(key, 0) + int(value)

    observed_wait = sum(by_kind.get(kind, 0.0) for kind in WAIT_OBSERVATION_KINDS)
    if segments:
        active = manual_active
        wait = manual_wait
        timing_mode = "manual_segments"
    else:
        wait = min(wall, observed_wait)
        active = max(0.0, wall - wait)
        timing_mode = "automatic_observations"

    return {
        "task_id": task["task_id"],
        "title": task["title"],
        "project": task["project"],
        "profile": task["profile"],
        "model": task["model"],
        "source": task["source"],
        "status": task["status"],
        "outcome": task["outcome"],
        "started_at": task["started_at"],
        "finished_at": task["finished_at"],
        "wall_seconds": round(wall, 3),
        "active_seconds": round(active, 3),
        "wait_seconds": round(wait, 3),
        "efficiency_ratio": round(active / wall, 4) if wall > 0 else None,
        "timing_mode": timing_mode,
        "segment_count": len(segments),
        "observation_count": len(observations),
        "failure_observation_count": failures,
        "by_kind_seconds": {key: round(value, 3) for key, value in sorted(by_kind.items()) if value > 0},
        "tokens": tokens,
        "providers": providers,
        "tools": tools,
    }


def cmd_start(args: argparse.Namespace) -> None:
    path = Path(args.db)
    conn = connect(path)
    task_id = args.task_id or str(uuid.uuid4())
    started = iso()
    metadata = parse_meta(args.meta)
    try:
        with conn:
            if conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
                raise SystemExit(f"task_id already exists: {task_id}")
            conn.execute(
                """INSERT INTO tasks(
                       task_id,title,project,profile,model,source,started_at,status,metadata_json
                   ) VALUES(?,?,?,?,?,?,?,'running',?)""",
                (
                    task_id,
                    args.title,
                    args.project,
                    args.profile,
                    args.model,
                    args.source,
                    started,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.execute(
                """INSERT INTO segments(task_id,kind,label,started_at,metadata_json)
                   VALUES(?,?,?,?,?)""",
                (task_id, args.kind, args.label, started, "{}"),
            )
    finally:
        conn.close()
    print(json.dumps({"task_id": task_id, "started_at": started, "db": str(path)}, ensure_ascii=False))


def cmd_switch(args: argparse.Namespace) -> None:
    conn = connect(Path(args.db))
    changed = iso()
    try:
        task = get_task(conn, args.task_id)
        if task["status"] != "running":
            raise SystemExit(f"Task is not running: {args.task_id}")
        with conn:
            close_open_segments(conn, args.task_id, changed)
            conn.execute(
                """INSERT INTO segments(task_id,kind,label,started_at,metadata_json)
                   VALUES(?,?,?,?,?)""",
                (
                    args.task_id,
                    args.kind,
                    args.label,
                    changed,
                    json.dumps(parse_meta(args.meta), ensure_ascii=False, sort_keys=True),
                ),
            )
    finally:
        conn.close()
    print(json.dumps({"task_id": args.task_id, "kind": args.kind, "started_at": changed}, ensure_ascii=False))


def cmd_mark(args: argparse.Namespace) -> None:
    conn = connect(Path(args.db))
    timestamp = iso()
    try:
        get_task(conn, args.task_id)
        with conn:
            conn.execute(
                "INSERT INTO marks(task_id,name,ts,metadata_json) VALUES(?,?,?,?)",
                (
                    args.task_id,
                    args.name,
                    timestamp,
                    json.dumps(parse_meta(args.meta), ensure_ascii=False, sort_keys=True),
                ),
            )
    finally:
        conn.close()
    print(json.dumps({"task_id": args.task_id, "name": args.name, "ts": timestamp}, ensure_ascii=False))


def cmd_finish(args: argparse.Namespace) -> None:
    conn = connect(Path(args.db))
    finished = iso()
    try:
        task = get_task(conn, args.task_id)
        if task["status"] != "running":
            raise SystemExit(f"Task is already finished: {args.task_id}")
        with conn:
            close_open_segments(conn, args.task_id, finished)
            conn.execute(
                """UPDATE tasks SET finished_at=?,status='completed',outcome=?
                   WHERE task_id=? AND status='running'""",
                (finished, args.outcome, args.task_id),
            )
        summary = summarize_task(conn, args.task_id, now=parse_iso(finished))
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


def parse_since(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().lower()
    if len(text) >= 2 and text[-1] in {"h", "d", "w"}:
        units = {"h": 3600, "d": 86400, "w": 604800}
        try:
            return now_utc() - timedelta(seconds=float(text[:-1]) * units[text[-1]])
        except ValueError:
            pass
    try:
        return parse_iso(text)
    except Exception as exc:
        raise SystemExit("--since must be ISO-8601 or like 24h / 7d / 4w") from exc


def matching_tasks(conn: sqlite3.Connection, args: argparse.Namespace) -> List[sqlite3.Row]:
    clauses: List[str] = []
    params: List[Any] = []
    since = parse_since(getattr(args, "since", None))
    if since:
        clauses.append("started_at>=?")
        params.append(iso(since))
    for field in ("project", "profile", "model", "source", "outcome"):
        value = getattr(args, field, None)
        if value:
            clauses.append(f"{field}=?")
            params.append(value)
    query = "SELECT * FROM tasks"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY started_at"
    return list(conn.execute(query, params))


def aggregate(summaries: Sequence[Mapping[str, Any]], include_tasks: bool) -> Dict[str, Any]:
    completed = [item for item in summaries if item.get("status") == "completed"]
    success = [item for item in completed if item.get("outcome") == "success"]
    walls = [float(item.get("wall_seconds") or 0.0) for item in completed]
    actives = [float(item.get("active_seconds") or 0.0) for item in completed]
    waits = [float(item.get("wait_seconds") or 0.0) for item in completed]
    by_kind: Dict[str, float] = {}
    outcomes: Dict[str, int] = {}
    profiles: Dict[str, int] = {}
    models: Dict[str, int] = {}
    sources: Dict[str, int] = {}
    tokens: Dict[str, int] = {}
    for item in summaries:
        outcome = str(item.get("outcome") or item.get("status") or "unknown")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        for field, target in (("profile", profiles), ("model", models), ("source", sources)):
            value = item.get(field)
            if value:
                target[str(value)] = target.get(str(value), 0) + 1
        for key, value in (item.get("by_kind_seconds") or {}).items():
            by_kind[str(key)] = by_kind.get(str(key), 0.0) + float(value)
        for key, value in (item.get("tokens") or {}).items():
            tokens[str(key)] = tokens.get(str(key), 0) + int(value)
    total_wall = sum(walls)
    total_active = sum(actives)
    rework = by_kind.get("rework", 0.0)
    return {
        "task_count": len(summaries),
        "completed_count": len(completed),
        "running_count": len(summaries) - len(completed),
        "success_rate": round(len(success) / len(completed), 4) if completed else None,
        "total_wall_seconds": round(total_wall, 3),
        "total_active_seconds": round(total_active, 3),
        "total_wait_seconds": round(sum(waits), 3),
        "overall_efficiency_ratio": round(total_active / total_wall, 4) if total_wall > 0 else None,
        "rework_share": round(rework / total_active, 4) if total_active > 0 else None,
        "median_wall_seconds": round(statistics.median(walls), 3) if walls else None,
        "p90_wall_seconds": round(percentile(walls, 0.9) or 0.0, 3) if walls else None,
        "median_active_seconds": round(statistics.median(actives), 3) if actives else None,
        "outcomes": outcomes,
        "profiles": profiles,
        "models": models,
        "sources": sources,
        "tokens": tokens,
        "by_kind_seconds": {key: round(value, 3) for key, value in sorted(by_kind.items())},
        "tasks": list(summaries) if include_tasks else None,
    }


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * fraction
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    part = index - low
    return ordered[low] * (1 - part) + ordered[high] * part


def cmd_report(args: argparse.Namespace) -> None:
    conn = connect(Path(args.db))
    try:
        rows = matching_tasks(conn, args)
        summaries = [summarize_task(conn, str(row["task_id"])) for row in rows]
        report = aggregate(summaries, args.include_tasks)
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_show(args: argparse.Namespace) -> None:
    conn = connect(Path(args.db))
    try:
        summary = summarize_task(conn, args.task_id)
        marks = [
            {
                "name": row["name"],
                "ts": row["ts"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
            }
            for row in conn.execute(
                "SELECT name,ts,metadata_json FROM marks WHERE task_id=? ORDER BY id",
                (args.task_id,),
            )
        ]
        summary["marks"] = marks
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_export(args: argparse.Namespace) -> None:
    conn = connect(Path(args.db))
    try:
        rows = matching_tasks(conn, args)
        summaries = [summarize_task(conn, str(row["task_id"])) for row in rows]
    finally:
        conn.close()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "json":
        output.write_text(json.dumps(summaries, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        fields = [
            "task_id", "title", "project", "profile", "model", "source", "status", "outcome",
            "started_at", "finished_at", "wall_seconds", "active_seconds", "wait_seconds",
            "efficiency_ratio", "timing_mode", "observation_count", "failure_observation_count",
        ]
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for summary in summaries:
                writer.writerow({key: summary.get(key) for key in fields})
    print(json.dumps({"exported": len(summaries), "output": str(output), "format": args.format}))


def cmd_doctor(args: argparse.Namespace) -> None:
    path = Path(args.db)
    conn = connect(path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "segments", "marks", "observations", "task_context")
        }
        stale = []
        cutoff = now_utc() - timedelta(hours=float(args.stale_after_hours))
        for row in conn.execute("SELECT task_id,profile,started_at FROM tasks WHERE status='running'"):
            try:
                if parse_iso(row["started_at"]) < cutoff:
                    stale.append({
                        "task_id": row["task_id"],
                        "profile": row["profile"],
                        "started_at": row["started_at"],
                    })
            except Exception:
                continue
    finally:
        conn.close()
    result = {
        "db": str(path),
        "integrity": integrity[0] if integrity else None,
        "counts": counts,
        "stale_running": stale,
        "ok": bool(integrity and str(integrity[0]).lower() == "ok"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if not result["ok"]:
        raise SystemExit(1)


def add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since")
    parser.add_argument("--project")
    parser.add_argument("--profile")
    parser.add_argument("--model")
    parser.add_argument("--source")
    parser.add_argument("--outcome", choices=OUTCOMES)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes task time metrics tracker")
    parser.add_argument("--db", default=str(default_db_path()), help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Start a task and its first manual phase")
    start.add_argument("--task-id")
    start.add_argument("--title", required=True)
    start.add_argument("--project")
    start.add_argument("--profile")
    start.add_argument("--model")
    start.add_argument("--source", default="hermes")
    start.add_argument("--kind", choices=SEGMENT_KINDS, default="agent_work")
    start.add_argument("--label")
    start.add_argument("--meta", action="append")
    start.set_defaults(func=cmd_start)

    switch = sub.add_parser("switch", help="Close current phase and switch timing kind")
    switch.add_argument("task_id")
    switch.add_argument("--kind", choices=SEGMENT_KINDS, required=True)
    switch.add_argument("--label")
    switch.add_argument("--meta", action="append")
    switch.set_defaults(func=cmd_switch)

    mark = sub.add_parser("mark", help="Record an instantaneous milestone")
    mark.add_argument("task_id")
    mark.add_argument("--name", required=True)
    mark.add_argument("--meta", action="append")
    mark.set_defaults(func=cmd_mark)

    finish = sub.add_parser("finish", help="Finish a running task")
    finish.add_argument("task_id")
    finish.add_argument("--outcome", choices=OUTCOMES, required=True)
    finish.set_defaults(func=cmd_finish)

    show = sub.add_parser("show", help="Show one task summary")
    show.add_argument("task_id")
    show.set_defaults(func=cmd_show)

    report = sub.add_parser("report", help="Aggregate timing metrics")
    add_filters(report)
    report.add_argument("--include-tasks", action="store_true")
    report.set_defaults(func=cmd_report)

    export = sub.add_parser("export", help="Export task summaries")
    add_filters(export)
    export.add_argument("--format", choices=("csv", "json"), default="csv")
    export.add_argument("--output", required=True)
    export.set_defaults(func=cmd_export)

    doctor = sub.add_parser("doctor", help="Check database integrity and stale tasks")
    doctor.add_argument("--stale-after-hours", type=float, default=24.0)
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
