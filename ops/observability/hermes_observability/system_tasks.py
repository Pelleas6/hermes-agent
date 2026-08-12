from __future__ import annotations

import json
import os
import shutil
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from .common import now_utc, open_sqlite_readonly, parse_datetime, percentile, table_exists, columns


def collect_system(root: Path) -> Dict[str, Any]:
    disk = shutil.disk_usage(root)
    disk_pct = disk.used / disk.total * 100 if disk.total else 0.0
    mem: Dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            mem[key] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError):
        pass
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", mem.get("MemFree", 0))
    used = max(0, total - available)
    mem_pct = used / total * 100 if total else None
    cg_current = cg_max = None
    try:
        cg_current = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if raw != "max":
            cg_max = int(raw)
    except (OSError, ValueError):
        pass
    cg_pct = cg_current / cg_max * 100 if cg_current is not None and cg_max not in (None, 0) else None
    load = [None, None, None]
    try:
        load = [float(v) for v in Path("/proc/loadavg").read_text().split()[:3]]
    except (OSError, ValueError):
        pass
    uptime = None
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError):
        pass
    return {
        "hostname": os.uname().nodename if hasattr(os, "uname") else None,
        "cpu_count": os.cpu_count(), "load_1m": load[0], "load_5m": load[1], "load_15m": load[2],
        "uptime_seconds": uptime,
        "disk": {"path": str(root), "total_bytes": disk.total, "used_bytes": disk.used,
                 "free_bytes": disk.free, "used_pct": round(disk_pct, 2)},
        "memory": {"total_bytes": total or None, "used_bytes": used if total else None,
                   "available_bytes": available if total else None,
                   "used_pct": round(mem_pct, 2) if mem_pct is not None else None},
        "cgroup_memory": {"current_bytes": cg_current, "max_bytes": cg_max,
                          "used_pct": round(cg_pct, 2) if cg_pct is not None else None},
    }


