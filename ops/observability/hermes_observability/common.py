from __future__ import annotations

import json
import math
import os
import sqlite3
import statistics
import subprocess
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

VERSION = "1.0.0"
DEFAULT_CONFIG: Dict[str, Any] = {
    "github_repositories": [
        "Pelleas6/palm-mvp",
        "Pelleas6/ArrivalAlert-Android",
        "Pelleas6/site_IA_Pour_Nul",
        "Pelleas6/hermes-agent",
    ],
    "vercel_projects": [{"name": "palm-mvp"}, {"name": "site-ia-pour-nul"}],
    "supabase_projects": [],
    "thresholds": {
        "disk_warning_pct": 80.0,
        "disk_critical_pct": 90.0,
        "memory_warning_pct": 85.0,
        "memory_critical_pct": 92.0,
        "task_success_warning": 0.85,
        "task_success_critical": 0.70,
        "first_pass_success_warning": 0.70,
        "first_pass_success_critical": 0.50,
        "task_min_sample": 5,
        "rework_share_warning": 0.30,
        "stale_task_warning_hours": 6.0,
        "stale_task_critical_hours": 24.0,
        "cron_heartbeat_warning_seconds": 180.0,
        "cron_heartbeat_critical_seconds": 600.0,
        "cron_running_stale_hours": 2.0,
        "kanban_blocked_warning": 1,
        "kanban_blocked_critical": 8,
        "supabase_latency_warning_ms": 1500.0,
        "supabase_latency_critical_ms": 5000.0,
        "backup_warning_hours": 36.0,
        "backup_critical_hours": 72.0,
    },
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime] = None) -> str:
    return (value or now_utc()).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return parse_datetime(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_window(value: str) -> timedelta:
    text = str(value or "7d").strip().lower()
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if len(text) >= 2 and text[-1] in units:
        try:
            return timedelta(seconds=float(text[:-1]) * units[text[-1]])
        except ValueError:
            pass
    raise ValueError("window must look like 30m, 24h, 7d, or 4w")


def resolve_root(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get("HERMES_SHARED_ROOT"):
        return Path(os.environ["HERMES_SHARED_ROOT"]).expanduser().resolve()
    home = Path(os.environ.get("HERMES_HOME", "/opt/data")).expanduser().resolve()
    return home.parent.parent if home.parent.name == "profiles" else home


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def deep_merge(base: Dict[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(root: Path, explicit: Optional[str] = None) -> Tuple[Dict[str, Any], Path]:
    path = Path(explicit).expanduser() if explicit else root / "metrics" / "observability_config.json"
    custom = read_json(path, {})
    return deep_merge(DEFAULT_CONFIG, custom if isinstance(custom, dict) else {}), path


def open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{urllib.parse.quote(str(path.resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * fraction
    low, high = math.floor(index), math.ceil(index)
    return ordered[low] if low == high else ordered[low] * (high - index) + ordered[high] * (index - low)


def human_duration(seconds: Any) -> str:
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "n/a"
    if value < 60:
        return f"{value:.0f}s"
    if value < 3600:
        return f"{value / 60:.1f} min"
    if value < 86400:
        return f"{value / 3600:.1f} h"
    return f"{value / 86400:.1f} j"


def human_bytes(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if abs(number) < 1024 or unit == "To":
            return f"{number:.1f} {unit}"
        number /= 1024
    return "n/a"


def pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def discover_profiles(root: Path) -> list[tuple[str, Path]]:
    result = [("default", root)]
    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        for path in sorted(profiles_dir.iterdir()):
            if path.is_dir() and not path.name.startswith(".") and any(
                (path / marker).exists() for marker in ("config.yaml", "state.db", "cron", "skills", "plugins")
            ):
                result.append((path.name, path))
    return result


def classify_error(value: Any) -> str:
    text = str(value or "").lower()
    buckets = (
        ("auth_failed", ("auth", "credential", "401", "403", "token")),
        ("rate_limited", ("rate", "quota", "429")),
        ("timeout", ("timeout", "timed out")),
        ("network_error", ("network", "dns", "connection", "unreachable")),
        ("invalid_config", ("config", "missing", "not configured")),
        ("interrupted", ("interrupted", "restart", "owner exited")),
        ("empty_response", ("empty response",)),
    )
    for name, needles in buckets:
        if any(needle in text for needle in needles):
            return name
    return "unknown"


def short_hash(value: Any, length: int = 16) -> str:
    import hashlib
    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()[:length]


def run_command(command: Sequence[str], *, timeout: int = 20, cwd: Optional[Path] = None,
                env: Optional[Mapping[str, str]] = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.update({str(k): str(v) for k, v in (env or {}).items()})
    merged.setdefault("NO_COLOR", "1")
    return subprocess.run(list(command), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, cwd=str(cwd) if cwd else None, env=merged, check=False)
