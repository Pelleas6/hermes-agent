from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from .common import iso, parse_datetime, read_json, run_command


def collect_github(config: Mapping[str, Any], cutoff: datetime) -> Dict[str, Any]:
    repos = [str(item) for item in config.get("github_repositories", []) if str(item).count("/") == 1]
    if not repos:
        return {"configured": False, "available": False, "repositories": []}
    gh = shutil.which("gh")
    if not gh:
        return {"configured": True, "available": False, "reason": "gh_cli_missing", "repositories": []}
    try:
        auth = run_command([gh, "auth", "status"], timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return {"configured": True, "available": False, "reason": "gh_auth_check_failed", "repositories": []}
    if auth.returncode != 0:
        return {"configured": True, "available": False, "reason": "gh_not_authenticated", "repositories": []}

    collected: List[Dict[str, Any]] = []
    total_failures = 0
    total_in_progress = 0
    cutoff_ms = int(cutoff.timestamp() * 1000)
    for repo in repos:
        try:
            result = run_command(
                [gh, "api", f"/repos/{repo}/actions/runs?per_page=50"],
                timeout=25,
                env={"GH_PAGER": "cat"},
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            collected.append({"repository": repo, "available": False, "reason": type(exc).__name__})
            continue
        if result.returncode != 0:
            collected.append({"repository": repo, "available": False, "reason": "api_error"})
            continue
        try:
            payload = json.loads(result.stdout)
            runs = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
        except json.JSONDecodeError:
            runs = []
        recent = []
        for run in runs:
            if not isinstance(run, dict):
                continue
            created = parse_datetime(run.get("created_at"))
            if created and int(created.timestamp() * 1000) < cutoff_ms:
                continue
            status = str(run.get("status") or "unknown")
            conclusion = str(run.get("conclusion") or "") or None
            if conclusion in {"failure", "timed_out", "startup_failure", "action_required"}:
                total_failures += 1
            if status != "completed":
                total_in_progress += 1
            started = parse_datetime(run.get("run_started_at")) or created
            updated = parse_datetime(run.get("updated_at"))
            duration = max(0.0, (updated - started).total_seconds()) if started and updated else None
            recent.append(
                {
                    "id": run.get("id"),
                    "name": str(run.get("name") or "workflow")[:160],
                    "branch": str(run.get("head_branch") or "")[:120],
                    "status": status,
                    "conclusion": conclusion,
                    "created_at": run.get("created_at"),
                    "duration_seconds": round(duration, 1) if duration is not None else None,
                }
            )
        latest = recent[0] if recent else None
        collected.append(
            {
                "repository": repo,
                "available": True,
                "run_count": len(recent),
                "failure_count": sum(1 for run in recent if run["conclusion"] in {"failure", "timed_out", "startup_failure", "action_required"}),
                "in_progress_count": sum(1 for run in recent if run["status"] != "completed"),
                "latest": latest,
                "runs": recent[:20],
            }
        )
    return {
        "configured": True,
        "available": any(item.get("available") for item in collected),
        "failure_count": total_failures,
        "in_progress_count": total_in_progress,
        "repositories": collected,
    }


def discover_vercel_projects(root: Path, configured: Sequence[Any]) -> List[Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for item in configured:
        if isinstance(item, str):
            entry = {"name": item}
        elif isinstance(item, dict):
            entry = dict(item)
        else:
            continue
        name = str(entry.get("name") or entry.get("project_id") or "").strip()
        if name:
            result[name] = entry

    projects_root = root / "projects"
    if projects_root.is_dir():
        for project_file in projects_root.glob("**/.vercel/project.json"):
            payload = read_json(project_file, {})
            if not isinstance(payload, dict):
                continue
            project_id = payload.get("projectId")
            org_id = payload.get("orgId")
            name = str(payload.get("projectName") or project_file.parent.parent.name)
            entry = result.get(name, {"name": name})
            entry.update({"project_id": project_id, "team_id": org_id, "dir": str(project_file.parent.parent)})
            result[name] = entry
    return list(result.values())


def vercel_api_project(project: Mapping[str, Any], cutoff: datetime, token: str) -> Dict[str, Any]:
    project_id = str(project.get("project_id") or project.get("name") or "")
    params = {"projectId": project_id, "limit": "50"}
    team_id = project.get("team_id")
    if team_id:
        params["teamId"] = str(team_id)
    url = "https://api.vercel.com/v6/deployments?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Hermes-Observability/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"name": project.get("name"), "available": False, "reason": type(exc).__name__}
    deployments = payload.get("deployments", []) if isinstance(payload, dict) else []
    recent = []
    for deployment in deployments:
        if not isinstance(deployment, dict):
            continue
        created = parse_datetime(deployment.get("created") or deployment.get("createdAt"))
        if created and created < cutoff:
            continue
        state = str(deployment.get("readyState") or deployment.get("state") or "unknown").upper()
        recent.append(
            {
                "id": deployment.get("uid") or deployment.get("id"),
                "state": state,
                "target": deployment.get("target"),
                "created_at": iso(created) if created else None,
                "url": deployment.get("url"),
            }
        )
    return {
        "name": project.get("name"),
        "available": True,
        "mode": "api",
        "deployment_count": len(recent),
        "error_count": sum(1 for item in recent if item["state"] in {"ERROR", "CANCELED"}),
        "building_count": sum(1 for item in recent if item["state"] in {"BUILDING", "INITIALIZING", "QUEUED"}),
        "latest": recent[0] if recent else None,
        "deployments": recent[:20],
    }


def vercel_cli_project(project: Mapping[str, Any], cutoff: datetime, binary: str) -> Dict[str, Any]:
    name = str(project.get("name") or "")
    command = [binary, "list", name, "--no-color"]
    team_id = project.get("team_id")
    if team_id:
        command.extend(["--scope", str(team_id)])
    try:
        result = run_command(command, timeout=25, cwd=Path(project["dir"]) if project.get("dir") else None)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"name": name, "available": False, "reason": type(exc).__name__}
    if result.returncode != 0:
        return {"name": name, "available": False, "reason": "cli_error"}
    states = Counter()
    for line in result.stdout.splitlines():
        upper = line.upper()
        for state in ("READY", "ERROR", "BUILDING", "INITIALIZING", "QUEUED", "CANCELED"):
            if state in upper:
                states[state] += 1
                break
    return {
        "name": name,
        "available": True,
        "mode": "cli",
        "deployment_count": sum(states.values()),
        "error_count": states["ERROR"] + states["CANCELED"],
        "building_count": states["BUILDING"] + states["INITIALIZING"] + states["QUEUED"],
        "states": dict(states),
        "latest": None,
        "deployments": [],
    }


def collect_vercel(root: Path, config: Mapping[str, Any], cutoff: datetime) -> Dict[str, Any]:
    projects = discover_vercel_projects(root, config.get("vercel_projects", []))
    if not projects:
        return {"configured": False, "available": False, "projects": []}
    token = os.environ.get("VERCEL_TOKEN", "").strip()
    binary = shutil.which("vercel")
    collected = []
    for project in projects:
        if token:
            collected.append(vercel_api_project(project, cutoff, token))
        elif binary:
            collected.append(vercel_cli_project(project, cutoff, binary))
        else:
            collected.append({"name": project.get("name"), "available": False, "reason": "vercel_credentials_or_cli_missing"})
    return {
        "configured": True,
        "available": any(item.get("available") for item in collected),
        "error_count": sum(int(item.get("error_count") or 0) for item in collected),
        "building_count": sum(int(item.get("building_count") or 0) for item in collected),
        "projects": collected,
    }