def collect_task_metrics(path: Path, cutoff: datetime) -> Dict[str, Any]:
    if not path.exists():
        return {"available": False, "path": str(path), "reason": "missing"}
    try:
        conn = open_sqlite_readonly(path)
    except sqlite3.Error as exc:
        return {"available": False, "path": str(path), "reason": type(exc).__name__}
    try:
        if not table_exists(conn, "tasks"):
            return {"available": False, "path": str(path), "reason": "tasks_table_missing"}
        tasks = [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY started_at")]
        tasks = [row for row in tasks if (parse_datetime(row.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
        ids = [str(row.get("task_id")) for row in tasks]
        observations: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        segments: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for table, target in (("observations", observations), ("segments", segments)):
            if ids and table_exists(conn, table):
                for offset in range(0, len(ids), 500):
                    batch = ids[offset:offset + 500]
                    marks = ",".join("?" for _ in batch)
                    for row in conn.execute(f"SELECT * FROM {table} WHERE task_id IN ({marks}) ORDER BY id", batch):
                        target[str(row["task_id"])].append(dict(row))
        now = now_utc()
        summaries: List[Dict[str, Any]] = []
        profile_counts: Counter[str] = Counter(); model_counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter(); provider_counts: Counter[str] = Counter()
        tool_counts: Counter[str] = Counter(); tokens: Counter[str] = Counter()
        total_rework = total_review = 0.0; failure_observations = 0
        for task in tasks:
            task_id = str(task.get("task_id")); started = parse_datetime(task.get("started_at")) or now
            finished = parse_datetime(task.get("finished_at")); wall = max(0.0, ((finished or now) - started).total_seconds())
            by_kind: Counter[str] = Counter(); manual_active = manual_wait = 0.0
            had_rework = False; had_blocked = False
            for obs in observations.get(task_id, []):
                seconds = max(0.0, float(obs.get("duration_ms") or 0.0)) / 1000.0
                kind = str(obs.get("kind") or "event"); by_kind[kind] += seconds
                had_rework = had_rework or kind == "rework"
                had_blocked = had_blocked or kind == "blocked"
                if kind == "tool" and obs.get("name"): tool_counts[str(obs["name"])] += 1
                if obs.get("provider"): provider_counts[str(obs["provider"])] += 1
                if str(obs.get("status") or "").lower() in {"failed", "failure", "error"}: failure_observations += 1
                try: metadata = json.loads(obs.get("metadata_json") or "{}")
                except (TypeError, json.JSONDecodeError): metadata = {}
                if isinstance(metadata, dict):
                    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens"):
                        if isinstance(metadata.get(key), (int, float)): tokens[key] += int(metadata[key])
            for segment in segments.get(task_id, []):
                seg_start = parse_datetime(segment.get("started_at")); seg_end = parse_datetime(segment.get("ended_at")) or now
                seconds = max(0.0, (seg_end - seg_start).total_seconds()) if seg_start else 0.0
                kind = str(segment.get("kind") or "agent_work"); by_kind[kind] += seconds
                had_rework = had_rework or kind == "rework"
                had_blocked = had_blocked or kind == "blocked"
                if kind in {"agent_work", "review", "rework"}: manual_active += seconds
                else: manual_wait += seconds
            observed_wait = sum(by_kind[k] for k in ("llm_api", "tool", "ci_wait", "deploy_wait", "external_wait", "user_wait", "blocked"))
            if segments.get(task_id): active, wait = manual_active, manual_wait
            else: wait, active = min(wall, observed_wait), max(0.0, wall - min(wall, observed_wait))
            total_rework += by_kind.get("rework", 0.0); total_review += by_kind.get("review", 0.0)
            if task.get("profile"): profile_counts[str(task["profile"])] += 1
            if task.get("model"): model_counts[str(task["model"])] += 1
            if task.get("source"): source_counts[str(task["source"])] += 1
            summaries.append({
                "task_id": task_id, "profile": task.get("profile"), "model": task.get("model"),
                "source": task.get("source"), "status": task.get("status"), "outcome": task.get("outcome"),
                "started_at": task.get("started_at"), "finished_at": task.get("finished_at"),
                "wall_seconds": round(wall, 3), "active_seconds": round(active, 3), "wait_seconds": round(wait, 3),
                "efficiency_ratio": round(active / wall, 4) if wall else None,
                "observation_count": len(observations.get(task_id, [])),
                "had_rework": had_rework, "had_blocked": had_blocked,
                "by_kind_seconds": {k: round(v, 3) for k, v in sorted(by_kind.items()) if v > 0},
            })
        completed = [x for x in summaries if x["status"] == "completed"]
        successes = [x for x in completed if x["outcome"] == "success"]
        failures = [x for x in completed if x["outcome"] == "failed"]
        partials = [x for x in completed if x["outcome"] == "partial"]
        first_pass = [x for x in completed if x["outcome"] == "success" and not x.get("had_rework")]
        reworked = [x for x in completed if x.get("had_rework")]
        blocked_tasks = [x for x in summaries if x.get("had_blocked")]
        running = [x for x in summaries if x["status"] == "running"]
        walls = [x["wall_seconds"] for x in completed]; actives = [x["active_seconds"] for x in completed]
        total_active = sum(actives); stale = []
        for item in running:
            when = parse_datetime(item["started_at"]); age = (now - when).total_seconds() if when else 0
            stale.append({"task_id": item["task_id"], "profile": item["profile"], "age_seconds": round(age, 1)})
        return {
            "available": True, "path": str(path), "task_count": len(summaries), "completed_count": len(completed),
            "running_count": len(running), "success_count": len(successes), "failure_count": len(failures),
            "partial_count": len(partials), "success_rate": round(len(successes) / len(completed), 4) if completed else None,
            "first_pass_success_count": len(first_pass),
            "first_pass_success_rate": round(len(first_pass) / len(completed), 4) if completed else None,
            "reworked_task_count": len(reworked), "blocked_task_count": len(blocked_tasks),
            "median_wall_seconds": round(statistics.median(walls), 3) if walls else None,
            "p90_wall_seconds": round(percentile(walls, .9) or 0, 3) if walls else None,
            "median_active_seconds": round(statistics.median(actives), 3) if actives else None,
            "total_wall_seconds": round(sum(walls), 3), "total_active_seconds": round(total_active, 3),
            "overall_efficiency_ratio": round(total_active / sum(walls), 4) if sum(walls) else None,
            "rework_seconds": round(total_rework, 3), "review_seconds": round(total_review, 3),
            "rework_share": round(total_rework / total_active, 4) if total_active else None,
            "failure_observation_count": failure_observations, "profile_breakdown": dict(profile_counts.most_common()),
            "profile_kpis": aggregate_task_groups(summaries, "profile"),
            "model_kpis": aggregate_task_groups(summaries, "model"),
            "source_kpis": aggregate_task_groups(summaries, "source"),
            "model_breakdown": dict(model_counts.most_common()), "source_breakdown": dict(source_counts.most_common()),
            "provider_breakdown": dict(provider_counts.most_common()), "top_tools": dict(tool_counts.most_common(20)),
            "tokens": dict(tokens), "stale_running": sorted(stale, key=lambda x: x["age_seconds"], reverse=True),
            "tasks": summaries[-200:],
        }
    except sqlite3.Error as exc:
        return {"available": False, "path": str(path), "reason": type(exc).__name__}
    finally:
        conn.close()


def aggregate_task_groups(summaries: Sequence[Mapping[str, Any]], field: str) -> Dict[str, Any]:
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for item in summaries:
        key = str(item.get(field) or "").strip()
        if key:
            grouped[key].append(item)
    result: Dict[str, Any] = {}
    for key, items in sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        completed = [item for item in items if item.get("status") == "completed"]
        success = [item for item in completed if item.get("outcome") == "success"]
        first_pass = [item for item in success if not item.get("had_rework")]
        walls = [float(item.get("wall_seconds") or 0.0) for item in completed]
        actives = [float(item.get("active_seconds") or 0.0) for item in completed]
        waits = [float(item.get("wait_seconds") or 0.0) for item in completed]
        result[key] = {
            "task_count": len(items),
            "completed_count": len(completed),
            "success_rate": round(len(success) / len(completed), 4) if completed else None,
            "first_pass_success_rate": round(len(first_pass) / len(completed), 4) if completed else None,
            "reworked_task_count": sum(1 for item in completed if item.get("had_rework")),
            "median_wall_seconds": round(statistics.median(walls), 3) if walls else None,
            "p90_wall_seconds": round(percentile(walls, 0.9) or 0.0, 3) if walls else None,
            "total_active_seconds": round(sum(actives), 3),
            "total_wait_seconds": round(sum(waits), 3),
            "efficiency_ratio": round(sum(actives) / sum(walls), 4) if sum(walls) else None,
        }
    return result


def collect_session_metrics(profile: str, home: Path, cutoff: datetime) -> Dict[str, Any]:
    path = home / "state.db"
    if not path.exists(): return {"profile": profile, "available": False, "path": str(path), "reason": "missing"}
    try: conn = open_sqlite_readonly(path)
    except sqlite3.Error as exc: return {"profile": profile, "available": False, "path": str(path), "reason": type(exc).__name__}
    try:
        if not table_exists(conn, "sessions"):
            return {"profile": profile, "available": False, "path": str(path), "reason": "sessions_table_missing"}
        available = columns(conn, "sessions")
        wanted = [c for c in ("id", "source", "model", "started_at", "ended_at", "message_count", "tool_call_count",
                  "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "estimated_cost_usd",
                  "actual_cost_usd", "api_call_count", "cost_status") if c in available]
        if not wanted: return {"profile": profile, "available": False, "path": str(path), "reason": "unsupported_schema"}
        rows = [dict(r) for r in conn.execute("SELECT " + ",".join(wanted) + " FROM sessions")]
        rows = [r for r in rows if (parse_datetime(r.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
        models: Counter[str] = Counter(); sources: Counter[str] = Counter(); totals: Counter[str] = Counter(); durations = []
        estimated = actual = 0.0; actual_count = 0
        for row in rows:
            if row.get("model"): models[str(row["model"])] += 1
            if row.get("source"): sources[str(row["source"])] += 1
            for field in ("message_count", "tool_call_count", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "api_call_count"):
                if isinstance(row.get(field), (int, float)): totals[field] += int(row[field])
            if isinstance(row.get("estimated_cost_usd"), (int, float)): estimated += float(row["estimated_cost_usd"])
            if isinstance(row.get("actual_cost_usd"), (int, float)): actual += float(row["actual_cost_usd"]); actual_count += 1
            start, end = parse_datetime(row.get("started_at")), parse_datetime(row.get("ended_at"))
            if start and end: durations.append(max(0.0, (end - start).total_seconds()))
        return {"profile": profile, "available": True, "path": str(path), "session_count": len(rows),
                "completed_session_count": sum(1 for r in rows if r.get("ended_at")), **dict(totals),
                "estimated_cost_usd": round(estimated, 6), "actual_cost_usd": round(actual, 6) if actual_count else None,
                "actual_cost_session_count": actual_count,
                "median_session_seconds": round(statistics.median(durations), 3) if durations else None,
                "p90_session_seconds": round(percentile(durations, .9) or 0, 3) if durations else None,
                "models": dict(models.most_common()), "sources": dict(sources.most_common())}
    except sqlite3.Error as exc:
        return {"profile": profile, "available": False, "path": str(path), "reason": type(exc).__name__}
    finally: conn.close()


def aggregate_sessions(items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    available = [x for x in items if x.get("available")]; totals: Counter[str] = Counter()
    models: Counter[str] = Counter(); sources: Counter[str] = Counter(); estimated = actual = 0.0; actual_ok = False
    for item in available:
        for field in ("session_count", "completed_session_count", "message_count", "tool_call_count", "input_tokens",
                      "output_tokens", "cache_read_tokens", "cache_write_tokens", "api_call_count"):
            totals[field] += int(item.get(field) or 0)
        estimated += float(item.get("estimated_cost_usd") or 0)
        if item.get("actual_cost_usd") is not None: actual_ok = True; actual += float(item.get("actual_cost_usd") or 0)
        models.update(item.get("models") or {}); sources.update(item.get("sources") or {})
    return {"available_profile_count": len(available), "unavailable_profile_count": len(items) - len(available),
            **dict(totals), "estimated_cost_usd": round(estimated, 6),
            "actual_cost_usd": round(actual, 6) if actual_ok else None,
            "models": dict(models.most_common()), "sources": dict(sources.most_common())}
