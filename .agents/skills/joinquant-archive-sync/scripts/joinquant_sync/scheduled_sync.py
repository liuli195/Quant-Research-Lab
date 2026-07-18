from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .archive import ObjectLocked, object_lock


AUTOMATION_BRANCH = "codex/joinquant-archive-auto"
RUNTIME_PARTS = ("QuantResearchLab", "joinquant-archive-sync")
PR_FLOW_PLUGIN = "pr-flow@my-agent-skills-marketplace"


class ScheduledSyncError(RuntimeError):
    pass


def _runtime_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())).joinpath(
        *RUNTIME_PARTS
    ).resolve()


def _write_state(root: Path, payload: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".last-run.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, root / "last-run.json")
    finally:
        temporary.unlink(missing_ok=True)


def _discover_pr_flow() -> Path:
    commands = (
        ["codex", "plugin", "list", "--json"],
        ["claude", "plugin", "list", "--json"],
    )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                errors="replace",
            )
            payload = json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, json.JSONDecodeError):
            continue
        plugins = payload.get("installed", []) if isinstance(payload, dict) else payload
        if not isinstance(plugins, list):
            continue
        for plugin in plugins:
            if not isinstance(plugin, dict):
                continue
            plugin_id = plugin.get("pluginId") or plugin.get("id")
            if (
                plugin_id != PR_FLOW_PLUGIN
                or plugin.get("enabled") is not True
                or plugin.get("installed", True) is not True
            ):
                continue
            source = plugin.get("source")
            root = (
                source.get("path")
                if isinstance(source, dict)
                else plugin.get("installPath")
            )
            if not root:
                continue
            script = (
                Path(str(root))
                / "skills"
                / "pr-flow"
                / "scripts"
                / "pr_flow.py"
            ).resolve()
            if script.is_file():
                return script
    raise ScheduledSyncError("pr_flow_unavailable")


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        raise ScheduledSyncError("git_failed")
    return result.stdout.strip()


def _command_ok(
    command: list[str], *, cwd: Path | None = None, reason: str = "command_failed"
) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        raise ScheduledSyncError(reason)


def _run_json(
    command: list[str], *, cwd: Path | None = None
) -> tuple[int, dict[str, object]]:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise ScheduledSyncError("command_output_invalid") from error
    if not isinstance(payload, dict):
        raise ScheduledSyncError("command_output_invalid")
    return result.returncode, payload


def _prepare_worktree(repository: Path, root: Path) -> Path:
    repository = repository.resolve()
    _git(repository, "rev-parse", "--show-toplevel")
    _git(repository, "fetch", "origin", "main")
    worktree = root.resolve() / "worktree"
    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(repository, "worktree", "add", "--detach", str(worktree), "origin/main")
        return worktree
    if _git(worktree, "status", "--porcelain"):
        raise ScheduledSyncError("worktree_dirty")
    branch = _git(worktree, "branch", "--show-current")
    if branch == AUTOMATION_BRANCH:
        return worktree
    if branch:
        raise ScheduledSyncError("worktree_branch_unknown")
    _git(worktree, "checkout", "--detach", "origin/main")
    return worktree


def _changed_paths(worktree: Path) -> tuple[set[str], set[str]]:
    tracked = set(
        filter(None, _git(worktree, "diff", "--name-only", "-z", "HEAD", "--").split("\0"))
    )
    untracked = set(
        filter(
            None,
            _git(
                worktree, "ls-files", "--others", "--exclude-standard", "-z"
            ).split("\0"),
        )
    )
    return tracked, untracked


def _allowed_prefixes(
    results: list[dict[str, object]],
) -> tuple[set[str], set[str]]:
    files = {"joinquant/strategies/strategy_index.csv"}
    directories: set[str] = set()
    for result in results:
        strategy_id = str(result.get("strategy_id") or "")
        if not strategy_id:
            continue
        strategy = f"joinquant/strategies/{strategy_id}"
        files.update(
            {
                f"{strategy}/manifest.json",
                f"{strategy}/default_code.py",
                f"{strategy}/simulations/index.json",
            }
        )
        simulation_id = str(result.get("simulation_id") or "")
        if simulation_id:
            directories.add(f"{strategy}/simulations/{simulation_id}/")
    return files, directories


