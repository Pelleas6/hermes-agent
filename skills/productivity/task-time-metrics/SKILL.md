---
name: task-time-metrics
description: Measure task time, waits, rework, and delivery efficiency.
version: 2.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [metrics, productivity, observability, timing]
    category: productivity
    requires_toolsets: [terminal]
---

# Task Time Metrics

Measure delivery performance from real timestamps rather than conversation
length or subjective estimates.

## Automatic mode

When the companion `task-metrics` plugin is enabled, Hermes automatically
records:

- task/session start and outcome;
- LLM API duration, model, provider and token counters;
- tool duration and coarse success state;
- verification and rework attempts;
- subagent duration;
- Kanban claim, completion and blocking.

Automatic mode stores no prompt, response, tool arguments/results, raw error,
phone number or credential. All profiles write to the shared local database:

```text
/opt/data/metrics/task_time.sqlite3
```

No agent action is required for ordinary task timing.

## Manual mode

Use manual phases only when the automatic hooks cannot distinguish a meaningful
state such as CI waiting, deployment waiting, user waiting, or explicit review.
Do not add noisy phase changes for every tool call; the plugin already measures
tools and APIs.

Resolve the tracker:

```bash
TRACKER="${HERMES_HOME:-$HOME/.hermes}/skills/productivity/task-time-metrics/scripts/task_time_tracker.py"
DB="${HERMES_TASK_TIME_DB:-/opt/data/metrics/task_time.sqlite3}"
```

Start a manually tracked task:

```bash
python "$TRACKER" --db "$DB" start \
  --title "<short title>" \
  --project "<project>" \
  --profile "<profile>" \
  --model "<model when known>"
```

Retain the returned `task_id`.

Switch only when the dominant state changes:

```bash
python "$TRACKER" --db "$DB" switch "$TASK_ID" --kind ci_wait --label "GitHub Actions"
python "$TRACKER" --db "$DB" switch "$TASK_ID" --kind rework --label "fix failed lint"
python "$TRACKER" --db "$DB" switch "$TASK_ID" --kind deploy_wait --label "Vercel"
python "$TRACKER" --db "$DB" switch "$TASK_ID" --kind agent_work --label "resume"
```

Supported manual kinds:

- `agent_work`
- `review`
- `rework`
- `tool_wait`
- `ci_wait`
- `deploy_wait`
- `external_wait`
- `user_wait`
- `blocked`

Finish with the real outcome:

```bash
python "$TRACKER" --db "$DB" finish "$TASK_ID" --outcome success
```

Outcomes: `success`, `partial`, `failed`, `cancelled`.

## Reports

```bash
python "$TRACKER" --db "$DB" report --since 7d
python "$TRACKER" --db "$DB" report --since 30d --profile dev_palm --include-tasks
python "$TRACKER" --db "$DB" doctor --stale-after-hours 24
```

Exports:

```bash
python "$TRACKER" --db "$DB" export --since 30d --format csv \
  --output /opt/data/metrics/task_time_30d.csv
```

## Interpretation

- **Wall time**: task finish minus task start.
- **Active time**: explicit active manual phases, or wall time minus observed
  LLM/tool waits in automatic mode.
- **Wait time**: API, tool, CI, deployment, external, user or blocked time.
- **Efficiency ratio**: active time divided by wall time.
- **Rework share**: rework time divided by active time.

Compare profiles/models only across similar task categories and adequate sample
sizes. One heterogeneous task is not evidence that a model is faster.

## Reliability rule

Failed, partial and blocked tasks must remain in the dataset. Never delete them
to improve a metric. They are the most useful evidence for reducing rework and
improving first-pass success.
