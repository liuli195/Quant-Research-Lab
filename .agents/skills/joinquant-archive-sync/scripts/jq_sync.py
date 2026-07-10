from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from joinquant_sync.archive import (
    TargetRequired,
    stage_external_file,
    validate_history_target,
)
from joinquant_sync.browser import (
    AuthRequired,
    ensure_authenticated,
    open_authenticated_context,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jq_sync.py")
    commands = parser.add_subparsers(dest="command", required=True)

    auth = commands.add_parser("auth")
    auth.add_argument("--profile")
    auth.add_argument("--headless", action="store_true")
    auth.add_argument("--timeout-seconds", type=float, default=300)

    sync = commands.add_parser("sync-backtest")
    sync.add_argument("--strategy", required=True)
    sync.add_argument("--target", required=True)
    sync.add_argument("--stage-only", required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("--import-file", required=True)
    verify.add_argument("--stage-only", required=True)
    return parser


def _run_auth(args: argparse.Namespace) -> int:
    profile = Path(args.profile) if args.profile else Path(
        os.environ.get("LOCALAPPDATA", Path.home())
    ) / "QuantResearchLab" / "joinquant-playwright"
    with open_authenticated_context(profile, headless=bool(args.headless)) as context:
        page = context.pages[0]
        page.goto("https://www.joinquant.com/algorithm/index/list")
        deadline = time.monotonic() + max(0, args.timeout_seconds)
        while "/login" in page.url.lower() and time.monotonic() < deadline:
            page.wait_for_timeout(500)
        ensure_authenticated(page)
        context.storage_state(path=profile / "storage-state.json")
    print(json.dumps({"status": "authenticated", "profile": str(profile)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    if args.command == "auth":
        try:
            return _run_auth(args)
        except AuthRequired:
            print(json.dumps({"status": "auth_required"}))
            return 1
    if args.command == "verify":
        item = stage_external_file(Path(args.import_file), Path(args.stage_only))
        print(json.dumps(item, ensure_ascii=False))
    if args.command == "sync-backtest":
        try:
            validate_history_target(args.strategy, args.target)
        except TargetRequired:
            print(json.dumps({"status": "target_required"}))
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
