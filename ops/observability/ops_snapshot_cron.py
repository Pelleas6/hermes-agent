#!/usr/bin/env python3
"""Silent no-agent cron wrapper for a local observability snapshot."""

from __future__ import annotations

import sys
from datetime import timedelta

import ops_observability as ops


def main() -> int:
    root = ops.resolve_root()
    config, _ = ops.load_config(root)
    snapshot = ops.build_snapshot(root, config, window=timedelta(days=7))
    ops.persist_snapshot(snapshot, root / "metrics")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Observabilité snapshot en échec: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
