from __future__ import annotations

import html
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .common import VERSION, discover_profiles, human_bytes, human_duration, iso, now_utc, pct, read_json
from .cron_kanban import collect_cron, collect_kanban
from .integrations import collect_github, collect_vercel
from .system_tasks import aggregate_sessions, collect_session_metrics, collect_system, collect_task_metrics
from .supabase import collect_supabase


def collect_backup_status(root: Path) -> Dict[str, Any]:
    parent = root / "backups" / "observability"
    manifests = (
        sorted(parent.glob("*/manifest.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        if parent.is_dir()
        else []
    )
    if not manifests:
        return {"available": False, "reason": "missing"}
    path = manifests[0]
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        return {"available": False, "reason": "invalid_manifest"}
    created = None
    try:
        created = datetime.fromisoformat(str(payload.get("created_at") or "").replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=now_utc().tzinfo)
    except ValueError:
        created = None
    age = max(0.0, (now_utc() - created).total_seconds()) if created else None
    return {
        "available": True,
        "manifest": str(path),
        "created_at": payload.get("created_at"),
        "age_seconds": round(age, 1) if age is not None else None,
        "record_count": int(payload.get("record_count") or 0),
        "all_integrity_ok": bool(payload.get("all_integrity_ok")),
        "all_restore_test_ok": bool(payload.get("all_restore_test_ok")),
        "secrets_excluded": bool(payload.get("secrets_excluded")),
    }


def evaluate_health(snapshot: Dict[str, Any], config: Mapping[str, Any], *, alert_cutoff: datetime) -> Dict[str, Any]:
    thresholds = config.get("thresholds", {})
    issues: List[Dict[str, str]] = []

    def add(severity: str, code: str, message: str) -> None:
        issues.append({"severity": severity, "code": code, "message": message})

    system = snapshot.get("system", {})
    disk_pct = (system.get("disk") or {}).get("used_pct")
    if disk_pct is not None:
        if disk_pct >= float(thresholds.get("disk_critical_pct", 90)):
            add("critical", "disk_critical", f"Disque utilisé à {disk_pct:.1f}%")
        elif disk_pct >= float(thresholds.get("disk_warning_pct", 80)):
            add("warning", "disk_warning", f"Disque utilisé à {disk_pct:.1f}%")
    memory_pct = (system.get("cgroup_memory") or {}).get("used_pct")
    if memory_pct is None:
        memory_pct = (system.get("memory") or {}).get("used_pct")
    if memory_pct is not None:
        if memory_pct >= float(thresholds.get("memory_critical_pct", 92)):
            add("critical", "memory_critical", f"Mémoire utilisée à {memory_pct:.1f}%")
        elif memory_pct >= float(thresholds.get("memory_warning_pct", 85)):
            add("warning", "memory_warning", f"Mémoire utilisée à {memory_pct:.1f}%")

    tasks = snapshot.get("tasks", {})
    if tasks.get("available"):
        completed = int(tasks.get("completed_count") or 0)
        success_rate = tasks.get("success_rate")
        min_sample = int(thresholds.get("task_min_sample", 5))
        if success_rate is not None and completed >= min_sample:
            if success_rate < float(thresholds.get("task_success_critical", 0.70)):
                add("critical", "task_success_critical", f"Taux de réussite des tâches {success_rate * 100:.1f}%")
            elif success_rate < float(thresholds.get("task_success_warning", 0.85)):
                add("warning", "task_success_warning", f"Taux de réussite des tâches {success_rate * 100:.1f}%")
        first_pass_rate = tasks.get("first_pass_success_rate")
        if first_pass_rate is not None and completed >= min_sample:
            if first_pass_rate < float(thresholds.get("first_pass_success_critical", 0.50)):
                add("critical", "first_pass_success_critical", f"Réussite au premier passage {first_pass_rate * 100:.1f}%")
            elif first_pass_rate < float(thresholds.get("first_pass_success_warning", 0.70)):
                add("warning", "first_pass_success_warning", f"Réussite au premier passage {first_pass_rate * 100:.1f}%")
        rework_share = tasks.get("rework_share")
        if rework_share is not None and completed >= 3 and rework_share > float(thresholds.get("rework_share_warning", 0.30)):
            add("warning", "rework_high", f"Part de rework élevée: {rework_share * 100:.1f}%")
        warning_age = float(thresholds.get("stale_task_warning_hours", 6)) * 3600
        critical_age = float(thresholds.get("stale_task_critical_hours", 24)) * 3600
        for item in tasks.get("stale_running", []):
            age = float(item.get("age_seconds") or 0)
            if age >= critical_age:
                add("critical", "task_stale_critical", f"Tâche {item.get('profile') or '?'} ouverte depuis {human_duration(age)}")
            elif age >= warning_age:
                add("warning", "task_stale_warning", f"Tâche {item.get('profile') or '?'} ouverte depuis {human_duration(age)}")

    cron_items = snapshot.get("cron", [])
    hb_warn = float(thresholds.get("cron_heartbeat_warning_seconds", 180))
    hb_crit = float(thresholds.get("cron_heartbeat_critical_seconds", 600))
    stale_run = float(thresholds.get("cron_running_stale_hours", 2)) * 3600
    for cron in cron_items:
        profile = cron.get("profile") or "default"
        if int(cron.get("failure_count") or 0) > 0:
            add("critical", "cron_failure", f"{cron['failure_count']} exécution(s) cron en échec dans {profile}")
        if int(cron.get("unknown_count") or 0) > 0:
            add("warning", "cron_unknown", f"{cron['unknown_count']} exécution(s) cron à état inconnu dans {profile}")
        if int(cron.get("overdue_count") or 0) > 0:
            add("warning", "cron_overdue", f"{cron['overdue_count']} cron(s) en retard dans {profile}")
        hb = cron.get("ticker_heartbeat_age_seconds")
        if cron.get("enabled_job_count", 0):
            if hb is None:
                add("warning", "cron_heartbeat_missing", f"Heartbeat du ticker cron absent dans {profile}")
            elif hb >= hb_crit:
                add("critical", "cron_heartbeat_critical", f"Ticker cron {profile} silencieux depuis {human_duration(hb)}")
            elif hb >= hb_warn:
                add("warning", "cron_heartbeat_warning", f"Ticker cron {profile} silencieux depuis {human_duration(hb)}")
        for item in cron.get("stale_running", []):
            if float(item.get("age_seconds") or 0) >= stale_run:
                add("critical", "cron_stale_running", f"Cron {profile}/{item.get('name') or '?'} actif depuis {human_duration(item.get('age_seconds'))}")

    kanban = snapshot.get("kanban", {})
    blocked = int(kanban.get("blocked_count") or 0)
    if blocked >= int(thresholds.get("kanban_blocked_critical", 8)):
        add("critical", "kanban_blocked_critical", f"{blocked} tâches Kanban bloquées")
    elif blocked >= int(thresholds.get("kanban_blocked_warning", 1)):
        add("warning", "kanban_blocked_warning", f"{blocked} tâche(s) Kanban bloquée(s)")

    github = snapshot.get("github", {})
    if int(github.get("failure_count") or 0) > 0:
        add("critical", "github_ci_failure", f"{github['failure_count']} workflow(s) GitHub Actions en échec")
    vercel = snapshot.get("vercel", {})
    if int(vercel.get("error_count") or 0) > 0:
        add("critical", "vercel_failure", f"{vercel['error_count']} déploiement(s) Vercel en erreur")

    supabase = snapshot.get("supabase", {})
    if supabase.get("configured"):
        if not supabase.get("available"):
            add("warning", "supabase_unavailable", "Santé Supabase non joignable depuis Hermes")
        latency_warn = float(thresholds.get("supabase_latency_warning_ms", 1500))
        latency_crit = float(thresholds.get("supabase_latency_critical_ms", 5000))
        for project in supabase.get("projects", []):
            if not project.get("available"):
                add("warning", "supabase_project_unavailable", f"Supabase {project.get('name') or '?'} indisponible")
                continue
            latency = project.get("latency_ms")
            if latency is None:
                continue
            if float(latency) >= latency_crit:
                add("critical", "supabase_latency_critical", f"Supabase {project.get('name') or '?'} répond en {float(latency):.0f} ms")
            elif float(latency) >= latency_warn:
                add("warning", "supabase_latency_warning", f"Supabase {project.get('name') or '?'} répond en {float(latency):.0f} ms")

    backup = snapshot.get("backup", {})
    if not backup.get("available"):
        add("warning", "backup_missing", "Aucune sauvegarde observabilité vérifiée disponible")
    elif not backup.get("all_integrity_ok") or not backup.get("all_restore_test_ok"):
        add("critical", "backup_restore_failed", "Dernière sauvegarde: intégrité ou test de restauration en échec")
    else:
        age = backup.get("age_seconds")
        if age is not None:
            warning_age = float(thresholds.get("backup_warning_hours", 36)) * 3600
            critical_age = float(thresholds.get("backup_critical_hours", 72)) * 3600
            if float(age) >= critical_age:
                add("critical", "backup_stale_critical", f"Dernière sauvegarde vérifiée il y a {human_duration(age)}")
            elif float(age) >= warning_age:
                add("warning", "backup_stale_warning", f"Dernière sauvegarde vérifiée il y a {human_duration(age)}")

    rank = {"healthy": 0, "warning": 1, "critical": 2}
    status = "healthy"
    for issue in issues:
        if rank[issue["severity"]] > rank[status]:
            status = issue["severity"]
    counts = Counter(issue["severity"] for issue in issues)
    return {
        "status": status,
        "critical_count": counts["critical"],
        "warning_count": counts["warning"],
        "issues": issues,
    }


def build_snapshot(root: Path, config: Mapping[str, Any], *, window: timedelta) -> Dict[str, Any]:
    generated = now_utc()
    cutoff = generated - window
    profiles = discover_profiles(root)
    sessions = [collect_session_metrics(name, home, cutoff) for name, home in profiles]
    cron = [collect_cron(name, home, cutoff) for name, home in profiles]
    snapshot: Dict[str, Any] = {
        "schema_version": 1,
        "collector_version": VERSION,
        "generated_at": iso(generated),
        "window_seconds": int(window.total_seconds()),
        "window_start": iso(cutoff),
        "root": str(root),
        "system": collect_system(root),
        "tasks": collect_task_metrics(root / "metrics" / "task_time.sqlite3", cutoff),
        "profiles": [{"name": name, "home": str(home)} for name, home in profiles],
        "sessions": sessions,
        "sessions_total": aggregate_sessions(sessions),
        "cron": cron,
        "kanban": collect_kanban(root, cutoff),
        "github": collect_github(config, cutoff),
        "vercel": collect_vercel(root, config, cutoff),
        "supabase": collect_supabase(root, config),
        "backup": collect_backup_status(root),
    }
    snapshot["health"] = evaluate_health(snapshot, config, alert_cutoff=cutoff)
    return snapshot


def render_alert(snapshot: Mapping[str, Any]) -> str:
    health = snapshot.get("health", {})
    issues = health.get("issues", []) if isinstance(health, dict) else []
    actionable = [item for item in issues if item.get("severity") in {"critical", "warning"}]
    if not actionable:
        return ""
    lines = [f"Observabilité Hermes — {str(health.get('status', 'warning')).upper()}"]
    for item in actionable[:12]:
        marker = "CRITIQUE" if item.get("severity") == "critical" else "ALERTE"
        lines.append(f"- {marker}: {item.get('message')}")
    if len(actionable) > 12:
        lines.append(f"- +{len(actionable) - 12} autre(s) signal(aux)")
    lines.append(f"Dashboard local: {snapshot.get('root')}/metrics/dashboard.html")
    return "\n".join(lines) + "\n"


def render_markdown(snapshot: Mapping[str, Any]) -> str:
    health = snapshot.get("health", {})
    system = snapshot.get("system", {})
    tasks = snapshot.get("tasks", {})
    sessions = snapshot.get("sessions_total", {})
    kanban = snapshot.get("kanban", {})
    github = snapshot.get("github", {})
    vercel = snapshot.get("vercel", {})
    supabase = snapshot.get("supabase", {})
    backup = snapshot.get("backup", {})
    lines = [
        "# Rapport d’observabilité Hermes",
        "",
        f"Généré : `{snapshot.get('generated_at')}`  ",
        f"Fenêtre : {human_duration(snapshot.get('window_seconds'))}  ",
        f"État global : **{str(health.get('status', 'unknown')).upper()}**",
        "",
        "## Synthèse",
        "",
        f"- Disque : {(system.get('disk') or {}).get('used_pct', 'n/a')}% ({human_bytes((system.get('disk') or {}).get('free_bytes'))} libres)",
        f"- Mémoire : {(system.get('cgroup_memory') or {}).get('used_pct') if (system.get('cgroup_memory') or {}).get('used_pct') is not None else (system.get('memory') or {}).get('used_pct', 'n/a')}%",
        f"- Tâches mesurées : {tasks.get('task_count', 0)} ; réussite : {pct(tasks.get('success_rate'))} ; premier passage : {pct(tasks.get('first_pass_success_rate'))}",
        f"- Sessions : {sessions.get('session_count', 0)} ; appels API : {sessions.get('api_call_count', 0)}",
        f"- Coût estimé observé : ${float(sessions.get('estimated_cost_usd') or 0):.4f}",
        f"- Kanban : {kanban.get('running_count', 0)} en cours, {kanban.get('blocked_count', 0)} bloquée(s), {kanban.get('review_count', 0)} en revue",
        f"- GitHub Actions : {github.get('failure_count', 0)} échec(s), disponibilité={github.get('available', False)}",
        f"- Vercel : {vercel.get('error_count', 0)} erreur(s), disponibilité={vercel.get('available', False)}",
        f"- Supabase : disponibilité={supabase.get('available', False)} ; projet(s) indisponible(s)={supabase.get('unavailable_count', 0)}",
        f"- Sauvegarde : intégrité={backup.get('all_integrity_ok', False)} ; restauration={backup.get('all_restore_test_ok', False)} ; âge={human_duration(backup.get('age_seconds'))}",
        "",
        "## Fiabilité des tâches",
        "",
        f"- Terminées : {tasks.get('completed_count', 0)}",
        f"- Succès : {tasks.get('success_count', 0)}",
        f"- Échecs : {tasks.get('failure_count', 0)}",
        f"- Partielles : {tasks.get('partial_count', 0)}",
        f"- Médiane murale : {human_duration(tasks.get('median_wall_seconds'))}",
        f"- P90 mural : {human_duration(tasks.get('p90_wall_seconds'))}",
        f"- Efficacité active/mur : {pct(tasks.get('overall_efficiency_ratio'))}",
        f"- Part de rework : {pct(tasks.get('rework_share'))}",
        f"- Réussite au premier passage : {pct(tasks.get('first_pass_success_rate'))}",
        "",
        "## Crons",
        "",
        "| Profil | Actifs | No-agent | Échecs | Inconnus | En retard | Heartbeat |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in snapshot.get("cron", []):
        lines.append(
            f"| {item.get('profile')} | {item.get('enabled_job_count', 0)} | {item.get('no_agent_job_count', 0)} | "
            f"{item.get('failure_count', 0)} | {item.get('unknown_count', 0)} | {item.get('overdue_count', 0)} | "
            f"{human_duration(item.get('ticker_heartbeat_age_seconds'))} |"
        )
    lines.extend(["", "## Profils et modèles", ""])
    for name, count in (tasks.get("profile_breakdown") or {}).items():
        lines.append(f"- `{name}` : {count} tâche(s)")
    if not (tasks.get("profile_breakdown") or {}):
        lines.append("- Pas encore assez de tâches automatiquement instrumentées.")
    profile_kpis = tasks.get("profile_kpis") or {}
    if profile_kpis:
        lines.extend([
            "",
            "| Profil | Tâches | Réussite | Premier passage | Médiane | P90 | Efficacité |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ])
        for name, values in profile_kpis.items():
            lines.append(
                f"| {name} | {values.get('task_count', 0)} | {pct(values.get('success_rate'))} | "
                f"{pct(values.get('first_pass_success_rate'))} | {human_duration(values.get('median_wall_seconds'))} | "
                f"{human_duration(values.get('p90_wall_seconds'))} | {pct(values.get('efficiency_ratio'))} |"
            )
    lines.extend(["", "## Alertes", ""])
    issues = health.get("issues", []) if isinstance(health, dict) else []
    if not issues:
        lines.append("- Aucune alerte active.")
    else:
        for item in issues:
            lines.append(f"- **{str(item.get('severity')).upper()}** `{item.get('code')}` — {item.get('message')}")
    lines.append("")
    return "\n".join(lines)


def render_html(snapshot: Mapping[str, Any]) -> str:
    health = snapshot.get("health", {})
    system = snapshot.get("system", {})
    tasks = snapshot.get("tasks", {})
    sessions = snapshot.get("sessions_total", {})
    kanban = snapshot.get("kanban", {})
    supabase = snapshot.get("supabase", {})
    backup = snapshot.get("backup", {})
    status = str(health.get("status", "unknown"))
    cards = [
        ("État", status.upper()),
        ("Tâches", str(tasks.get("task_count", 0))),
        ("Réussite", pct(tasks.get("success_rate"))),
        ("1er passage", pct(tasks.get("first_pass_success_rate"))),
        ("Rework", pct(tasks.get("rework_share"))),
        ("Sessions", str(sessions.get("session_count", 0))),
        ("Coût estimé", f"${float(sessions.get('estimated_cost_usd') or 0):.4f}"),
        ("Disque", f"{(system.get('disk') or {}).get('used_pct', 'n/a')}%"),
        ("Kanban bloqué", str(kanban.get("blocked_count", 0))),
        ("Supabase", "OK" if supabase.get("available") else "N/A"),
        ("Backup", "OK" if backup.get("all_integrity_ok") and backup.get("all_restore_test_ok") else "ALERTE"),
    ]
    card_html = "".join(
        f'<section class="card"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></section>'
        for label, value in cards
    )
    issue_html = "".join(
        f'<li class="{html.escape(str(item.get("severity")))}"><b>{html.escape(str(item.get("severity")).upper())}</b> {html.escape(str(item.get("message")))}</li>'
        for item in health.get("issues", [])
    ) or "<li>Aucune alerte active.</li>"
    cron_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('profile')))}</td>"
        f"<td>{item.get('enabled_job_count', 0)}</td>"
        f"<td>{item.get('no_agent_job_count', 0)}</td>"
        f"<td>{item.get('failure_count', 0)}</td>"
        f"<td>{item.get('unknown_count', 0)}</td>"
        f"<td>{item.get('overdue_count', 0)}</td>"
        f"<td>{html.escape(human_duration(item.get('ticker_heartbeat_age_seconds')))}</td>"
        "</tr>"
        for item in snapshot.get("cron", [])
    )
    profile_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('profile')))}</td>"
        f"<td>{item.get('session_count', 0)}</td>"
        f"<td>{item.get('api_call_count', 0)}</td>"
        f"<td>{item.get('tool_call_count', 0)}</td>"
        f"<td>{int(item.get('input_tokens') or 0) + int(item.get('output_tokens') or 0):,}</td>"
        f"<td>${float(item.get('estimated_cost_usd') or 0):.4f}</td>"
        "</tr>"
        for item in snapshot.get("sessions", []) if item.get("available")
    )
    task_profile_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(name))}</td>"
        f"<td>{values.get('task_count', 0)}</td>"
        f"<td>{html.escape(pct(values.get('success_rate')))}</td>"
        f"<td>{html.escape(pct(values.get('first_pass_success_rate')))}</td>"
        f"<td>{html.escape(human_duration(values.get('median_wall_seconds')))}</td>"
        f"<td>{html.escape(human_duration(values.get('p90_wall_seconds')))}</td>"
        f"<td>{html.escape(pct(values.get('efficiency_ratio')))}</td>"
        "</tr>"
        for name, values in (tasks.get("profile_kpis") or {}).items()
    )
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><title>Hermes — Observabilité</title>
<style>
:root{{--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--muted:#8b949e;--ok:#3fb950;--warn:#d29922;--bad:#f85149}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:1180px;margin:auto;padding:28px}} h1{{margin:0}} .sub{{color:var(--muted);margin:6px 0 24px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px}} .card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:15px}}
.card span{{display:block;color:var(--muted);font-size:12px;text-transform:uppercase}} .card strong{{display:block;font-size:25px;margin-top:6px}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px;margin-top:16px;overflow:auto}} table{{width:100%;border-collapse:collapse}} th,td{{padding:9px;border-bottom:1px solid var(--line);text-align:left}} th{{color:var(--muted)}}
ul{{padding-left:22px}} li{{margin:7px 0}} li.critical{{color:var(--bad)}} li.warning{{color:var(--warn)}} .healthy{{color:var(--ok)}} .warning{{color:var(--warn)}} .critical{{color:var(--bad)}}
footer{{color:var(--muted);margin-top:20px;font-size:12px}}
</style></head><body><main>
<h1>Observabilité Hermes</h1><p class="sub">Généré {html.escape(str(snapshot.get('generated_at')))} · fenêtre {human_duration(snapshot.get('window_seconds'))} · <b class="{html.escape(status)}">{html.escape(status.upper())}</b></p>
<div class="grid">{card_html}</div>
<section class="panel"><h2>Alertes</h2><ul>{issue_html}</ul></section>
<section class="panel"><h2>Crons</h2><table><thead><tr><th>Profil</th><th>Actifs</th><th>No-agent</th><th>Échecs</th><th>Inconnus</th><th>Retard</th><th>Heartbeat</th></tr></thead><tbody>{cron_rows}</tbody></table></section>
<section class="panel"><h2>Sessions</h2><table><thead><tr><th>Profil</th><th>Sessions</th><th>API</th><th>Outils</th><th>Tokens</th><th>Coût estimé</th></tr></thead><tbody>{profile_rows}</tbody></table></section>
<section class="panel"><h2>Fiabilité mesurée</h2><p>Médiane: {human_duration(tasks.get('median_wall_seconds'))} · P90: {human_duration(tasks.get('p90_wall_seconds'))} · réussite premier passage: {pct(tasks.get('first_pass_success_rate'))} · efficacité active/mur: {pct(tasks.get('overall_efficiency_ratio'))}</p><table><thead><tr><th>Profil</th><th>Tâches</th><th>Réussite</th><th>1er passage</th><th>Médiane</th><th>P90</th><th>Efficacité</th></tr></thead><tbody>{task_profile_rows}</tbody></table><p>Sources: {html.escape(json.dumps(tasks.get('source_breakdown') or {}, ensure_ascii=False))}</p></section>
<footer>Données locales uniquement. Aucun prompt, résultat d’outil, secret ou contenu de message n’est exporté.</footer>
</main></body></html>"""
