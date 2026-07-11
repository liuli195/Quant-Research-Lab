from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest


def test_self_test_runs_full_pipeline_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jq_sync import run_self_test

    def network_used(*args: object, **kwargs: object) -> object:
        raise AssertionError("network used")

    monkeypatch.setattr(socket, "create_connection", network_used)
    result = run_self_test()
    assert result["gate"] == "pass"
    assert result["idempotent"] is True
    assert result["duckdb"] == ":memory:"
    assert result["csv_rows"] == result["manifest_rows"]
    assert result["temporary_removed"] is True
    assert result["cases"] == {
        "run_statuses": ["cancelled", "done", "failed"],
        "normal_log_boundaries": [999, 1000, 1001],
        "attribution": [
            "complete",
            "missing_page",
            "missing_run_end",
            "missing_writer",
            "sequence_gap",
        ],
        "malformed_json": True,
        "unsupported_api_version": True,
        "production_orchestration": "committed",
    }
    assert result["elapsed_seconds"] >= 0
    assert result["peak_bytes"] > 0


def test_both_published_skill_entries_run_the_same_production_self_test(
    repo_root: Path,
) -> None:
    python = repo_root / ".venv" / "Scripts" / "python.exe"
    entries = [
        repo_root
        / ".agents"
        / "skills"
        / "joinquant-archive-sync"
        / "scripts"
        / "jq_sync.py",
        repo_root
        / ".claude"
        / "skills"
        / "joinquant-archive-sync"
        / "scripts"
        / "jq_sync.py",
    ]
    results = [
        json.loads(
            subprocess.run(
                [str(python), str(entry), "self-test"],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        )
        for entry in entries
    ]
    stable = (
        "gate",
        "idempotent",
        "duckdb",
        "csv_rows",
        "manifest_rows",
        "query_rows",
        "sync_statuses",
        "object_kind",
        "cases",
    )
    assert {field: results[0][field] for field in stable} == {
        field: results[1][field] for field in stable
    }
    assert results[0]["sync_statuses"] == ["committed", "unchanged"]
    assert results[0]["object_kind"] == "simulation"


def test_self_test_never_reads_repository_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import joinquant_sync.selftest as selftest

    original = Path.read_text
    repository_history = (Path.cwd() / "joinquant" / "strategies").resolve()

    def guarded(path: Path, *args: object, **kwargs: object) -> str:
        try:
            path.resolve().relative_to(repository_history)
        except ValueError:
            pass
        else:
            raise AssertionError("repository history used")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    assert selftest.run_self_test()["gate"] == "pass"
