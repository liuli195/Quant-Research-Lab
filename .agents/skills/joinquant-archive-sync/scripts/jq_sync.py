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
from joinquant_sync.query import export_csv, query_rows
from joinquant_sync.scheduler import (
    SchedulerError,
    install_scheduler,
    scheduler_status,
    uninstall_scheduler,
)
from joinquant_sync.selftest import run_self_test


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

    active_sync = commands.add_parser("sync-active-simulations")
    active_sync.add_argument("--repository", default=".")
    active_sync.add_argument("--profile")

    verify = commands.add_parser("verify")
    verify.add_argument("--import-file", required=True)
    verify.add_argument("--stage-only", required=True)

    query = commands.add_parser("query")
    query.add_argument("--object", required=True)
    query.add_argument("--dataset", required=True)
    query.add_argument("--limit", type=int, default=100)

    csv_export = commands.add_parser("export-csv")
    csv_export.add_argument("--object", required=True)
    csv_export.add_argument("--dataset", required=True)
    csv_export.add_argument("--fields", required=True)
    csv_export.add_argument("--start")
    csv_export.add_argument("--end")
    csv_export.add_argument("--destination", required=True)

    schedule_install = commands.add_parser("schedule-install")
    schedule_install.add_argument("--repo-root", default=".")
    schedule_install.add_argument("--task-name", default="JoinQuantArchiveSync")
    schedule_status = commands.add_parser("schedule-status")
    schedule_status.add_argument("--task-name", default="JoinQuantArchiveSync")
    schedule_uninstall = commands.add_parser("schedule-uninstall")
    schedule_uninstall.add_argument("--task-name", default="JoinQuantArchiveSync")
    self_test = commands.add_parser("self-test")
    self_test.add_argument("--repo-root", default=".")
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
    if args.command == "sync-active-simulations":
        print(
            json.dumps(
                {
                    "status": "integration_pending",
                    "repository": str(Path(args.repository).resolve()),
                }
            )
        )
        return 1
    if args.command == "query":
        path = Path(args.object)
        manifest = path / "manifest.json" if path.is_dir() else path
        print(
            json.dumps(
                query_rows(manifest, args.dataset, args.limit),
                ensure_ascii=False,
                default=str,
            )
        )
    if args.command == "export-csv":
        path = Path(args.object)
        manifest = path / "manifest.json" if path.is_dir() else path
        result = export_csv(
            manifest,
            args.dataset,
            [field.strip() for field in args.fields.split(",") if field.strip()],
            args.start,
            args.end,
            Path(args.destination),
        )
        print(json.dumps(result, ensure_ascii=False))
    if args.command == "schedule-install":
        root = Path(args.repo_root).resolve()
        command = [
            str(root / ".venv" / "Scripts" / "python.exe"),
            str(Path(__file__).resolve()),
            "--repository",
            str(root),
        ]
        try:
            install_scheduler(args.task_name, command)
        except SchedulerError as error:
            print(json.dumps({"status": "failed", "error": str(error)}))
            return 1
        print(json.dumps({"status": "installed", "task_name": args.task_name}))
    if args.command == "schedule-status":
        result = scheduler_status(args.task_name)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("installed") else 1
    if args.command == "schedule-uninstall":
        try:
            uninstall_scheduler(args.task_name)
        except SchedulerError as error:
            print(json.dumps({"status": "failed", "error": str(error)}))
            return 1
        print(json.dumps({"status": "uninstalled", "task_name": args.task_name}))
    if args.command == "self-test":
        print(json.dumps(run_self_test(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