def _is_allowed(path: str, files: set[str], directories: set[str]) -> bool:
    selected = path.replace("\\", "/")
    return selected in files or any(selected.startswith(prefix) for prefix in directories)


def _rollback(
    worktree: Path,
    baseline: str,
    tracked: set[str],
    untracked: set[str],
    files: set[str],
    directories: set[str],
) -> str:
    restore = sorted(path for path in tracked if _is_allowed(path, files, directories))
    if restore:
        _git(worktree, "restore", "--source", baseline, "--worktree", "--", *restore)
    root = worktree.resolve()
    for relative in sorted(
        path for path in untracked if _is_allowed(path, files, directories)
    ):
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ScheduledSyncError("rollback_path_invalid") from error
        target.unlink(missing_ok=True)
    return "complete"


def _batch_failure(
    root: Path,
    worktree: Path,
    baseline: str,
    reason: str,
    results: list[dict[str, object]],
) -> tuple[int, dict[str, object]]:
    tracked, untracked = _changed_paths(worktree)
    files, directories = _allowed_prefixes(results)
    state: dict[str, object] = {
        "phase": "sync" if reason == "sync_failed" else "verify",
        "status": "failed",
        "reason": reason,
        "worktree": str(worktree),
        "branch": _git(worktree, "branch", "--show-current"),
        "pr": None,
        "rollback_status": "pending",
    }
    _write_state(root, state)
    state["rollback_status"] = _rollback(
        worktree, baseline, tracked, untracked, files, directories
    )
    _write_state(root, state)
    return 1, state


