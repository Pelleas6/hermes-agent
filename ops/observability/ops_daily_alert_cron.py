#!/usr/bin/env python3
"""No-agent daily alert: silent when healthy, concise when action is needed."""

from __future__ import annotations

import sys
from datetime import timedelta

import ops_observability as ops


def main() -> int:
    root = ops.resolve_root()
    output = root / "metrics"
    config, _ = ops.load_config(root)
    backup_issue = None
    try:
        backup = ops.create_backup(root, output, retention_days=14)
        if not backup.get("all_integrity_ok") or not backup.get("all_restore_test_ok"):
            backup_issue = "Sauvegarde observabilité créée mais intégrité/restauration incomplète."
    except Exception as exc:
        backup_issue = f"Sauvegarde observabilité en échec: {type(exc).__name__}."

    snapshot = ops.build_snapshot(root, config, window=timedelta(hours=24))
    ops.persist_snapshot(snapshot, output)
    alert = ops.render_alert(snapshot).strip()
    lines = []
    if alert:
        lines.append(alert)
    if backup_issue:
        lines.append(backup_issue)
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Observabilité quotidienne en échec: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
