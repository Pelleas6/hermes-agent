"""Automatic task timing and delivery-efficiency metrics for Hermes.

The plugin observes existing Hermes lifecycle hooks.  It never blocks the agent
pipeline and never stores prompts, responses, tool arguments, tool results, raw
errors, phone numbers, or credentials.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .storage import (
    TaskMetricsStore,
    active_profile_name,
    classify_block_reason,
    stable_hash,
)

_STORE: Optional[TaskMetricsStore] = None
_STORE_LOCK = threading.RLock()
_REGISTERED_HOOKS: list[str] = []


def _store() -> TaskMetricsStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = TaskMetricsStore()
    return _STORE


def _profile(value: Any = None) -> str:
    text = str(value or "").strip()
    return text or active_profile_name()


def _context_key(*, profile: str, task_id: Any = None, session_id: Any = None) -> str:
    if task_id:
        return f"raw-task:{profile}:{stable_hash(task_id)}"
    return f"session:{profile}:{stable_hash(session_id or 'unknown')}"


def _project_hint() -> Optional[str]:
    board = os.environ.get("HERMES_KANBAN_BOARD")
    if board:
        return str(board)[:160]
    cwd = os.environ.get("TERMINAL_CWD")
    if cwd:
        try:
            return Path(cwd).expanduser().resolve().name[:160]
        except Exception:
            return Path(cwd).name[:160]
    return None


def _event_source(platform: Any = None) -> str:
    if os.environ.get("HERMES_CRON_JOB_ID") or str(platform or "").lower() == "cron":
        return "cron"
    if os.environ.get("HERMES_KANBAN_TASK_ID"):
        return "kanban"
    return "agent"


def _canonical_task(
    *,
    task_id: Any = None,
    session_id: Any = None,
    profile_name: Any = None,
    model: Any = None,
    platform: Any = None,
) -> str:
    store = _store()
    profile = _profile(profile_name)
    key = _context_key(profile=profile, task_id=task_id, session_id=session_id)
    mapped = store.resolve_context(key)
    if mapped:
        row = store.task_row(mapped)
        if row and row.get("status") == "running":
            store.ensure_task(mapped, profile=profile, model=str(model) if model else None)
            return mapped
        if task_id:
            # A raw task identifier should normally be unique.  If it is reused
            # after a completed turn, create a new generation rather than
            # rewriting immutable historical timing.
            mapped = None
        else:
            store.unbind_context(key)
            mapped = None

    source = _event_source(platform)
    if task_id:
        canonical = f"task:{profile}:{stable_hash(task_id)}"
        existing = store.task_row(canonical)
        if existing and existing.get("status") != "running":
            canonical = f"{canonical}:{int(time.time() * 1000)}"
    else:
        canonical = (
            f"turn:{profile}:{stable_hash(session_id or 'unknown')}:"
            f"{int(time.time() * 1000)}"
        )

    store.ensure_task(
        canonical,
        title="Hermes task",
        project=_project_hint(),
        profile=profile,
        model=str(model)[:160] if model else None,
        source=source,
        metadata={
            "platform": str(platform)[:80] if platform else None,
            "id_hash": stable_hash(task_id or session_id or canonical),
        },
    )
    store.bind_context(key, canonical, profile=profile, source=source)
    return canonical


def _status_from_result(result: Any) -> str:
    if isinstance(result, dict):
        if result.get("is_error") or result.get("error"):
            return "failed"
        success = result.get("success")
        if success is False:
            return "failed"
        if success is True:
            return "success"
    return "unknown"


def _on_pre_api_request(
    session_id: str = "",
    task_id: str = "",
    platform: str = "",
    model: str = "",
    provider: str = "",
    api_mode: str = "",
    api_call_count: Any = None,
    **_: Any,
) -> None:
    canonical = _canonical_task(
        task_id=task_id,
        session_id=session_id,
        model=model,
        platform=platform,
    )
    _store().mark(
        canonical,
        "api_request_started",
        metadata={
            "provider": str(provider)[:120] if provider else None,
            "api_mode": str(api_mode)[:80] if api_mode else None,
            "api_call_count": api_call_count if isinstance(api_call_count, (int, float)) else None,
        },
    )


def _on_post_api_request(
    session_id: str = "",
    task_id: str = "",
    platform: str = "",
    model: str = "",
    provider: str = "",
    api_mode: str = "",
    api_duration: Any = None,
    finish_reason: Any = None,
    usage: Any = None,
    response_model: Any = None,
    **_: Any,
) -> None:
    canonical = _canonical_task(
        task_id=task_id,
        session_id=session_id,
        model=model or response_model,
        platform=platform,
    )
    try:
        duration_ms = max(0.0, float(api_duration)) * 1000.0
    except (TypeError, ValueError):
        duration_ms = None

    safe_usage: Dict[str, Any] = {}
    if isinstance(usage, dict):
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                safe_usage[key] = value

    _store().record_observation(
        canonical,
        kind="llm_api",
        name=str(api_mode or "api")[:160],
        duration_ms=duration_ms,
        status="success",
        model=str(response_model or model)[:160] if (response_model or model) else None,
        provider=str(provider)[:120] if provider else None,
        metadata={
            "finish_reason": str(finish_reason)[:80] if finish_reason else None,
            "platform": str(platform)[:80] if platform else None,
            **safe_usage,
        },
    )


def _on_api_request_error(
    session_id: str = "",
    task_id: str = "",
    platform: str = "",
    model: str = "",
    provider: str = "",
    api_mode: str = "",
    api_duration: Any = None,
    error_type: Any = None,
    **_: Any,
) -> None:
    canonical = _canonical_task(
        task_id=task_id,
        session_id=session_id,
        model=model,
        platform=platform,
    )
    try:
        duration_ms = max(0.0, float(api_duration)) * 1000.0
    except (TypeError, ValueError):
        duration_ms = None
    _store().record_observation(
        canonical,
        kind="llm_api",
        name=str(api_mode or "api")[:160],
        duration_ms=duration_ms,
        status="failed",
        model=str(model)[:160] if model else None,
        provider=str(provider)[:120] if provider else None,
        metadata={
            "platform": str(platform)[:80] if platform else None,
            "error_class": str(error_type or "unknown")[:80],
        },
    )


def _on_post_tool_call(
    tool_name: str = "",
    function_name: str = "",
    result: Any = None,
    duration_ms: Any = None,
    status: Any = None,
    error_type: Any = None,
    task_id: str = "",
    session_id: str = "",
    model: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    canonical = _canonical_task(
        task_id=task_id,
        session_id=session_id,
        model=model,
        platform=platform,
    )
    explicit_status = str(status or "").strip().lower()
    safe_status = explicit_status if explicit_status else _status_from_result(result)
    _store().record_observation(
        canonical,
        kind="tool",
        name=str(tool_name or function_name or "unknown")[:160],
        duration_ms=duration_ms,
        status=safe_status,
        model=str(model)[:160] if model else None,
        metadata={
            "platform": str(platform)[:80] if platform else None,
            "error_class": str(error_type or "")[:80] or None,
        },
    )


def _on_pre_verify(
    session_id: str = "",
    task_id: str = "",
    platform: str = "",
    model: str = "",
    coding: Any = None,
    attempt: Any = 0,
    changed_paths: Any = None,
    **_: Any,
) -> None:
    canonical = _canonical_task(
        task_id=task_id,
        session_id=session_id,
        model=model,
        platform=platform,
    )
    try:
        attempt_number = int(attempt or 0)
    except (TypeError, ValueError):
        attempt_number = 0
    changed_count = len(changed_paths) if isinstance(changed_paths, (list, tuple)) else None
    kind = "rework" if attempt_number > 0 else "review"
    _store().record_observation(
        canonical,
        kind=kind,
        name="verification",
        duration_ms=0,
        status="started",
        model=str(model)[:160] if model else None,
        metadata={
            "attempt": attempt_number,
            "coding": bool(coding) if coding is not None else None,
            "changed_path_count": changed_count,
        },
    )


def _on_subagent_start(
    parent_session_id: str = "",
    session_id: str = "",
    task_id: str = "",
    child_role: Any = None,
    model: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    canonical = _canonical_task(
        task_id=task_id,
        session_id=parent_session_id or session_id,
        model=model,
        platform=platform,
    )
    _store().record_observation(
        canonical,
        kind="subagent",
        name=str(child_role or "child")[:160],
        duration_ms=0,
        status="started",
        metadata={},
    )


def _on_subagent_stop(
    parent_session_id: str = "",
    session_id: str = "",
    task_id: str = "",
    child_role: Any = None,
    child_status: Any = None,
    duration_ms: Any = None,
    model: str = "",
    platform: str = "",
    tool_call_history: Any = None,
    **_: Any,
) -> None:
    canonical = _canonical_task(
        task_id=task_id,
        session_id=parent_session_id or session_id,
        model=model,
        platform=platform,
    )
    tool_count = len(tool_call_history) if isinstance(tool_call_history, list) else None
    _store().record_observation(
        canonical,
        kind="subagent",
        name=str(child_role or "child")[:160],
        duration_ms=duration_ms,
        status=str(child_status or "unknown")[:80],
        metadata={"tool_call_count": tool_count},
    )


def _on_session_end(
    session_id: str = "",
    task_id: str = "",
    completed: bool = True,
    failed: bool = False,
    interrupted: bool = False,
    turn_exit_reason: Any = None,
    model: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    profile = _profile()
    key = _context_key(profile=profile, task_id=task_id, session_id=session_id)
    canonical = _store().resolve_context(key)
    if not canonical:
        canonical = _canonical_task(
            task_id=task_id,
            session_id=session_id,
            model=model,
            platform=platform,
        )
    row = _store().task_row(canonical) or {}
    _store().record_observation(
        canonical,
        kind="session",
        name="session_end",
        duration_ms=0,
        status="failed" if failed else ("cancelled" if interrupted else "success"),
        model=str(model)[:160] if model else None,
        metadata={
            "completed": bool(completed),
            "interrupted": bool(interrupted),
            "exit_reason": str(turn_exit_reason)[:120] if turn_exit_reason else None,
            "platform": str(platform)[:80] if platform else None,
        },
    )

    # Kanban lifecycle hooks are authoritative for board task completion.
    if row.get("source") == "kanban":
        return

    if failed:
        outcome = "failed"
    elif interrupted:
        outcome = "cancelled"
    elif completed:
        outcome = "success"
    else:
        outcome = "partial"
    _store().finish_task(canonical, outcome=outcome)
    if not task_id:
        _store().unbind_context(key)


def _on_kanban_claimed(
    task_id: str,
    profile_name: str = "",
    board: str = "default",
    assignee: Any = None,
    run_id: Any = None,
    **_: Any,
) -> None:
    # Claim hooks fire in the dispatcher process, so profile_name can be the
    # dispatcher profile. The assignee is the worker profile that will emit the
    # completion/block/tool hooks; prefer it to keep one shared task context.
    profile = _profile(assignee or profile_name)
    base = f"kanban:{stable_hash(board)}:{stable_hash(task_id)}"
    canonical = f"{base}:{stable_hash(run_id)}" if run_id else base
    store = _store()
    existing = store.task_row(canonical)
    if existing and existing.get("status") != "running":
        canonical = f"{base}:{stable_hash(run_id or time.time_ns())}"
    store.ensure_task(
        canonical,
        title="Kanban task",
        project=str(board or "default")[:160],
        profile=profile,
        source="kanban",
        metadata={
            "board": str(board or "default")[:160],
            "assignee": str(assignee)[:120] if assignee else None,
            "run_hash": stable_hash(run_id) if run_id else None,
            "task_hash": stable_hash(task_id),
        },
    )
    store.bind_context(
        _context_key(profile=profile, task_id=task_id),
        canonical,
        profile=profile,
        source="kanban",
    )
    store.record_observation(
        canonical,
        kind="kanban",
        name="claimed",
        duration_ms=0,
        status="started",
        profile=profile,
        metadata={"board": str(board or "default")[:160]},
    )


def _resolve_kanban_task(task_id: Any, profile_name: Any, board: Any) -> str:
    profile = _profile(profile_name)
    store = _store()
    mapped = store.resolve_context(_context_key(profile=profile, task_id=task_id))
    if mapped:
        return mapped
    canonical = f"kanban:{stable_hash(board)}:{stable_hash(task_id)}"
    store.ensure_task(
        canonical,
        title="Kanban task",
        project=str(board or "default")[:160],
        profile=profile,
        source="kanban",
        metadata={"task_hash": stable_hash(task_id)},
    )
    store.bind_context(
        _context_key(profile=profile, task_id=task_id),
        canonical,
        profile=profile,
        source="kanban",
    )
    return canonical


def _on_kanban_completed(
    task_id: str,
    profile_name: str = "",
    board: str = "default",
    assignee: Any = None,
    run_id: Any = None,
    **_: Any,
) -> None:
    canonical = _resolve_kanban_task(task_id, profile_name or assignee, board)
    _store().record_observation(
        canonical,
        kind="kanban",
        name="completed",
        duration_ms=0,
        status="success",
        profile=_profile(profile_name or assignee),
        metadata={"run_hash": stable_hash(run_id) if run_id else None},
    )
    _store().finish_task(canonical, outcome="success")


def _on_kanban_blocked(
    task_id: str,
    profile_name: str = "",
    board: str = "default",
    assignee: Any = None,
    run_id: Any = None,
    reason: Any = None,
    block_kind: Any = None,
    **_: Any,
) -> None:
    canonical = _resolve_kanban_task(task_id, profile_name or assignee, board)
    reason_class = str(block_kind or classify_block_reason(reason))[:80]
    _store().record_observation(
        canonical,
        kind="blocked",
        name=reason_class,
        duration_ms=0,
        status="partial",
        profile=_profile(profile_name or assignee),
        metadata={
            "reason_class": reason_class,
            "reason_hash": stable_hash(reason) if reason else None,
            "run_hash": stable_hash(run_id) if run_id else None,
        },
    )
    _store().finish_task(
        canonical,
        outcome="partial",
        metadata={"block_reason_class": reason_class},
    )


def _command(raw_args: str) -> str:
    args = (raw_args or "").strip().lower().split()
    if args and args[0] in {"help", "-h", "--help"}:
        return (
            "/task-metrics [7d|30d|status]\n"
            "Shows local task timing coverage and success metrics. "
            "No prompts or tool results are stored."
        )
    days = 30 if args and args[0] == "30d" else 7
    report = _store().recent_summary(days=days)
    success = report["success_rate"]
    success_text = "n/a" if success is None else f"{success * 100:.1f}%"
    return (
        f"Task metrics ({days}d)\n"
        f"- tasks: {report['task_count']}\n"
        f"- completed: {report['completed_count']}\n"
        f"- running: {report['running_count']}\n"
        f"- success rate: {success_text}\n"
        f"- database: {report['db']}"
    )


def _safe_register_hook(ctx: Any, name: str, callback: Any) -> None:
    try:
        ctx.register_hook(name, callback)
        _REGISTERED_HOOKS.append(name)
    except Exception:
        # Compatibility with Hermes releases that predate one of the optional
        # lifecycle hooks.  Core timing still loads through the hooks that the
        # installed runtime supports.
        return


def register(ctx: Any) -> None:
    hooks = {
        "pre_api_request": _on_pre_api_request,
        "post_api_request": _on_post_api_request,
        "api_request_error": _on_api_request_error,
        "post_tool_call": _on_post_tool_call,
        "pre_verify": _on_pre_verify,
        "on_session_end": _on_session_end,
        "subagent_start": _on_subagent_start,
        "subagent_stop": _on_subagent_stop,
        "kanban_task_claimed": _on_kanban_claimed,
        "kanban_task_completed": _on_kanban_completed,
        "kanban_task_blocked": _on_kanban_blocked,
    }
    for name, callback in hooks.items():
        _safe_register_hook(ctx, name, callback)

    try:
        ctx.register_command(
            "task-metrics",
            handler=_command,
            description="Show local task timing and delivery reliability metrics.",
        )
    except Exception:
        pass
