from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .common import discover_profiles

_URL_KEYS = (
    "SUPABASE_URL",
    "NEXT_PUBLIC_SUPABASE_URL",
    "VITE_SUPABASE_URL",
    "PUBLIC_SUPABASE_URL",
)
_KEY_KEYS = (
    "SUPABASE_ANON_KEY",
    "NEXT_PUBLIC_SUPABASE_ANON_KEY",
    "VITE_SUPABASE_ANON_KEY",
    "PUBLIC_SUPABASE_ANON_KEY",
)


def _parse_env(path: Path) -> Dict[str, str]:
    wanted = set(_URL_KEYS) | set(_KEY_KEYS)
    result: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return result
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in wanted:
            continue
        value = value.strip().strip('"').strip("'")
        if value:
            result[key] = value
    return result


def _env_sources(root: Path) -> List[Path]:
    paths: List[Path] = []
    for _, home in discover_profiles(root):
        paths.append(home / ".env")
    projects = root / "projects"
    if projects.is_dir():
        for name in (".env", ".env.local", ".env.production", ".env.production.local"):
            paths.extend(projects.glob(f"**/{name}"))
    dedup: List[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key not in seen and path.is_file():
            seen.add(key)
            dedup.append(path)
    return dedup


def _first(mapping: Mapping[str, str], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        value = str(mapping.get(key) or "").strip()
        if value:
            return value
    return None


def _configured_projects(root: Path, configured: Sequence[Any]) -> List[Tuple[str, str, Optional[str]]]:
    file_envs = [_parse_env(path) for path in _env_sources(root)]
    runtime = {key: os.environ.get(key, "") for key in set(_URL_KEYS) | set(_KEY_KEYS)}
    envs = [runtime, *file_envs]

    discovered: List[Tuple[str, str, Optional[str]]] = []
    seen_urls: set[str] = set()

    for item in configured:
        entry = {"name": item} if isinstance(item, str) else dict(item) if isinstance(item, dict) else {}
        name = str(entry.get("name") or "supabase")[:120]
        url = str(entry.get("url") or "").strip()
        key = str(entry.get("anon_key") or "").strip() or None
        if not url:
            for env in envs:
                url = _first(env, _URL_KEYS) or ""
                if url:
                    key = key or _first(env, _KEY_KEYS)
                    break
        if url:
            normalized = url.rstrip("/")
            if normalized not in seen_urls:
                seen_urls.add(normalized)
                discovered.append((name, normalized, key))

    if not discovered:
        for env in envs:
            url = _first(env, _URL_KEYS)
            if not url:
                continue
            normalized = url.rstrip("/")
            if normalized in seen_urls:
                continue
            seen_urls.add(normalized)
            host = urllib.parse.urlparse(normalized).hostname or "supabase"
            name = host.split(".", 1)[0][:120]
            discovered.append((name, normalized, _first(env, _KEY_KEYS)))
    return discovered


def _probe(name: str, url: str, anon_key: Optional[str]) -> Dict[str, Any]:
    endpoint = url.rstrip("/") + "/auth/v1/health"
    headers = {"User-Agent": "Hermes-Observability/1.0", "Accept": "application/json"}
    if anon_key:
        headers["apikey"] = anon_key
    request = urllib.request.Request(endpoint, headers=headers, method="GET")
    started = time.monotonic()
    status: Optional[int] = None
    reason: Optional[str] = None
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            status = int(response.status)
            response.read(4096)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        reason = "http_error"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = type(exc).__name__
    latency_ms = (time.monotonic() - started) * 1000.0
    available = status is not None and status < 500
    return {
        "name": name,
        "configured": True,
        "available": available,
        "status_code": status,
        "latency_ms": round(latency_ms, 1),
        "reason": reason,
        "auth_health": available,
        "credential_mode": "anon" if anon_key else "public",
    }


def collect_supabase(root: Path, config: Mapping[str, Any]) -> Dict[str, Any]:
    projects = _configured_projects(root, config.get("supabase_projects", []))
    if not projects:
        return {
            "configured": bool(config.get("supabase_projects")),
            "available": False,
            "reason": "url_not_found",
            "projects": [],
        }
    results = [_probe(name, url, key) for name, url, key in projects]
    return {
        "configured": True,
        "available": any(item.get("available") for item in results),
        "unavailable_count": sum(1 for item in results if not item.get("available")),
        "projects": results,
    }
