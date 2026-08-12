#!/usr/bin/env python3
"""No-agent weekly KPI report rendered directly from local metrics."""

from __future__ import annotations

import sys
from datetime import timedelta

import ops_observability as ops


def main() -> int:
    root = ops.resolve_root()
    output = root / "metrics"
    config, _ = ops.load_config(root)
    snapshot = ops.build_snapshot(root, config, window=timedelta(days=7))
    ops.persist_snapshot(snapshot, output)
    print(ops.render_markdown(snapshot), end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Rapport hebdomadaire observabilité en échec: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
