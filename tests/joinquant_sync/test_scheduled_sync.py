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
    pr_flow = repository / ".pr-flow"
    pr_flow.mkdir()
    (pr_flow / ".gitignore").write_text("/last-status.json\n", encoding="utf-8")
    _git(repository, "add", "README.md", "joinquant", ".pr-flow/.gitignore")
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


def test_prepare_worktree_rejects_worktree_from_another_repository(
    tmp_path: Path,
) -> None:
    from joinquant_sync.scheduled_sync import ScheduledSyncError, _prepare_worktree

    first, first_head = _repository(tmp_path / "first")
    second, second_head = _repository(tmp_path / "second")
    runtime_root = tmp_path / "runtime"
    _prepare_worktree(first, runtime_root)

    with pytest.raises(ScheduledSyncError, match="worktree_repository_mismatch"):
        _prepare_worktree(second, runtime_root)

    assert _git(first, "rev-parse", "HEAD") == first_head
    assert _git(second, "rev-parse", "HEAD") == second_head


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
            if name == "verify_start_failed":
                raise scheduled_sync.ScheduledSyncError("command_output_invalid")
            if name == "verify_command_failed":
                return 1, {"status": "integrity_failed"}
            if name == "verify_gate_failed":
                return 0, {"status": "verified", "gate": {"status": "fail"}}
            return 0, {"status": "verified", "gate": {"status": "pass"}}
        assert action == "sync-active-simulations"
        if name == "sync_start_failed":
            raise scheduled_sync.ScheduledSyncError("command_output_invalid")
        invalid_results = {
            "missing_results": {"status": "complete"},
            "null_results": {"status": "complete", "results": None},
            "string_results": {"status": "complete", "results": "bad"},
            "dict_results": {"status": "complete", "results": {}},
        }
        if name in invalid_results:
            return 0, invalid_results[name]
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
            "changed_paths_failed",
            "verify_command_failed",
            "verify_gate_failed",
            "verify_start_failed",
            "checks_failed",
            "pr_flow_start_failed",
        }:
            (archive / "manifest.json").write_text("changed\n", encoding="utf-8")
        if name in {
            "sync_failed",
            "changed_paths_failed",
            "rollback_restore_failed",
            "rollback_unlink_failed",
        }:
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
    if name == "changed_paths_failed":
        monkeypatch.setattr(
            scheduled_sync,
            "_changed_paths",
            lambda _worktree: (_ for _ in ()).throw(
                scheduled_sync.ScheduledSyncError("git_failed")
            ),
        )
    if name in {"rollback_restore_failed", "rollback_unlink_failed"}:

        def fail_rollback(*_args: object, **_kwargs: object) -> str:
            if name == "rollback_restore_failed":
                raise scheduled_sync.ScheduledSyncError("git_failed")
            raise OSError("denied")

        monkeypatch.setattr(scheduled_sync, "_rollback", fail_rollback)

    def run_pr_flow(
        _python_exe: Path,
        _script: Path,
        worktree: Path,
        *,
        command: str,
        pr: object = None,
    ) -> tuple[int, dict[str, object]]:
        if name == "pr_flow_start_failed":
            raise scheduled_sync.ScheduledSyncError("pr_flow_unavailable")
        if name == "checks_failed":
            return 1, {"status": "CHECKS_FAILED", "details": {"pr": 123}}
        if name == "valid":
            _git(worktree, "push", "origin", "HEAD:main")
            _git(worktree, "checkout", "--detach", "origin/main")
            _git(worktree, "branch", "-d", scheduled_sync.AUTOMATION_BRANCH)
        return 0, {"status": "cleanup_complete", "details": {"pr": pr or 123}}

    monkeypatch.setattr(scheduled_sync, "_run_pr_flow", run_pr_flow, raising=False)
    import jq_sync

    code = jq_sync.main(["scheduled-sync-pr", "--repository", str(repository)])
    state = json.loads(
        (runtime_root / "last-run.json").read_text(encoding="utf-8")
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
    assert _git(result["worktree"], "branch", "--show-current") == ""
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


@pytest.mark.parametrize(
    "name", ["missing_results", "null_results", "string_results", "dict_results"]
)
def test_invalid_sync_results_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    result = _run_scenario(name, tmp_path, monkeypatch)
    assert result["code"] != 0
    assert result["state"]["status"] == "failed"
    assert result["state"]["reason"] == "sync_failed"
    assert result["head"] == result["baseline"]


@pytest.mark.parametrize(
    ("name", "rollback_reason"),
    [
        ("rollback_restore_failed", "git_failed"),
        ("rollback_unlink_failed", "rollback_io_failed"),
    ],
)
def test_rollback_failure_preserves_original_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    rollback_reason: str,
) -> None:
    result = _run_scenario(name, tmp_path, monkeypatch)
    state = result["state"]
    assert result["code"] != 0
    assert state["phase"] == "sync"
    assert state["reason"] == "sync_failed"
    assert state["rollback_status"] == "failed"
    assert state["rollback_reason"] == rollback_reason
    assert state["run_id"]
    assert state["recovery_command"]


def test_change_collection_failure_preserves_original_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run_scenario("changed_paths_failed", tmp_path, monkeypatch)
    state = result["state"]
    assert result["code"] != 0
    assert state["phase"] == "sync"
    assert state["reason"] == "sync_failed"
    assert state["rollback_status"] == "failed"
    assert state["rollback_reason"] == "git_failed"
    assert state["recovery_command"]


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