def _run_pr_flow(
    python_exe: Path,
    script: Path,
    worktree: Path,
    *,
    command: str,
    pr: object = None,
) -> tuple[int, dict[str, object]]:
    arguments = [
        str(python_exe),
        str(script),
        command,
        "--project",
        str(worktree),
    ]
    if command == "complete":
        arguments.extend(
            ["--summary", "活动模拟交易归档", "--scope", "joinquant/strategies"]
        )
    elif command == "cleanup" and pr is not None:
        arguments.extend(["--pr", str(pr)])
    else:
        raise ScheduledSyncError("pr_flow_state_invalid")
    result = subprocess.run(
        arguments,
        cwd=worktree,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    status_path = worktree / ".pr-flow" / "last-status.json"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    return result.returncode, payload if isinstance(payload, dict) else {}


def _pr_flow_result(
    root: Path,
    worktree: Path,
    python_exe: Path,
    script: Path,
    *,
    command: str,
    pr: object = None,
) -> tuple[int, dict[str, object]]:
    code, payload = _run_pr_flow(
        python_exe, script, worktree, command=command, pr=pr
    )
    details = payload.get("details")
    details = details if isinstance(details, dict) else {}
    pr_number = details.get("pr") or pr
    state: dict[str, object] = {
        "phase": "pr_flow",
        "status": "complete" if code == 0 else "failed",
        "reason": "pr_flow_complete" if code == 0 else "pr_flow_stopped",
        "worktree": str(worktree),
        "branch": _git(worktree, "branch", "--show-current"),
        "pr": pr_number,
        "rollback_status": None,
    }
    _write_state(root, state)
    return (0 if code == 0 else 1), state


def _recovery_action(worktree: Path) -> tuple[str, object]:
    try:
        payload = json.loads(
            (worktree / ".pr-flow" / "last-status.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        return "complete", None
    details = payload.get("details") if isinstance(payload, dict) else None
    if (
        isinstance(details, dict)
        and payload.get("command") == "cleanup"
        and details.get("sourceBranch") == AUTOMATION_BRANCH
        and details.get("pr") is not None
    ):
        return "cleanup", details["pr"]
    return "complete", None


def _run_new_batch(
    root: Path,
    worktree: Path,
    *,
    python_exe: Path,
    cli: Path,
    pr_flow: Path,
) -> tuple[int, dict[str, object]]:
    baseline = _git(worktree, "rev-parse", "HEAD")
    auth_code, auth = _run_json(
        [
            str(python_exe),
            str(cli),
            "auth",
            "--headless",
            "--timeout-seconds",
            "0",
        ],
        cwd=cli.resolve().parent,
    )
    if auth_code != 0 or auth.get("status") != "authenticated":
        raise ScheduledSyncError(str(auth.get("status") or "auth_required"))
    sync_code, sync = _run_json(
        [
            str(python_exe),
            str(cli),
            "sync-active-simulations",
            "--repository",
            str(worktree),
        ],
        cwd=cli.resolve().parent,
    )
    raw_results = sync.get("results")
    results = (
        [item for item in raw_results if isinstance(item, dict)]
        if isinstance(raw_results, list)
        else []
    )
    if (
        sync_code != 0
        or sync.get("status") != "complete"
        or len(results) != len(raw_results or [])
        or any(item.get("status") == "failed" for item in results)
    ):
        return _batch_failure(root, worktree, baseline, "sync_failed", results)
    for result in results:
        if result.get("status") != "committed":
            continue
        strategy_id = str(result.get("strategy_id") or "")
        simulation_id = str(result.get("simulation_id") or "")
        if not strategy_id or not simulation_id:
            return _batch_failure(root, worktree, baseline, "verify_failed", results)
        object_dir = (
            worktree
            / "joinquant"
            / "strategies"
            / strategy_id
            / "simulations"
            / simulation_id
        )
        verify_code, verification = _run_json(
            [str(python_exe), str(cli), "verify", "--object", str(object_dir)],
            cwd=cli.resolve().parent,
        )
        gate = verification.get("gate")
        if (
            verify_code != 0
            or not isinstance(gate, dict)
            or gate.get("status") != "pass"
        ):
            return _batch_failure(root, worktree, baseline, "verify_failed", results)
    tracked, untracked = _changed_paths(worktree)
    files, directories = _allowed_prefixes(results)
    changed = tracked | untracked
    if any(not _is_allowed(path, files, directories) for path in changed):
        return _batch_failure(root, worktree, baseline, "path_out_of_scope", results)
    if not changed:
        state = {
            "phase": "sync",
            "status": "noop",
            "reason": "no_changes",
            "worktree": str(worktree),
            "branch": "",
            "pr": None,
            "rollback_status": None,
        }
        _write_state(root, state)
        return 0, state
    _git(worktree, "switch", "-c", AUTOMATION_BRANCH)
    _git(worktree, "add", "--", *sorted(changed))
    _git(worktree, "commit", "-m", "归档活动模拟交易更新")
    return _pr_flow_result(
        root,
        worktree,
        python_exe,
        pr_flow,
        command="complete",
    )


def run_scheduled_sync(
    repository: Path, *, python_exe: Path, cli: Path
) -> tuple[int, dict[str, object]]:
    root = _runtime_root()
    run_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        with object_lock(root):
            pr_flow = _discover_pr_flow()
            _command_ok(["gh", "auth", "status"], reason="gh_auth_required")
            worktree = _prepare_worktree(repository, root)
            if _git(worktree, "branch", "--show-current") == AUTOMATION_BRANCH:
                command, pr = _recovery_action(worktree)
                code, state = _pr_flow_result(
                    root,
                    worktree,
                    python_exe,
                    pr_flow,
                    command=command,
                    pr=pr,
                )
            else:
                code, state = _run_new_batch(
                    root,
                    worktree,
                    python_exe=python_exe,
                    cli=cli,
                    pr_flow=pr_flow,
                )
    except ObjectLocked:
        state = {
            "phase": "lock",
            "status": "skipped",
            "reason": "run_locked",
        }
        code = 0
    except ScheduledSyncError as error:
        state = {
            "phase": "preflight",
            "status": "failed",
            "reason": str(error),
        }
        code = 1
    state.update(
        {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "recovery_command": subprocess.list2cmdline(
                [
                    str(python_exe),
                    str(cli),
                    "scheduled-sync-pr",
                    "--repository",
                    str(repository.resolve()),
                ]
            ),
        }
    )
    state.setdefault("worktree", str(root / "worktree"))
    state.setdefault("branch", "")
    state.setdefault("pr", None)
    state.setdefault("rollback_status", None)
    _write_state(root, state)
    return code, state
