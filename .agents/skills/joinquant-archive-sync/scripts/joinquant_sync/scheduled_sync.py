from __future__ import annotations

import json
import os
import subprocess
import uuid
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


def run_scheduled_sync(
    repository: Path, *, python_exe: Path, cli: Path
) -> tuple[int, dict[str, object]]:
    root = _runtime_root()
    try:
        with object_lock(root):
            _discover_pr_flow()
            worktree = _prepare_worktree(repository, root)
            state: dict[str, object] = {
                "phase": "preflight",
                "status": "complete",
                "reason": "preflight_complete",
                "worktree": str(worktree),
            }
            _write_state(root, state)
            return 0, state
    except ObjectLocked:
        state = {
            "phase": "lock",
            "status": "skipped",
            "reason": "run_locked",
        }
        _write_state(root, state)
        return 0, state
    except ScheduledSyncError as error:
        state = {
            "phase": "preflight",
            "status": "failed",
            "reason": str(error),
        }
        _write_state(root, state)
        return 1, state
