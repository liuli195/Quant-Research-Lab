from __future__ import annotations

import json
import socket
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
    }
    assert result["elapsed_seconds"] >= 0
    assert result["peak_bytes"] > 0


def test_self_test_cli_is_repeatable(capsys: pytest.CaptureFixture[str]) -> None:
    from jq_sync import main

    assert main(["self-test"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(["self-test"]) == 0
    second = json.loads(capsys.readouterr().out)
    for field in ("gate", "idempotent", "duckdb", "csv_rows", "manifest_rows", "cases"):
        assert first[field] == second[field]


def test_self_test_never_reads_repository_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import joinquant_sync.selftest as selftest

    original = Path.read_text

    def guarded(path: Path, *args: object, **kwargs: object) -> str:
        if "joinquant" in path.parts and "strategies" in path.parts:
            raise AssertionError("repository history used")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    assert selftest.run_self_test()["gate"] == "pass"
