from __future__ import annotations

import sqlite3
import statistics
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from .common import (
    classify_error,
    columns,
    iso,
    now_utc,
    open_sqlite_readonly,
    parse_datetime,
    percentile,
    read_json,
    short_hash,
    table_exists,
)


def normalize_jobs(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("jobs", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if all(isinstance(value, dict) for value in payload.values()):
            return [dict(value, id=value.get("id", key)) for key, value in payload.items()]
    return []


def is_job_enabled(job: Mapping[str, Any]) -> bool:
    if job.get("enabled") is False:
        return False
    if job.get("paused") is True:
        return False
    status = str(job.get("status") or "").lower()
    return status not in {"paused", "disabled", "archived"}


def collect_cron(profile: str, home: Path, cutoff: datetime) -> Dict[str, Any]:
    cron_dir = home / "cron"
    jobs_file = cron_dir / "jobs.json"
    payload = read_json(jobs_file, [])
    jobs = normalize_jobs(payload)
    enabled = [job for job in jobs if is_job_enabled(job)]
    no_agent = [job for job in jobs if bool(job.get("no_agent") or job.get("noAgent"))]
    now = now_utc()
    overdue: List[Dict[str, Any]] = []
    for job in enabled:
        next_run = parse_datetime(job.get("next_run_at") or job.get("nextRunAt") or job.get("next_run"))
        if next_run and next_run < now - timedelta(minutes=2):
            overdue.append(
                {
                    "id": str(job.get("id") or "")[:80],
                    "name": str(job.get("name") or job.get("title") or "cron")[:160],
                    "next_run_at": iso(next_run),
                    "overdue_seconds": round((now - next_run).total_seconds(), 1),
                }
            )

    heartbeat_age = None
    success_age = None
    for target, key in ((cron_dir / "ticker_heartbeat", "heartbeat"), (cron_dir / "ticker_last_success", "success")):
        try:
            age = max(0.0, time.time() - target.stat().st_mtime)
        except OSError:
            age = None
        if key == "heartbeat":
            heartbeat_age = age
        else:
            success_age = age

    executions_path = cron_dir / "executions.db"
    statuses: Counter[str] = Counter()
    durations: List[float] = []
    failures: List[Dict[str, Any]] = []
    stale_running: List[Dict[str, Any]] = []
    execution_count = 0
    if executions_path.exists():
        try:
            conn = open_sqlite_readonly(executions_path)
            try:
                if table_exists(conn, "executions"):
                    rows = [dict(row) for row in conn.execute("SELECT * FROM executions ORDER BY claimed_at DESC")]
                    for row in rows:
                        claimed = parse_datetime(row.get("claimed_at"))
                        if not claimed or claimed < cutoff:
                            continue
                        execution_count += 1
                        status = str(row.get("status") or "unknown")
                        statuses[status] += 1
                        started = parse_datetime(row.get("started_at")) or claimed
                        finished = parse_datetime(row.get("finished_at"))
                        if finished:
                            durations.append(max(0.0, (finished - started).total_seconds()))
                        if status in {"failed", "unknown"}:
                            failures.append(
                                {
                                    "job_id": str(row.get("job_id") or "")[:80],
                                    "status": status,
                                    "claimed_at": row.get("claimed_at"),
                                    "error_class": classify_error(row.get("error")),
                                }
                            )
                        if status in {"claimed", "running"}:
                            age = (now - started).total_seconds()
                            stale_running.append(
                                {
                                    "job_id": str(row.get("job_id") or "")[:80],
                                    "status": status,
                                    "age_seconds": round(age, 1),
                                }
                            )
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    name_by_id = {
        str(job.get("id")): str(job.get("name") or job.get("title") or job.get("id") or "cron")[:160]
        for job in jobs
    }
    for failure in failures:
        failure["name"] = name_by_id.get(failure["job_id"], failure["job_id"])
    for item in stale_running:
        item["name"] = name_by_id.get(item["job_id"], item["job_id"])

    return {
        "profile": profile,
        "available": jobs_file.exists() or executions_path.exists(),
        "jobs_file": str(jobs_file),
        "execution_db": str(executions_path),
        "job_count": len(jobs),
        "enabled_job_count": len(enabled),
        "no_agent_job_count": len(no_agent),
        "overdue_count": len(overdue),
        "overdue": overdue[:20],
        "ticker_heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_age is not None else None,
        "ticker_last_success_age_seconds": round(success_age, 1) if success_age is not None else None,
        "execution_count": execution_count,
        "execution_statuses": dict(statuses),
        "failure_count": statuses.get("failed", 0),
        "unknown_count": statuses.get("unknown", 0),
        "running_count": statuses.get("running", 0) + statuses.get("claimed", 0),
        "median_execution_seconds": round(statistics.median(durations), 3) if durations else None,
        "p90_execution_seconds": round(percentile(durations, 0.90) or 0.0, 3) if durations else None,
        "failures": failures[:30],
        "stale_running": sorted(stale_running, key=lambda item: item["age_seconds"], reverse=True)[:20],
    }


def discover_kanban_dbs(root: Path) -> List[Tuple[str, Path]]:
    result: List[Tuple[str, Path]] = []
    default = root / "kanban.db"
    if default.exists():
        result.append(("default", default))
    boards = root / "kanban" / "boards"
    if boards.is_dir():
        for path in sorted(boards.glob("*/kanban.db")):
            result.append((path.parent.name, path))
    return result


def collect_kanban(root: Path, cutoff: datetime) -> Dict[str, Any]:
    boards: List[Dict[str, Any]] = []
    total_statuses: Counter[str] = Counter()
    stale_running: List[Dict[str, Any]] = []
    now = now_utc()
    for board_name, path in discover_kanban_dbs(root):
        try:
            conn = open_sqlite_readonly(path)
        except sqlite3.Error:
            continue
        try:
            if not table_exists(conn, "tasks"):
                continue
            available = columns(conn, "tasks")
            selected = [name for name in ("id", "status", "created_at", "updated_at", "started_at", "completed_at", "claimed_at") if name in available]
            if "status" not in selected:
                continue
            rows = [dict(row) for row in conn.execute("SELECT " + ",".join(selected) + " FROM tasks")]
            statuses: Counter[str] = Counter(str(row.get("status") or "unknown") for row in rows)
            total_statuses.update(statuses)
            for row in rows:
                if str(row.get("status")) != "running":
                    continue
                start = None
                for field in ("started_at", "claimed_at", "updated_at", "created_at"):
                    start = parse_datetime(row.get(field))
                    if start:
                        break
                if start:
                    stale_running.append(
                        {
                            "board": board_name,
                            "task_hash": short_hash(row.get("id")),
                            "age_seconds": round((now - start).total_seconds(), 1),
                        }
                    )
            boards.append(
                {
                    "board": board_name,
                    "path": str(path),
                    "task_count": len(rows),
                    "statuses": dict(statuses),
                }
            )
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    return {
        "available": bool(boards),
        "board_count": len(boards),
        "task_count": sum(board["task_count"] for board in boards),
        "statuses": dict(total_statuses),
        "blocked_count": total_statuses.get("blocked", 0),
        "running_count": total_statuses.get("running", 0),
        "review_count": total_statuses.get("review", 0),
        "done_count": total_statuses.get("done", 0),
        "stale_running": sorted(stale_running, key=lambda item: item["age_seconds"], reverse=True),
        "boards": boards,
    }
