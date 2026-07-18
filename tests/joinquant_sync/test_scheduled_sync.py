from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _completed(command: list[str], returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, "")


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    repository = tmp_path / "repository"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test User")
    (repository / "README.md").write_text("baseline\n", encoding="utf-8")
    archive = (
        repository
        / "joinquant"
        / "strategies"
        / "strategy-001"
        / "simulations"
        / "simulation-001"
    )
    archive.mkdir(parents=True)
    (archive / "manifest.json").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", "README.md", "joinquant")
    _git(repository, "commit", "-m", "baseline")
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "-u", "origin", "main")
    return repository, _git(repository, "rev-parse", "HEAD")


def test_locked_run_skips_without_external_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from joinquant_sync import scheduled_sync
    from joinquant_sync.archive import object_lock

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(
        scheduled_sync, "_discover_pr_flow", lambda: calls.append("plugin")
    )
    with object_lock(tmp_path / "QuantResearchLab" / "joinquant-archive-sync"):
        code, state = scheduled_sync.run_scheduled_sync(
            tmp_path / "repo",
            python_exe=Path(sys.executable),
            cli=Path("jq_sync.py"),
        )
    assert code == 0
    assert state["status"] == "skipped"
    assert state["reason"] == "run_locked"
    assert calls == []


def test_write_state_atomically_replaces_last_run(tmp_path: Path) -> None:
    from joinquant_sync.scheduled_sync import _write_state

    _write_state(tmp_path, {"status": "failed", "reason": "test"})
    assert json.loads((tmp_path / "last-run.json").read_text(encoding="utf-8")) == {
        "status": "failed",
        "reason": "test",
    }
    assert not list(tmp_path.glob(".last-run.*.tmp"))


