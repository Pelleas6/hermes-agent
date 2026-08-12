---
name: task-time-metrics
description: Measure task time, waits, rework, and delivery efficiency.
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [metrics, productivity, observability, timing]
    category: productivity
    requires_toolsets: [terminal]
---

# Task Time Metrics

Measure real delivery time instead of estimating it from conversation length.

## When to Use

Use this skill automatically for every non-trivial task that is likely to take more than about one minute, and for every code, infrastructure, CI, deployment, audit, debugging, or multi-step task.

Also use it when the user asks how long work takes, which profiles are slow, where time is lost, how much rework occurs, or how delivery performance changes over time.

Do not create timing records for trivial one-shot answers.

## What It Measures

The tracker stores task and phase timestamps in SQLite and separates:

- `agent_work` — implementation, analysis, coding, configuration;
- `review` — verification, QA, code review;
- `rework` — fixing a failed check or regression;
- `tool_wait` — waiting for a local/remote tool response;
- `ci_wait` — GitHub Actions or other CI execution;
- `deploy_wait` — deployment/build propagation;
- `external_wait` — third-party service or external dependency;
- `user_wait` — explicit human input/approval wait;
- `blocked` — task cannot progress because a prerequisite is unavailable.

This distinction matters: wall-clock time is not the same as active engineering time.

## Storage

The script uses only the Python standard library.

Default database resolution:

1. `$HERMES_TASK_TIME_DB` when explicitly set;
2. `/opt/data/metrics/task_time.sqlite3` when `/opt/data` is writable;
3. `$HERMES_HOME/metrics/task_time.sqlite3`;
4. otherwise `~/.hermes/metrics/task_time.sqlite3`.

SQLite WAL mode is enabled so multiple profiles can record concurrently.

## Resolve the Tracker

Prefer:

```bash
TRACKER="${HERMES_HOME:-$HOME/.hermes}/skills/productivity/task-time-metrics/scripts/task_time_tracker.py"
```

If that path does not exist because the skill was installed from another external directory, locate the loaded skill package once and reuse its `scripts/task_time_tracker.py` path for the session.

## Start a Task

Start timing before substantial execution, not after the work is already underway.

If a Kanban card, issue, PR, cron run, or other stable task identifier exists, reuse it as `--task-id`. Otherwise let the script generate a UUID and retain the returned ID for the whole task.

```bash
python "$TRACKER" start \
  --task-id "<stable-id-if-available>" \
  --title "<short task title>" \
  --project "<project/repository>" \
  --profile "<active Hermes profile>" \
  --model "<active model when known>" \
  --source "hermes"
```

The command returns JSON containing `task_id`, `started_at`, and the database path.

Do not invent model/profile values when they are unknown; omit them.

## Switch Phase When Work State Changes

Close the current phase and open another one whenever the dominant task state changes.

Examples:

```bash
python "$TRACKER" switch "$TASK_ID" --kind review --label "verification loop"
python "$TRACKER" switch "$TASK_ID" --kind ci_wait --label "GitHub Actions"
python "$TRACKER" switch "$TASK_ID" --kind rework --label "fix failed lint"
python "$TRACKER" switch "$TASK_ID" --kind deploy_wait --label "Vercel deployment"
python "$TRACKER" switch "$TASK_ID" --kind agent_work --label "resume implementation"
```

Do not create noisy sub-second phase changes. Switch only when the dominant state genuinely changes.

## Record Milestones

Use marks for meaningful instantaneous events without ending the current phase:

```bash
python "$TRACKER" mark "$TASK_ID" --name pr_opened --meta pr=142
python "$TRACKER" mark "$TASK_ID" --name ci_started --meta run_id=31587618092
python "$TRACKER" mark "$TASK_ID" --name deployment_ready --meta provider=vercel
```

Metadata accepts repeated `--meta key=value`.

Never put secrets, tokens, passwords, private message bodies, phone numbers, or personal credentials in metadata.

## Finish a Task

Before the final completion message, finish the tracker with the real outcome:

```bash
python "$TRACKER" finish "$TASK_ID" --outcome success
```

Allowed outcomes:

- `success`;
- `partial`;
- `failed`;
- `cancelled`.

A task with a genuine unresolved blocker is `partial` or `failed`, not `success`.

Finishing automatically closes the open timing segment and prints the task summary.

## Reports

Last seven days:

```bash
python "$TRACKER" report --since 7d
```

Filter by project or profile:

```bash
python "$TRACKER" report --since 30d --project "Pelleas6/site_IA_Pour_Nul"
python "$TRACKER" report --since 30d --profile dev_palm
```

Include individual tasks:

```bash
python "$TRACKER" report --since 7d --include-tasks
```

The report provides:

- task count;
- completed count;
- success rate;
- total wall time;
- total active time;
- active/wall efficiency ratio;
- median task duration;
- p90 task duration;
- median active duration;
- time split by phase type;
- outcomes.

## Export for Analytics

CSV:

```bash
python "$TRACKER" export --since 30d \
  --format csv \
  --output /opt/data/metrics/task_time_30d.csv
```

JSON:

```bash
python "$TRACKER" export --since 30d \
  --format json \
  --output /opt/data/metrics/task_time_30d.json
```

These exports are intended for dashboards, KPI analysis, or later ingestion into Supabase.

## Interpretation Rules

Never report wall-clock duration as pure agent effort.

Use these definitions:

- **Wall time** = task finish minus task start.
- **Active time** = `agent_work + review + rework`.
- **Wait time** = tool/CI/deploy/external/user/blocking phases.
- **Efficiency ratio** = active time / wall time.
- **Rework share** = rework time / active time.

When comparing profiles or models, compare similar task categories and sufficient sample sizes. Do not conclude that one model is faster from one heterogeneous task.

## Historical Data

Existing GitHub PR, commit, workflow, and deployment timestamps can be used to reconstruct approximate historical wall time. Mark reconstructed records as historical/approximate in downstream analytics; do not present them as equal in precision to tasks recorded live by this skill.

GitHub Actions job and step timestamps are especially useful for separating CI wait from active agent time.

## Failure and Recovery

If the agent/process crashes, the task remains `running` with an open segment. On the next session:

```bash
python "$TRACKER" show "$TASK_ID"
```

Then either resume by switching to the appropriate phase or finish with the real outcome.

Do not delete timing records merely because a task failed. Failed/reworked tasks are important performance data.

## Completion Requirement

For a tracked task, do not send the final delivery message until:

1. the task outcome is known;
2. the current timing segment reflects the actual final phase;
3. `finish` has been executed;
4. the returned timing summary is retained for future metrics.

The tracker measures the process; it must never delay or replace the actual technical verification required by the task.
