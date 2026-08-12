#!/usr/bin/env python3
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
from typing import Any

KINDS = (
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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def default_db_path() -> Path:
    explicit = os.environ.get("HERMES_TASK_TIME_DB")
    if explicit:
        return Path(explicit).expanduser()
    persistent = Path("/opt/data/metrics/task_time.sqlite3")
    try:
        persistent.parent.mkdir(parents=True, exist_ok=True)
        if os.access(persistent.parent, os.W_OK):
            return persistent
    except OSError:
        pass
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "metrics" / "task_time.sqlite3"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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

        CREATE INDEX IF NOT EXISTS idx_segments_task_id ON segments(task_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_started_at ON tasks(started_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
        CREATE INDEX IF NOT EXISTS idx_tasks_profile ON tasks(profile);

        CREATE TABLE IF NOT EXISTS marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            ts TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_marks_task_id ON marks(task_id);
        """
    )
    return conn


def parse_meta(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"Invalid --meta value {item!r}; expected key=value")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit("Metadata key cannot be empty")
        result[key] = value
    return result


def get_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown task_id: {task_id}")
    return row


def close_open_segment(conn: sqlite3.Connection, task_id: str, ended_at: str) -> None:
    conn.execute(
        """
        UPDATE segments
        SET ended_at = ?
        WHERE task_id = ? AND ended_at IS NULL
        """,
        (ended_at, task_id),
    )


def cmd_start(args: argparse.Namespace) -> None:
    db = Path(args.db)
    conn = connect(db)
    task_id = args.task_id or str(uuid.uuid4())
    now = iso()
    meta = parse_meta(args.meta)
    with conn:
        existing = conn.execute("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if existing:
            raise SystemExit(f"task_id already exists: {task_id}")
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, title, project, profile, model, source,
                started_at, status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                task_id,
                args.title,
                args.project,
                args.profile,
                args.model,
                args.source,
                now,
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.execute(
            """
            INSERT INTO segments (task_id, kind, label, started_at, metadata_json)
            VALUES (?, ?, ?, ?, '{}')
            """,
            (task_id, args.kind, args.label, now),
        )
    print(json.dumps({"task_id": task_id, "started_at": now, "db": str(db)}, ensure_ascii=False))


def cmd_switch(args: argparse.Namespace) -> None:
    db = Path(args.db)
    conn = connect(db)
    task = get_task(conn, args.task_id)
    if task["status"] != "running":
        raise SystemExit(f"Task is not running: {args.task_id}")
    now = iso()
    meta = parse_meta(args.meta)
    with conn:
        close_open_segment(conn, args.task_id, now)
        conn.execute(
            """
            INSERT INTO segments (task_id, kind, label, started_at, metadata_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                args.task_id,
                args.kind,
                args.label,
                now,
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
            ),
        )
    print(json.dumps({"task_id": args.task_id, "kind": args.kind, "started_at": now}, ensure_ascii=False))


def cmd_mark(args: argparse.Namespace) -> None:
    db = Path(args.db)
    conn = connect(db)
    get_task(conn, args.task_id)
    now = iso()
    meta = parse_meta(args.meta)
    with conn:
        conn.execute(
            "INSERT INTO marks (task_id, name, ts, metadata_json) VALUES (?, ?, ?, ?)",
            (args.task_id, args.name, now, json.dumps(meta, ensure_ascii=False, sort_keys=True)),
        )
    print(json.dumps({"task_id": args.task_id, "name": args.name, "ts": now}, ensure_ascii=False))


def cmd_finish(args: argparse.Namespace) -> None:
    db = Path(args.db)
    conn = connect(db)
    task = get_task(conn, args.task_id)
    if task["status"] != "running":
        raise SystemExit(f"Task is already finished: {args.task_id}")
    now = iso()
    with conn:
        close_open_segment(conn, args.task_id, now)
        conn.execute(
            """
            UPDATE tasks
            SET finished_at = ?, status = 'completed', outcome = ?
            WHERE task_id = ?
            """,
            (now, args.outcome, args.task_id),
        )
    summary = summarize_task(conn, args.task_id, now_dt=parse_iso(now))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


def duration_seconds(start: str, end: str | None, now_dt: datetime) -> float:
    start_dt = parse_iso(start)
    end_dt = parse_iso(end) if end else now_dt
    return max(0.0, (end_dt - start_dt).total_seconds())


def summarize_task(conn: sqlite3.Connection, task_id: str, now_dt: datetime | None = None) -> dict[str, Any]:
    now_dt = now_dt or utc_now()
    task = get_task(conn, task_id)
    segments = conn.execute(
        "SELECT kind, label, started_at, ended_at FROM segments WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    by_kind: dict[str, float] = {kind: 0.0 for kind in KINDS}
    for seg in segments:
        by_kind.setdefault(seg["kind"], 0.0)
        by_kind[seg["kind"]] += duration_seconds(seg["started_at"], seg["ended_at"], now_dt)

    wall_end = parse_iso(task["finished_at"]) if task["finished_at"] else now_dt
    wall = max(0.0, (wall_end - parse_iso(task["started_at"])).total_seconds())
    active = sum(by_kind.get(k, 0.0) for k in ("agent_work", "review", "rework"))
    wait = sum(v for k, v in by_kind.items() if k not in ("agent_work", "review", "rework"))
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
        "by_kind_seconds": {k: round(v, 3) for k, v in by_kind.items() if v > 0},
    }


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip().lower()
    if value.endswith("d") and value[:-1].isdigit():
        return utc_now() - timedelta(days=int(value[:-1]))
    if value.endswith("h") and value[:-1].isdigit():
        return utc_now() - timedelta(hours=int(value[:-1]))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit("--since must be ISO-8601 or like 24h / 7d / 30d") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def matching_tasks(conn: sqlite3.Connection, args: argparse.Namespace) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    since = parse_since(args.since)
    if since:
        clauses.append("started_at >= ?")
        params.append(iso(since))
    for field in ("project", "profile", "model", "outcome"):
        value = getattr(args, field, None)
        if value:
            clauses.append(f"{field} = ?")
            params.append(value)
    query = "SELECT * FROM tasks"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY started_at"
    return conn.execute(query, params).fetchall()


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def cmd_report(args: argparse.Namespace) -> None:
    db = Path(args.db)
    conn = connect(db)
    rows = matching_tasks(conn, args)
    summaries = [summarize_task(conn, row["task_id"]) for row in rows]
    walls = [s["wall_seconds"] for s in summaries]
    actives = [s["active_seconds"] for s in summaries]
    kinds: dict[str, float] = {}
    outcomes: dict[str, int] = {}
    for summary in summaries:
        outcome_key = summary["outcome"] or summary["status"]
        outcomes[outcome_key] = outcomes.get(outcome_key, 0) + 1
        for kind, seconds in summary["by_kind_seconds"].items():
            kinds[kind] = kinds.get(kind, 0.0) + seconds
    completed = [s for s in summaries if s["status"] == "completed"]
    success = [s for s in completed if s["outcome"] == "success"]
    report = {
        "task_count": len(summaries),
        "completed_count": len(completed),
        "success_rate": round(len(success) / len(completed), 4) if completed else None,
        "total_wall_seconds": round(sum(walls), 3),
        "total_active_seconds": round(sum(actives), 3),
        "overall_efficiency_ratio": round(sum(actives) / sum(walls), 4) if sum(walls) > 0 else None,
        "median_wall_seconds": round(statistics.median(walls), 3) if walls else None,
        "p90_wall_seconds": round(percentile(walls, 0.9), 3) if walls else None,
        "median_active_seconds": round(statistics.median(actives), 3) if actives else None,
        "by_kind_seconds": {k: round(v, 3) for k, v in sorted(kinds.items())},
        "outcomes": outcomes,
        "tasks": summaries if args.include_tasks else None,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_show(args: argparse.Namespace) -> None:
    db = Path(args.db)
    conn = connect(db)
    summary = summarize_task(conn, args.task_id)
    marks = conn.execute(
        "SELECT name, ts, metadata_json FROM marks WHERE task_id = ? ORDER BY id",
        (args.task_id,),
    ).fetchall()
    summary["marks"] = [
        {"name": row["name"], "ts": row["ts"], "metadata": json.loads(row["metadata_json"] or "{}")}
        for row in marks
    ]
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_export(args: argparse.Namespace) -> None:
    db = Path(args.db)
    conn = connect(db)
    rows = matching_tasks(conn, args)
    summaries = [summarize_task(conn, row["task_id"]) for row in rows]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "json":
        output.write_text(json.dumps(summaries, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        fieldnames = [
            "task_id", "title", "project", "profile", "model", "source", "status", "outcome",
            "started_at", "finished_at", "wall_seconds", "active_seconds", "wait_seconds", "efficiency_ratio",
            *[f"{kind}_seconds" for kind in KINDS],
        ]
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for summary in summaries:
                flat = {key: summary.get(key) for key in fieldnames}
                for kind in KINDS:
                    flat[f"{kind}_seconds"] = summary["by_kind_seconds"].get(kind, 0.0)
                writer.writerow(flat)
    print(json.dumps({"exported": len(summaries), "output": str(output), "format": args.format}))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes task time metrics tracker")
    parser.add_argument("--db", default=str(default_db_path()), help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Start a task and its first timing segment")
    start.add_argument("--task-id")
    start.add_argument("--title", required=True)
    start.add_argument("--project")
    start.add_argument("--profile")
    start.add_argument("--model")
    start.add_argument("--source", default="hermes")
    start.add_argument("--kind", choices=KINDS, default="agent_work")
    start.add_argument("--label")
    start.add_argument("--meta", action="append")
    start.set_defaults(func=cmd_start)

    switch = sub.add_parser("switch", help="Close current segment and switch timing kind")
    switch.add_argument("task_id")
    switch.add_argument("--kind", choices=KINDS, required=True)
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

    def add_filters(command: argparse.ArgumentParser) -> None:
        command.add_argument("--since")
        command.add_argument("--project")
        command.add_argument("--profile")
        command.add_argument("--model")
        command.add_argument("--outcome", choices=OUTCOMES)

    report = sub.add_parser("report", help="Aggregate timing metrics")
    add_filters(report)
    report.add_argument("--include-tasks", action="store_true")
    report.set_defaults(func=cmd_report)

    export = sub.add_parser("export", help="Export task summaries")
    add_filters(export)
    export.add_argument("--format", choices=("csv", "json"), default="csv")
    export.add_argument("--output", required=True)
    export.set_defaults(func=cmd_export)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
