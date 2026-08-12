# Hermes local observability

This package industrialises the existing Hermes installation without adding a
hosted monitoring service, another model, or another agent.

It reuses the architecture already present on the VPS:

- Hermes plugin/lifecycle hooks;
- the existing multi-profile layout;
- the shared Kanban board;
- per-profile `state.db` session ledgers;
- per-profile cron `jobs.json` and `executions.db`;
- the existing task-time SQLite database;
- GitHub CLI and Vercel credentials only when already available;
- Supabase URL/anon credentials only when already present;
- no-agent Hermes cron jobs for collection, alerts and reporting.

## Installed runtime paths

```text
/opt/data/metrics/task_time.sqlite3
/opt/data/metrics/current.json
/opt/data/metrics/current.md
/opt/data/metrics/dashboard.html
/opt/data/metrics/latest_alert.txt
/opt/data/metrics/snapshots.jsonl
/opt/data/metrics/observability_config.json
/opt/data/backups/observability/
```

The automatic plugin is installed per profile under:

```text
<profile-home>/plugins/task-metrics/
```

The timing skill remains per profile under:

```text
<profile-home>/skills/productivity/task-time-metrics/
```

The collector and no-agent wrappers are installed under:

```text
/opt/data/scripts/ops_observability.py
/opt/data/scripts/ops_snapshot_cron.py
/opt/data/scripts/ops_daily_alert_cron.py
/opt/data/scripts/ops_weekly_report_cron.py
/opt/data/scripts/hermes_observability/
```

## Automatic measurements

- task wall time and outcome;
- success rate and first-pass success rate;
- rework count/share and blocked-task count;
- median and p90 task duration;
- active/wall efficiency;
- model/provider/API duration and token counters;
- tool duration and coarse success state;
- verification/rework attempts;
- subagent duration;
- Kanban claim/completion/blocking;
- cron success/failure/unknown/running state and ticker heartbeat;
- session count, tokens and locally recorded cost;
- GitHub Actions and Vercel state when existing credentials are available;
- Supabase Auth health latency when an existing URL is discoverable;
- disk, memory and load.

The plugin does **not** store prompts, model responses, tool arguments/results,
raw errors, message bodies, phone numbers or credentials. Task/session IDs are
hashed before persistence. Supabase URLs and anon keys are never written into
reports.

## Free operation

The three recurring jobs use Hermes no-agent mode:

- snapshot every 15 minutes;
- daily alert, silent when healthy;
- weekly KPI report.

They make no LLM calls and consume no model tokens. Local SQLite, JSON,
Markdown and HTML have no additional service charge. Existing GitHub, Vercel
and Supabase plan limits still apply to those services; the collector does not
create a paid monitoring account or a hosted database.

## Health policy

Default signals include:

- disk/memory saturation;
- task success, first-pass success and rework share;
- stale running tasks;
- cron failures, unknown attempts, overdue jobs and stale ticker heartbeat;
- blocked Kanban tasks;
- failed GitHub workflows;
- failed Vercel deployments;
- unavailable or abnormally slow Supabase Auth health checks;
- missing/failed backup integrity or restore tests.

Thresholds can be changed in `observability_config.json` without modifying the
collector.

## Validation

```bash
python /opt/data/scripts/ops_observability.py doctor
python /opt/data/scripts/ops_observability.py snapshot --since 7d
python /opt/data/scripts/ops_observability.py report --since 7d
python /opt/data/skills/productivity/task-time-metrics/scripts/task_time_tracker.py \
  --db /opt/data/metrics/task_time.sqlite3 doctor --stale-after-hours 24
```

Hermes also exposes a compact plugin command in new sessions:

```text
/task-metrics
```

## Backup/restore evidence

The daily no-agent job creates online SQLite backups and runs
`PRAGMA integrity_check` on each backup. It then copies each backup to a fresh
temporary restore location and runs the integrity check again. File copies are
verified with SHA-256.

Backup manifests are retained under `/opt/data/backups/observability/` and
record both `all_integrity_ok` and `all_restore_test_ok`. `.env` files and
credentials are explicitly excluded.

The installer also creates a pre-install snapshot under:

```text
/opt/data/backups/observability-install-<UTC timestamp>/
```

and writes an executable `rollback.sh` there. The rollback restores the
previous plugin, skill, scripts and profile configuration. It is intended for
an immediate rollback after installation validation, before unrelated config or
cron changes are made.

## Scope boundary

This layer measures and diagnoses the existing platform. It does not:

- add an AI provider or routing layer;
- change Hermes models, prompts or Kanban semantics;
- modify Supabase schemas or RLS policies;
- deploy or promote Vercel projects;
- replace existing GitHub Actions workflows.
