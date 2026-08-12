# task-metrics

Local Hermes plugin for automatic delivery-efficiency measurement.

It observes lifecycle hooks and writes bounded, content-minimised metrics into
`/opt/data/metrics/task_time.sqlite3` (or `HERMES_TASK_TIME_DB`). It does not
store prompts, responses, tool arguments/results, raw errors or credentials.

Measured surfaces:

- LLM API duration and token counters;
- tool duration and coarse success state;
- verification/rework attempts;
- subagent duration;
- session outcome;
- Kanban claim, completion and blocking;
- active profile/model/provider metadata.

The plugin is best-effort: its failures are isolated from the agent pipeline.
Use `/task-metrics` for a compact local summary.
