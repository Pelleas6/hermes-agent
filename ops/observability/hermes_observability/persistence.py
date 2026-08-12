from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from .common import atomic_write, discover_profiles, iso, now_utc, open_sqlite_readonly, read_json, table_exists
from .cron_kanban import discover_kanban_dbs
from .health_render import render_alert, render_html, render_markdown


def rotate_history(path: Path, *, max_bytes: int = 20 * 1024 * 1024, keep: int = 6) -> None:
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        stamp = now_utc().strftime("%Y%m%d-%H%M%S")
        rotated = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
        path.rename(rotated)
        old = sorted(path.parent.glob(f"{path.stem}-*{path.suffix}"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in old[keep:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def persist_snapshot(snapshot: Mapping[str, Any], output_dir: Path) -> Dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    current_json = output_dir / "current.json"
    current_md = output_dir / "current.md"
    dashboard = output_dir / "dashboard.html"
    alert_path = output_dir / "latest_alert.txt"
    atomic_write(current_json, json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    atomic_write(current_md, render_markdown(snapshot))
    atomic_write(dashboard, render_html(snapshot))
    atomic_write(alert_path, render_alert(snapshot))
    history = output_dir / "snapshots.jsonl"
    rotate_history(history)
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {
        "json": str(current_json),
        "markdown": str(current_md),
        "html": str(dashboard),
        "alert": str(alert_path),
        "history": str(history),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_verified(source: Path, destination: Path) -> Dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    source_hash = sha256_file(source)
    destination_hash = sha256_file(destination)
    return {
        "kind": "file",
        "source": str(source),
        "destination": str(destination),
        "sha256": destination_hash,
        "integrity_ok": source_hash == destination_hash,
        "restore_test_ok": destination.exists() and destination.stat().st_size == source.stat().st_size,
    }


def sqlite_backup(source: Path, destination: Path) -> Dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{urllib.parse.quote(str(source.resolve()))}?mode=ro", uri=True, timeout=5)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()
        integrity_ok = bool(check and str(check[0]).lower() == "ok")
    finally:
        src.close()
        dst.close()

    restore_test_ok = False
    with tempfile.TemporaryDirectory(prefix="restore-test-", dir=str(destination.parent)) as tmp:
        restored = Path(tmp) / destination.name
        shutil.copy2(destination, restored)
        probe = sqlite3.connect(f"file:{urllib.parse.quote(str(restored.resolve()))}?mode=ro", uri=True, timeout=5)
        try:
            restored_check = probe.execute("PRAGMA integrity_check").fetchone()
            restore_test_ok = bool(restored_check and str(restored_check[0]).lower() == "ok")
        finally:
            probe.close()
    return {
        "kind": "sqlite",
        "source": str(source),
        "destination": str(destination),
        "sha256": sha256_file(destination),
        "integrity_ok": integrity_ok,
        "restore_test_ok": restore_test_ok,
    }


def create_backup(root: Path, output_dir: Path, *, retention_days: int = 14) -> Dict[str, Any]:
    stamp = now_utc().strftime("%Y%m%d-%H%M%S")
    backup_root = root / "backups" / "observability" / stamp
    backup_root.mkdir(parents=True, exist_ok=True)
    records: List[Dict[str, Any]] = []

    databases: List[Tuple[str, Path]] = [("task_time", output_dir / "task_time.sqlite3")]
    files: List[Tuple[str, Path]] = []
    for name, home in discover_profiles(root):
        databases.append((f"state-{name}", home / "state.db"))
        databases.append((f"cron-executions-{name}", home / "cron" / "executions.db"))
        files.extend(
            [
                (f"profile-{name}-config.yaml", home / "config.yaml"),
                (f"profile-{name}-SOUL.md", home / "SOUL.md"),
                (f"profile-{name}-cron-jobs.json", home / "cron" / "jobs.json"),
                (f"profile-{name}-task-metrics-plugin.yaml", home / "plugins" / "task-metrics" / "plugin.yaml"),
                (f"profile-{name}-task-metrics-init.py", home / "plugins" / "task-metrics" / "__init__.py"),
                (f"profile-{name}-task-metrics-storage.py", home / "plugins" / "task-metrics" / "storage.py"),
                (f"profile-{name}-task-time-SKILL.md", home / "skills" / "productivity" / "task-time-metrics" / "SKILL.md"),
                (f"profile-{name}-task-time-tracker.py", home / "skills" / "productivity" / "task-time-metrics" / "scripts" / "task_time_tracker.py"),
            ]
        )
    for board, path in discover_kanban_dbs(root):
        databases.append((f"kanban-{board}", path))

    for label, source in databases:
        if not source.exists():
            continue
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
        try:
            records.append(sqlite_backup(source, backup_root / "sqlite" / f"{safe_label}.sqlite3"))
        except (sqlite3.Error, OSError) as exc:
            records.append(
                {
                    "kind": "sqlite",
                    "source": str(source),
                    "error": type(exc).__name__,
                    "integrity_ok": False,
                    "restore_test_ok": False,
                }
            )

    files.extend(
        [
            ("metrics-current.json", output_dir / "current.json"),
            ("metrics-current.md", output_dir / "current.md"),
            ("metrics-dashboard.html", output_dir / "dashboard.html"),
            ("metrics-observability_config.json", output_dir / "observability_config.json"),
        ]
    )
    scripts_root = root / "scripts"
    for name in (
        "ops_observability.py",
        "ops_snapshot_cron.py",
        "ops_daily_alert_cron.py",
        "ops_weekly_report_cron.py",
    ):
        files.append((f"script-{name}", scripts_root / name))
    package_root = scripts_root / "hermes_observability"
    for module in (
        "__init__.py", "common.py", "system_tasks.py", "cron_kanban.py",
        "integrations.py", "supabase.py", "health_render.py", "persistence.py", "app.py",
    ):
        files.append((f"script-hermes_observability-{module}", package_root / module))

    for label, source in files:
        if not source.is_file():
            continue
        safe_label = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in label)
        try:
            records.append(copy_verified(source, backup_root / "files" / safe_label))
        except OSError as exc:
            records.append(
                {
                    "kind": "file",
                    "source": str(source),
                    "error": type(exc).__name__,
                    "integrity_ok": False,
                    "restore_test_ok": False,
                }
            )

    all_integrity_ok = bool(records) and all(bool(item.get("integrity_ok")) for item in records)
    all_restore_test_ok = bool(records) and all(bool(item.get("restore_test_ok")) for item in records)
    manifest = {
        "schema_version": 2,
        "created_at": iso(),
        "root": str(root),
        "records": records,
        "record_count": len(records),
        "all_integrity_ok": all_integrity_ok,
        "all_restore_test_ok": all_restore_test_ok,
        "secrets_excluded": True,
        "notes": "Environment files and credentials are intentionally excluded.",
    }
    atomic_write(backup_root / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    cutoff = time.time() - max(1, retention_days) * 86400
    parent = backup_root.parent
    for candidate in parent.iterdir():
        if candidate.is_dir() and candidate.stat().st_mtime < cutoff:
            shutil.rmtree(candidate, ignore_errors=True)
    return {"backup_dir": str(backup_root), **manifest}


def doctor(root: Path, output_dir: Path) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    check("root_exists", root.is_dir(), str(root))
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        probe = output_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("metrics_writable", True, str(output_dir))
    except OSError as exc:
        check("metrics_writable", False, type(exc).__name__)

    task_db = output_dir / "task_time.sqlite3"
    if task_db.exists():
        try:
            conn = open_sqlite_readonly(task_db)
            result = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
            check("task_db_integrity", bool(result and str(result[0]).lower() == "ok"), result[0] if result else None)
        except sqlite3.Error as exc:
            check("task_db_integrity", False, type(exc).__name__)
    else:
        check("task_db_integrity", False, "missing")

    profiles = discover_profiles(root)
    check("profiles_discovered", bool(profiles), [name for name, _ in profiles])
    missing_plugins = []
    missing_skills = []
    disabled_plugins = []
    for name, home in profiles:
        if not (home / "plugins" / "task-metrics" / "plugin.yaml").exists():
            missing_plugins.append(name)
        if not (home / "skills" / "productivity" / "task-time-metrics" / "SKILL.md").exists():
            missing_skills.append(name)
        config_path = home / "config.yaml"
        enabled = False
        try:
            import yaml
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
            plugin_cfg = (payload or {}).get("plugins") or {}
            enabled = "task-metrics" in (plugin_cfg.get("enabled") or []) and "task-metrics" not in (plugin_cfg.get("disabled") or [])
        except Exception:
            text = config_path.read_text(encoding="utf-8", errors="ignore") if config_path.exists() else ""
            enabled = "task-metrics" in text
        if not enabled:
            disabled_plugins.append(name)
    check("task_metrics_plugin_all_profiles", not missing_plugins, missing_plugins)
    check("task_time_skill_all_profiles", not missing_skills, missing_skills)
    check("task_metrics_enabled_all_profiles", not disabled_plugins, disabled_plugins)

    scripts_home = root / "scripts"
    required_scripts = [
        "ops_observability.py",
        "ops_snapshot_cron.py",
        "ops_daily_alert_cron.py",
        "ops_weekly_report_cron.py",
    ]
    missing_scripts = [name for name in required_scripts if not (scripts_home / name).exists()]
    check("ops_scripts", not missing_scripts, missing_scripts)

    package = scripts_home / "hermes_observability"
    required_modules = (
        "__init__.py", "common.py", "system_tasks.py", "cron_kanban.py",
        "integrations.py", "supabase.py", "health_render.py", "persistence.py", "app.py",
    )
    missing_modules = [name for name in required_modules if not (package / name).is_file()]
    check("ops_package", not missing_modules, missing_modules)

    required_outputs = ("current.json", "current.md", "dashboard.html", "snapshots.jsonl")
    missing_outputs = [name for name in required_outputs if not (output_dir / name).is_file()]
    check("observability_outputs", not missing_outputs, missing_outputs)

    jobs_payload = read_json(root / "cron" / "jobs.json", [])
    if isinstance(jobs_payload, dict):
        jobs = jobs_payload.get("jobs") or jobs_payload.get("items") or jobs_payload.get("data") or []
        if not isinstance(jobs, list):
            jobs = [value for value in jobs_payload.values() if isinstance(value, dict)]
    elif isinstance(jobs_payload, list):
        jobs = jobs_payload
    else:
        jobs = []
    required_crons = {
        "Observabilite snapshot 15 min",
        "Observabilite alerte quotidienne",
        "Observabilite rapport hebdomadaire",
    }
    cron_by_name = {
        str(job.get("name") or job.get("title") or ""): job
        for job in jobs if isinstance(job, dict)
    }
    missing_crons = sorted(required_crons - set(cron_by_name))
    non_no_agent = sorted(
        name for name in required_crons
        if name in cron_by_name and not bool(cron_by_name[name].get("no_agent") or cron_by_name[name].get("noAgent"))
    )
    check("observability_crons_present", not missing_crons, missing_crons)
    check("observability_crons_no_agent", not non_no_agent, non_no_agent)

    backup_parent = root / "backups" / "observability"
    manifests = sorted(backup_parent.glob("*/manifest.json"), key=lambda item: item.stat().st_mtime, reverse=True) if backup_parent.is_dir() else []
    if manifests:
        latest = read_json(manifests[0], {})
        backup_ok = bool(latest.get("all_integrity_ok")) and bool(latest.get("all_restore_test_ok"))
        check("latest_backup_restore_test", backup_ok, str(manifests[0]))
    else:
        check("latest_backup_restore_test", False, "missing")

    failures = [item for item in checks if not item["ok"]]
    return {"ok": not failures, "checked_at": iso(), "checks": checks, "failure_count": len(failures)}