def test_sync_start_failure_is_recorded_in_sync_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run_scenario("sync_start_failed", tmp_path, monkeypatch)
    assert result["code"] != 0
    assert result["state"]["phase"] == "sync"
    assert result["state"]["reason"] == "sync_failed"
    assert result["head"] == result["baseline"]


def test_verify_start_failure_rolls_back_in_verify_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run_scenario("verify_start_failed", tmp_path, monkeypatch)
    assert result["code"] != 0
    assert result["state"]["phase"] == "verify"
    assert result["state"]["reason"] == "verify_failed"
    assert result["tracked_archive"].read_text(encoding="utf-8") == "baseline\n"
    assert result["head"] == result["baseline"]


def test_pr_flow_start_failure_preserves_recoverable_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run_scenario("pr_flow_start_failed", tmp_path, monkeypatch)
    assert result["code"] != 0
    assert result["state"]["phase"] == "pr_flow"
    assert result["state"]["reason"] == "pr_flow_stopped"
    assert result["state"]["branch"] == "codex/joinquant-archive-auto"
    assert result["head"] != result["baseline"]


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


def test_command_start_failure_writes_complete_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from joinquant_sync import scheduled_sync

    repository, _ = _repository(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    monkeypatch.setattr(scheduled_sync, "_runtime_root", lambda: runtime_root)
    monkeypatch.setattr(
        scheduled_sync, "_discover_pr_flow", lambda: tmp_path / "pr_flow.py"
    )
    monkeypatch.setattr(
        scheduled_sync.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("not found")),
    )

    code, state = scheduled_sync.run_scheduled_sync(
        repository, python_exe=Path(sys.executable), cli=Path("jq_sync.py")
    )

    assert code != 0
    assert state["phase"] == "preflight"
    assert state["reason"] == "gh_auth_required"
    assert state["recovery_command"]
    assert json.loads((runtime_root / "last-run.json").read_text(encoding="utf-8")) == state


def test_process_helpers_convert_start_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from joinquant_sync import scheduled_sync

    monkeypatch.setattr(
        scheduled_sync.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("not found")),
    )
    with pytest.raises(scheduled_sync.ScheduledSyncError, match="git_failed"):
        scheduled_sync._git(tmp_path, "status")
    with pytest.raises(scheduled_sync.ScheduledSyncError, match="command_output_invalid"):
        scheduled_sync._run_json(["missing"])
    with pytest.raises(scheduled_sync.ScheduledSyncError, match="pr_flow_unavailable"):
        scheduled_sync._run_pr_flow(
            Path(sys.executable),
            tmp_path / "pr_flow.py",
            tmp_path,
            command="complete",
        )


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


def test_final_state_has_recovery_fields_without_external_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run_scenario("noop", tmp_path, monkeypatch)
    state = result["state"]
    assert {
        "run_id",
        "started_at",
        "finished_at",
        "phase",
        "status",
        "reason",
        "worktree",
        "branch",
        "pr",
        "recovery_command",
        "rollback_status",
    } <= state.keys()
    serialized = json.dumps(state)
    assert "token" not in serialized.lower()
    assert "cookie" not in serialized.lower()


def _run_recovery_scenario(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, dict[str, object], list[str]]:
    from joinquant_sync import scheduled_sync

    repository, _ = _repository(tmp_path)
    runtime_root = tmp_path / "runtime-root"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(scheduled_sync, "_runtime_root", lambda: runtime_root)
    worktree = scheduled_sync._prepare_worktree(repository, runtime_root)
    _git(worktree, "switch", "-c", scheduled_sync.AUTOMATION_BRANCH)
    if name == "cleanup":
        (worktree / ".pr-flow" / "last-status.json").write_text(
            json.dumps(
                {
                    "status": "EXCEPTION_REQUIRED",
                    "command": "cleanup",
                    "details": {
                        "sourceBranch": scheduled_sync.AUTOMATION_BRANCH,
                        "pr": 123,
                    },
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        scheduled_sync, "_discover_pr_flow", lambda: tmp_path / "pr_flow.py"
    )
    monkeypatch.setattr(scheduled_sync, "_command_ok", lambda *_a, **_k: None)
    sync_calls: list[list[str]] = []
    monkeypatch.setattr(
        scheduled_sync,
        "_run_json",
        lambda command, **_kwargs: sync_calls.append(command),
    )
    pr_calls: list[str] = []

    def run_pr_flow(
        *_args: object, command: str, pr: object = None, **_kwargs: object
    ) -> tuple[int, dict[str, object]]:
        pr_calls.append(f"{command}:{pr}" if pr is not None else command)
        return 0, {"status": "cleanup_complete", "details": {"pr": pr or 123}}

    monkeypatch.setattr(scheduled_sync, "_run_pr_flow", run_pr_flow, raising=False)
    code, state = scheduled_sync.run_scheduled_sync(
        repository, python_exe=Path(sys.executable), cli=Path("jq_sync.py")
    )
    assert sync_calls == []
    return code, state, pr_calls


def test_fixed_branch_without_pr_status_resumes_complete_without_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, state, calls = _run_recovery_scenario("complete", tmp_path, monkeypatch)
    assert code == 0
    assert state["status"] == "complete"
    assert calls == ["complete"]


def test_merged_cleanup_state_resumes_cleanup_without_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, state, calls = _run_recovery_scenario("cleanup", tmp_path, monkeypatch)
    assert code == 0
    assert state["status"] == "complete"
    assert calls == ["cleanup:123"]


def test_pr_flow_stop_is_recoverable_and_never_uses_alternate_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run_scenario("checks_failed", tmp_path, monkeypatch)
    assert result["code"] != 0
    assert result["state"]["reason"] == "pr_flow_stopped"
    assert result["state"]["pr"] == 123