def test_discover_pr_flow_prefers_codex(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from joinquant_sync import scheduled_sync

    plugin = tmp_path / "codex-pr-flow"
    script = plugin / "skills" / "pr-flow" / "scripts" / "pr_flow.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert command[0] == "codex"
        return _completed(
            command,
            0,
            json.dumps(
                {
                    "installed": [
                        {
                            "pluginId": "pr-flow@my-agent-skills-marketplace",
                            "installed": True,
                            "enabled": True,
                            "source": {"path": str(plugin)},
                        }
                    ]
                }
            ),
        )

    monkeypatch.setattr(scheduled_sync.subprocess, "run", run)
    assert scheduled_sync._discover_pr_flow() == script.resolve()
    assert calls == [["codex", "plugin", "list", "--json"]]


def test_discover_pr_flow_falls_back_to_claude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from joinquant_sync import scheduled_sync

    plugin = tmp_path / "claude-pr-flow"
    script = plugin / "skills" / "pr-flow" / "scripts" / "pr_flow.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "codex":
            return _completed(command, 1)
        return _completed(
            command,
            0,
            json.dumps(
                [
                    {
                        "id": "pr-flow@my-agent-skills-marketplace",
                        "enabled": True,
                        "installPath": str(plugin),
                    }
                ]
            ),
        )

    monkeypatch.setattr(scheduled_sync.subprocess, "run", run)
    assert scheduled_sync._discover_pr_flow() == script.resolve()


def test_discover_pr_flow_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from joinquant_sync import scheduled_sync

    monkeypatch.setattr(
        scheduled_sync.subprocess,
        "run",
        lambda command, **_kwargs: _completed(command, 1),
    )
    with pytest.raises(scheduled_sync.ScheduledSyncError, match="pr_flow_unavailable"):
        scheduled_sync._discover_pr_flow()


def test_prepare_worktree_creates_detached_origin_main(tmp_path: Path) -> None:
    from joinquant_sync.scheduled_sync import _prepare_worktree

    repository, origin_main = _repository(tmp_path)
    runtime_root = tmp_path / "runtime"
    prepared = _prepare_worktree(repository, runtime_root)
    assert prepared == runtime_root / "worktree"
    assert _git(prepared, "branch", "--show-current") == ""
    assert _git(prepared, "rev-parse", "HEAD") == origin_main


def test_prepare_worktree_accepts_clean_automation_branch(tmp_path: Path) -> None:
    from joinquant_sync.scheduled_sync import AUTOMATION_BRANCH, _prepare_worktree

    repository, _ = _repository(tmp_path)
    runtime_root = tmp_path / "runtime"
    prepared = _prepare_worktree(repository, runtime_root)
    _git(prepared, "switch", "-c", AUTOMATION_BRANCH)
    assert _prepare_worktree(repository, runtime_root) == prepared
    assert _git(prepared, "branch", "--show-current") == AUTOMATION_BRANCH


@pytest.mark.parametrize("dirty", [False, True])
def test_prepare_worktree_rejects_unknown_or_dirty_state(
    tmp_path: Path, dirty: bool
) -> None:
    from joinquant_sync.scheduled_sync import ScheduledSyncError, _prepare_worktree

    repository, _ = _repository(tmp_path)
    runtime_root = tmp_path / "runtime"
    prepared = _prepare_worktree(repository, runtime_root)
    marker = prepared / "preserve.txt"
    if dirty:
        marker.write_text("preserve\n", encoding="utf-8")
    else:
        _git(prepared, "switch", "-c", "unexpected")
    with pytest.raises(ScheduledSyncError):
        _prepare_worktree(repository, runtime_root)
    if dirty:
        assert marker.read_text(encoding="utf-8") == "preserve\n"


def _run_scenario(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    from joinquant_sync import scheduled_sync

    repository, baseline = _repository(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(scheduled_sync, "_runtime_root", lambda: runtime_root)
    monkeypatch.setattr(
        scheduled_sync, "_discover_pr_flow", lambda: tmp_path / "pr_flow.py"
    )
    monkeypatch.setattr(
        scheduled_sync, "_command_ok", lambda *_args, **_kwargs: None, raising=False
    )
    calls: list[str] = []

    def run_json(
        command: list[str], *, cwd: Path | None = None
    ) -> tuple[int, dict[str, object]]:
        action = command[2]
        calls.append(action)
        if action == "auth":
            return 0, {"status": "authenticated"}
        if action == "verify":
            if name == "verify_command_failed":
                return 1, {"status": "integrity_failed"}
            if name == "verify_gate_failed":
                return 0, {"status": "verified", "gate": {"status": "fail"}}
            return 0, {"status": "verified", "gate": {"status": "pass"}}
        assert action == "sync-active-simulations"
        worktree = runtime_root / "worktree"
        archive = (
            worktree
            / "joinquant"
            / "strategies"
            / "strategy-001"
            / "simulations"
            / "simulation-001"
        )
        result = {
            "status": "committed",
            "strategy_id": "strategy-001",
            "simulation_id": "simulation-001",
            "gate": {"status": "pass"},
        }
        if name == "noop":
            result["status"] = "unchanged"
            return 0, {"status": "complete", "results": [result]}
        if name in {
            "valid",
            "sync_failed",
            "verify_command_failed",
            "verify_gate_failed",
        }:
            (archive / "manifest.json").write_text("changed\n", encoding="utf-8")
        if name == "sync_failed":
            (archive / "partial.json").write_text("partial\n", encoding="utf-8")
            (worktree / "outside.txt").write_text("preserve\n", encoding="utf-8")
            failed = {
                "status": "failed",
                "strategy_id": "strategy-001",
                "simulation_id": "simulation-001",
            }
            return 1, {"status": "partial", "results": [result, failed]}
        if name == "out_of_scope":
            (worktree / "outside.txt").write_text("preserve\n", encoding="utf-8")
        return 0, {"status": "complete", "results": [result]}

    monkeypatch.setattr(scheduled_sync, "_run_json", run_json, raising=False)
    code, state = scheduled_sync.run_scheduled_sync(
        repository,
        python_exe=Path(sys.executable),
        cli=Path("jq_sync.py"),
    )
    worktree = runtime_root / "worktree"
    return {
        "code": code,
        "state": state,
        "calls": calls,
        "worktree": worktree,
        "baseline": baseline,
        "head": _git(worktree, "rev-parse", "HEAD"),
        "tracked_archive": worktree
        / "joinquant"
        / "strategies"
        / "strategy-001"
        / "simulations"
        / "simulation-001"
        / "manifest.json",
        "untracked_archive": worktree
        / "joinquant"
        / "strategies"
        / "strategy-001"
        / "simulations"
        / "simulation-001"
        / "partial.json",
        "out_of_scope": worktree / "outside.txt",
    }


def test_noop_does_not_create_branch_or_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run_scenario("noop", tmp_path, monkeypatch)
    assert result["code"] == 0
    assert result["state"]["status"] == "noop"
    assert result["head"] == result["baseline"]
    assert _git(result["worktree"], "branch", "--show-current") == ""


def test_valid_archive_change_creates_exact_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run_scenario("valid", tmp_path, monkeypatch)
    assert result["code"] == 0
    assert result["state"]["status"] == "complete"
    assert result["head"] != result["baseline"]
    assert _git(result["worktree"], "branch", "--show-current") == (
        "codex/joinquant-archive-auto"
    )
    assert _git(result["worktree"], "show", "--format=", "--name-only", "HEAD") == (
        "joinquant/strategies/strategy-001/simulations/simulation-001/manifest.json"
    )


def test_partial_sync_rolls_back_only_identified_archive_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run_scenario("sync_failed", tmp_path, monkeypatch)
    assert result["code"] != 0
    assert result["state"]["reason"] == "sync_failed"
    assert result["tracked_archive"].read_text(encoding="utf-8") == "baseline\n"
    assert not result["untracked_archive"].exists()
    assert result["out_of_scope"].read_text(encoding="utf-8") == "preserve\n"
    assert result["head"] == result["baseline"]


def test_out_of_scope_change_blocks_commit_and_is_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run_scenario("out_of_scope", tmp_path, monkeypatch)
    assert result["code"] != 0
    assert result["state"]["reason"] == "path_out_of_scope"
    assert result["out_of_scope"].exists()
    assert result["head"] == result["baseline"]


@pytest.mark.parametrize("name", ["verify_command_failed", "verify_gate_failed"])
def test_verify_failure_rolls_back_before_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    result = _run_scenario(name, tmp_path, monkeypatch)
    assert result["code"] != 0
    assert result["state"]["reason"] == "verify_failed"
    assert result["tracked_archive"].read_text(encoding="utf-8") == "baseline\n"
    assert result["head"] == result["baseline"]


def test_github_auth_failure_stops_before_joinquant_or_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from joinquant_sync import scheduled_sync

    repository, _ = _repository(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(
        scheduled_sync, "_discover_pr_flow", lambda: tmp_path / "pr_flow.py"
    )
    monkeypatch.setattr(
        scheduled_sync,
        "_command_ok",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            scheduled_sync.ScheduledSyncError("gh_auth_required")
        ),
        raising=False,
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        scheduled_sync,
        "_run_json",
        lambda command, **_kwargs: calls.append(command),
        raising=False,
    )
    code, state = scheduled_sync.run_scheduled_sync(
        repository, python_exe=Path(sys.executable), cli=Path("jq_sync.py")
    )
    assert code != 0
    assert state["reason"] == "gh_auth_required"
    assert calls == []


def test_joinquant_auth_failure_stops_before_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from joinquant_sync import scheduled_sync

    repository, _ = _repository(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(
        scheduled_sync, "_discover_pr_flow", lambda: tmp_path / "pr_flow.py"
    )
    monkeypatch.setattr(
        scheduled_sync, "_command_ok", lambda *_args, **_kwargs: None, raising=False
    )
    calls: list[str] = []

    def run_json(
        command: list[str], **_kwargs: object
    ) -> tuple[int, dict[str, object]]:
        calls.append(command[2])
        return 1, {"status": "auth_required"}

    monkeypatch.setattr(scheduled_sync, "_run_json", run_json, raising=False)
    code, state = scheduled_sync.run_scheduled_sync(
        repository, python_exe=Path(sys.executable), cli=Path("jq_sync.py")
    )
    assert code != 0
    assert state["reason"] == "auth_required"
    assert calls == ["auth"]
