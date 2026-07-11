from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from joinquant_sync.archive import (
    AttributionIncomplete,
    IntegrityError,
    TargetRequired,
    extract_paid_log_range,
    stage_external_file,
    validate_history_target,
    verify_existing_manifest,
)
from joinquant_sync.browser import (
    AuthRequired,
    FreeLogIncomplete,
    PaidConfirmationRequired,
    TargetDiscoveryError,
    create_paid_preview,
    download_confirmed_paid_log,
    discover_history_targets,
    ensure_authenticated,
    open_authenticated_context,
    load_paid_preview,
    open_paid_log_quote,
    persist_authenticated_session,
)
from joinquant_sync.query import export_csv, query_rows
from joinquant_sync.scheduler import (
    SchedulerError,
    install_scheduler,
    scheduler_status,
    uninstall_scheduler,
)
from joinquant_sync.selftest import run_self_test
from joinquant_sync.sync_pipeline import (
    commit_paid_log_supplement,
    persist_failure_evidence,
    sync_all_active_simulations,
    sync_selected_backtest,
)


class ProfileError(ValueError):
    """Raised when browser credentials would be stored in the repository."""


def _profile_path(value: str | None, *repositories: Path) -> Path:
    profile = (
        Path(value)
        if value
        else Path(os.environ.get("LOCALAPPDATA", Path.home()))
        / "QuantResearchLab"
        / "joinquant-playwright"
    ).resolve()
    if value:
        for repository in (Path.cwd(), *repositories):
            try:
                profile.relative_to(repository.resolve())
            except ValueError:
                continue
            raise ProfileError("--profile must be outside the repository")
    return profile


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
    sync.add_argument("--repository", default=".")
    sync.add_argument("--profile")
    sync.add_argument("--attribution-path", default="")
    sync.add_argument("--stage-only")

    active_sync = commands.add_parser("sync-active-simulations")
    active_sync.add_argument("--repository", default=".")
    active_sync.add_argument("--profile")

    list_targets = commands.add_parser("list-targets")
    list_targets.add_argument("--strategy", required=True)
    list_targets.add_argument("--profile")

    verify = commands.add_parser("verify")
    verify_target = verify.add_mutually_exclusive_group(required=True)
    verify_target.add_argument("--object")
    verify_target.add_argument("--import-file")
    verify.add_argument("--stage-only")

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

    paid_log = commands.add_parser("paid-log")
    paid_actions = paid_log.add_subparsers(dest="paid_action", required=True)
    paid_preview = paid_actions.add_parser("preview")
    paid_preview.add_argument("--object", required=True)
    paid_preview.add_argument("--type", choices=["normal_log"], required=True)
    paid_preview.add_argument("--range", required=True)
    paid_preview.add_argument("--profile")
    paid_download = paid_actions.add_parser("download")
    paid_download.add_argument("--preview-id", required=True)
    paid_download.add_argument("--confirm", action="store_true")
    paid_download.add_argument("--destination", required=True)
    paid_download.add_argument("--profile")

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
    profile = _profile_path(args.profile)
    with open_authenticated_context(profile, headless=bool(args.headless)) as context:
        page = context.pages[0]
        page.goto("https://www.joinquant.com/algorithm/index/list")
        deadline = time.monotonic() + max(0, args.timeout_seconds)
        while "/login" in page.url.lower() and time.monotonic() < deadline:
            page.wait_for_timeout(500)
        ensure_authenticated(page)
        persist_authenticated_session(context, profile)
    print(json.dumps({"status": "authenticated", "profile": str(profile)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    if hasattr(args, "profile"):
        roots = [Path(args.repository)] if hasattr(args, "repository") else []
        if getattr(args, "paid_action", None) == "preview":
            roots.append(Path(args.object))
        try:
            args.profile = str(_profile_path(args.profile, *roots))
        except ProfileError as error:
            print(json.dumps({"status": "invalid_profile", "message": str(error)}))
            return 2
    if args.command == "auth":
        try:
            return _run_auth(args)
        except ProfileError as error:
            print(json.dumps({"status": "invalid_profile", "message": str(error)}))
            return 2
        except AuthRequired:
            print(json.dumps({"status": "auth_required"}))
            return 1
    if args.command == "verify":
        try:
            if args.object:
                path = Path(args.object)
                object_dir = path if path.is_dir() else path.parent
                manifest = verify_existing_manifest(object_dir)
                print(
                    json.dumps(
                        {
                            "status": "verified",
                            "object": str(object_dir.resolve()),
                            "gate": manifest["gate"],
                            "datasets": {
                                name: item.get("status")
                                for name, item in manifest["datasets"].items()
                            },
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                if not args.stage_only:
                    parser.error("--stage-only is required with --import-file")
                item = stage_external_file(
                    Path(args.import_file), Path(args.stage_only)
                )
                print(json.dumps(item, ensure_ascii=False))
        except IntegrityError as error:
            print(json.dumps({"status": "integrity_failed", "message": str(error)}))
            return 3
        return 0
    if args.command == "sync-backtest":
        try:
            validate_history_target(args.strategy, args.target)
        except TargetRequired:
            print(json.dumps({"status": "target_required"}))
            return 2
        repository = Path(args.stage_only or args.repository).resolve()
        try:
            profile = _profile_path(args.profile, repository)
            with open_authenticated_context(profile, headless=True) as context:
                result = sync_selected_backtest(
                    context.pages[0],
                    repository,
                    args.strategy,
                    args.target,
                    attribution_path=args.attribution_path,
                )
        except AuthRequired:
            print(json.dumps({"status": "auth_required"}))
            return 1
        except (AttributionIncomplete, FreeLogIncomplete, IntegrityError) as error:
            failure_evidence = (
                persist_failure_evidence(
                    repository, error, identity=f"{args.strategy}:{args.target}"
                )
                if isinstance(error, FreeLogIncomplete)
                else None
            )
            print(
                json.dumps(
                    {
                        "status": "integrity_failed",
                        "error": type(error).__name__,
                        "message": str(error),
                        **(
                            {"failure_evidence": failure_evidence}
                            if failure_evidence
                            else {}
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return 3
        except Exception as error:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error": type(error).__name__,
                        "message": str(error),
                    },
                    ensure_ascii=False,
                )
            )
            return 1
        manifest = result.pop("manifest")
        result["gate"] = manifest.get("gate")
        result["datasets"] = {
            name: item.get("status")
            for name, item in manifest.get("datasets", {}).items()
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "sync-active-simulations":
        repository = Path(args.repository).resolve()
        try:
            profile = _profile_path(args.profile, repository)
            with open_authenticated_context(profile, headless=True) as context:
                results = sync_all_active_simulations(context.pages[0], repository)
        except AuthRequired:
            print(json.dumps({"status": "auth_required"}))
            return 1
        failed = any(item.get("status") == "failed" for item in results)
        print(
            json.dumps(
                {"status": "partial" if failed else "complete", "results": results},
                ensure_ascii=False,
            )
        )
        return 1 if failed else 0
    if args.command == "list-targets":
        try:
            profile = _profile_path(args.profile)
            with open_authenticated_context(profile, headless=True) as context:
                targets = discover_history_targets(context.pages[0], args.strategy)
        except AuthRequired:
            print(json.dumps({"status": "auth_required"}))
            return 1
        except TargetDiscoveryError as error:
            print(
                json.dumps({"status": "target_discovery_failed", "error": str(error)})
            )
            return 1
        print(
            json.dumps(
                {"status": "complete", "strategy": args.strategy, "targets": targets},
                ensure_ascii=False,
            )
        )
        return 0
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
        return 0
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
        return 0
    if args.command == "paid-log":
        try:
            profile = _profile_path(args.profile)
            if args.paid_action == "preview":
                try:
                    start, end = (int(value) for value in args.range.split(":", 1))
                except (ValueError, TypeError):
                    raise PaidConfirmationRequired(
                        "paid log range must be START:END"
                    ) from None
                if start < 0 or start >= end:
                    raise PaidConfirmationRequired("paid log range must be non-empty")
                path = Path(args.object)
                object_dir = path if path.is_dir() else path.parent
                manifest = verify_existing_manifest(object_dir)
                dataset = manifest["datasets"].get(args.type)
                if (
                    not isinstance(dataset, dict)
                    or dataset.get("status") != "capped_free"
                ):
                    print(
                        json.dumps(
                            {
                                "status": "not_required",
                                "reason": "paid log preview is only allowed for capped_free data",
                            }
                        )
                    )
                    return 0
                source = manifest.get("source")
                source_url = (
                    str(source.get("url") or "") if isinstance(source, dict) else ""
                )
                with open_authenticated_context(profile, headless=True) as context:
                    quote = open_paid_log_quote(context.pages[0], source_url)
                preview = create_paid_preview(
                    str(object_dir.resolve()),
                    args.type,
                    args.range,
                    quote,
                    store_dir=profile,
                    source_url=source_url,
                    object_path=str(object_dir.resolve()),
                )
                print(
                    json.dumps(
                        {
                            "status": "confirmation_required",
                            "preview_id": preview["preview_id"],
                            "log_type": args.type,
                            "requested_local_range": args.range,
                            "quote": quote,
                            "warning": "JoinQuant charges for full_log; only the requested local range will be retained",
                        },
                        ensure_ascii=False,
                    )
                )
                return 4
            if not args.confirm:
                raise PaidConfirmationRequired(
                    "--confirm is required for paid download"
                )
            preview = load_paid_preview(args.preview_id, store_dir=profile)
            object_dir = Path(str(preview.get("object_path") or "")).resolve()
            verify_existing_manifest(object_dir)
            destination = Path(args.destination).resolve()
            try:
                destination.relative_to(object_dir)
            except ValueError:
                raise PaidConfirmationRequired(
                    "paid log destination must stay inside the archive object"
                ) from None
            downloaded = (
                object_dir
                / "supplements"
                / "paid"
                / "source"
                / f"{preview['preview_id']}.zip"
            )
            if downloaded.is_file():
                payload = downloaded.read_bytes()
                remote = {
                    "path": str(downloaded),
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "quote": preview["quote"],
                    "reused_paid_download": True,
                }
            else:
                with open_authenticated_context(profile, headless=True) as context:
                    remote = download_confirmed_paid_log(
                        context.pages[0],
                        preview,
                        downloaded,
                        confirm=True,
                        store_dir=profile,
                    )
            selected = extract_paid_log_range(
                downloaded,
                str(preview["range"]),
                destination,
            )
            commit_paid_log_supplement(
                object_dir, preview, downloaded, selected, remote
            )
            remote.pop("path", None)
            print(
                json.dumps(
                    {"status": "downloaded", "remote": remote, "selected": selected},
                    ensure_ascii=False,
                )
            )
            return 0
        except PaidConfirmationRequired as error:
            print(
                json.dumps({"status": "confirmation_required", "message": str(error)})
            )
            return 4
        except IntegrityError as error:
            print(json.dumps({"status": "integrity_failed", "message": str(error)}))
            return 3
    if args.command == "schedule-install":
        root = Path(args.repo_root).resolve()
        command = [
            str(root / ".venv" / "Scripts" / "python.exe"),
            str(Path(__file__).resolve()),
            "sync-active-simulations",
            "--repository",
            str(root),
        ]
        try:
            install_scheduler(args.task_name, command)
        except SchedulerError as error:
            print(json.dumps({"status": "failed", "error": str(error)}))
            return 1
        print(json.dumps({"status": "installed", "task_name": args.task_name}))
        return 0
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
        return 0
    if args.command == "self-test":
        print(json.dumps(run_self_test(), ensure_ascii=False))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
