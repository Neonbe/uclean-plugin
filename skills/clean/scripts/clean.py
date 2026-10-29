#!/usr/bin/env python3
"""UClean command-line entry point. Python 3.10+, standard library only."""
import argparse
import json
import math
import os
import signal
import sys
from pathlib import Path

from uclean import VERSION
from uclean.engine import Engine, GIB
from uclean.fs import Rejected


def parser():
    p = argparse.ArgumentParser(description="UClean — progressive local Mac disk cleanup")
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--state", type=Path, help="Private record directory (0700)")
    p.add_argument("--sandbox", type=Path, help="Marked temporary test home; never use real data")
    p.add_argument("--protect", action="append", default=[], help="Additional protected path")
    p.add_argument("--redact", action="store_true", help="Replace home and candidate paths in output")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check runtime and platform")
    scan = sub.add_parser("scan", help="Read-only scan; records local metadata")
    scan.add_argument("--level", choices=["quick", "recommended", "deep"], default="quick")
    scan.add_argument("--target-free-gb", type=float, default=10, help="Desired available space, not amount to delete")
    scan.add_argument("--budget", type=float, help="Maximum traversal budget in seconds, capped at 300")
    plan = sub.add_parser("plan", help="Build exact expiring preview; no deletion")
    plan.add_argument("--scan", required=True)
    plan.add_argument("--items", nargs="+", help="Candidate IDs. Default: eligible quick caches only")
    apply = sub.add_parser("apply", help="Execute exactly one explicitly confirmed plan")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--confirm", required=True, help="Confirmation value from the displayed plan")
    verify = sub.add_parser("verify", help="Read saved receipt and current disk availability")
    verify.add_argument("--id", required=True)
    restore = sub.add_parser("restore", help="Restore a trashed installer without overwriting anything")
    restore.add_argument("--receipt", required=True)
    restore.add_argument("--item", required=True)
    records = sub.add_parser("records", help="Remove old local records, retaining interrupted recovery records")
    records.add_argument("--prune-days", type=int, default=30)
    ui = sub.add_parser("ui", help="Open the private local button panel")
    ui.add_argument("--port", type=int, default=0)
    ui.add_argument("--no-open", action="store_true")
    return p


def redact(value, home):
    if isinstance(value, dict):
        return {k: ("[local path]" if k in ("path", "trash_path", "recovery_path") else redact(v, home)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, home) for v in value]
    if isinstance(value, str):
        return value.replace(str(home), "~")
    return value


def main():
    args = parser().parse_args()
    try:
        if args.command == "doctor":
            import platform
            data = dict(version=VERSION, python=sys.version.split()[0], macos=platform.system() == "Darwin",
                        supported=sys.version_info >= (3, 10) and platform.system() == "Darwin" and os.geteuid() != 0,
                        runtime="Python 3.10+", network_required=False, dependencies=[],
                        message="扫描与面板无需安装依赖。真实清理只支持 macOS 普通用户；不会自动提权。")
        else:
            engine = Engine(home=args.sandbox, state=args.state, sandbox=bool(args.sandbox), protected=args.protect)
            def stop(*_):
                engine.cancel.set()
            if args.command != "ui":
                signal.signal(signal.SIGINT, stop)
                signal.signal(signal.SIGTERM, stop)
            if args.command == "scan":
                if not math.isfinite(args.target_free_gb) or (args.budget is not None and not math.isfinite(args.budget)):
                    raise Rejected("invalid_scan_options")
                data = engine.scan(args.level, int(args.target_free_gb * GIB), args.budget)
            elif args.command == "plan":
                data = engine.plan(args.scan, args.items)
            elif args.command == "apply":
                data = engine.apply(args.plan, args.confirm)
            elif args.command == "verify":
                data = engine.verify(args.id)
            elif args.command == "restore":
                data = engine.restore(args.receipt, args.item)
            elif args.command == "records":
                if args.prune_days < 1:
                    raise Rejected("retention_must_be_at_least_one_day")
                data = engine.store.prune(args.prune_days)
            else:
                from uclean.server import serve
                serve(engine, args.port, not args.no_open)
                return 0
            if args.redact:
                data = redact(data, engine.home)
        print(json.dumps(data, ensure_ascii=True, indent=2))
        return 0
    except (Rejected, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc), "next": {"scenario": "error", "actions": [
            {"id": "scan", "label": "重新扫描"}, {"id": "done", "label": "暂时结束"}]}}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
