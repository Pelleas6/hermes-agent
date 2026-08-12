from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Optional, Sequence

from .common import VERSION, load_config, parse_window, resolve_root
from .health_render import build_snapshot, render_alert, render_markdown
from .persistence import create_backup, doctor, persist_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Hermes observability collector")
    parser.add_argument("--root", help="Shared Hermes root (default: HERMES_SHARED_ROOT/HERMES_HOME)")
    parser.add_argument("--output-dir", help="Metrics output directory (default: <root>/metrics)")
    parser.add_argument("--config", help="Observability JSON config")
    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot", help="Collect and persist a current snapshot")
    snapshot.add_argument("--since", default="7d")
    snapshot.add_argument("--quiet", action="store_true")

    report = sub.add_parser("report", help="Collect and print a report")
    report.add_argument("--since", default="7d")
    report.add_argument("--format", choices=("markdown", "json"), default="markdown")

    alert = sub.add_parser("alert", help="Collect and print only actionable alerts")
    alert.add_argument("--since", default="24h")

    backup = sub.add_parser("backup", help="Create verified local SQLite backups")
    backup.add_argument("--retention-days", type=int, default=14)

    sub.add_parser("doctor", help="Verify the local observability installation")
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root = resolve_root(args.root)
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else root / "metrics"
    config, config_path = load_config(root, args.config)

    if args.command in {"snapshot", "report", "alert"}:
        try:
            window = parse_window(args.since)
        except ValueError as exc:
            raise SystemExit(str(exc))
        snapshot = build_snapshot(root, config, window=window)
        paths = persist_snapshot(snapshot, output_dir)
        if args.command == "snapshot":
            if not args.quiet:
                print(json.dumps({"health": snapshot["health"], "paths": paths}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "report":
            if args.format == "json":
                print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(render_markdown(snapshot), end="")
            return 0
        text = render_alert(snapshot)
        if text:
            print(text, end="")
        return 2 if snapshot.get("health", {}).get("status") == "critical" else 0

    if args.command == "backup":
        result = create_backup(root, output_dir, retention_days=args.retention_days)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("all_integrity_ok") and result.get("all_restore_test_ok") else 1

    result = doctor(root, output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(argv)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"ops_observability failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
