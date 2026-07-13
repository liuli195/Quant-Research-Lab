from __future__ import annotations

import gzip
import hashlib
import zipfile
import json
from pathlib import Path

import pytest


def _mark_collected(datasets: dict[str, dict[str, object]]) -> None:
    for name, item in datasets.items():
        if item.get("status") == "missing_at_source" or item.get("verified_empty"):
            continue
        item.update(
            status="complete",
            files=[{"path": f"raw/{name}.json", "sha256": "0" * 64}],
        )


@pytest.mark.parametrize("target", [None, "", "latest", "all"])
def test_history_target_rejects_implicit_selection(target: str | None) -> None:
    from joinquant_sync.archive import TargetRequired, validate_history_target

    with pytest.raises(TargetRequired):
        validate_history_target("strategy-001", target)


def test_history_target_accepts_only_ordinal_or_detail_url() -> None:
    from joinquant_sync.archive import TargetRequired, validate_history_target

    assert validate_history_target(" strategy-001 ", "4") == ("strategy-001", "4")
    detail = (
        "https://www.joinquant.com/algorithm/backtest/detail?"
        "backtestId=121b439805c89d76b93c5ce520310c2d"
    )
    assert validate_history_target("strategy-001", detail) == (
        "strategy-001",
        detail,
    )
    with pytest.raises(TargetRequired):
        validate_history_target("strategy-001", "121b439805c89d76b93c5ce520310c2d")


def test_remote_alias_change_does_not_create_duplicate_directory(
    tmp_path: Path,
) -> None:
    from joinquant_sync.archive import resolve_local_id

    index = tmp_path / "index.json"
    first = resolve_local_id(
        index,
        "backtest",
        {
            "strategy_id": "strategy-001",
            "page_ordinal": "4",
            "remote_id": "old-id",
            "url": "https://example.test/old",
        },
    )
    second = resolve_local_id(
        index,
        "backtest",
        {
            "strategy_id": "strategy-001",
            "page_ordinal": "4",
            "remote_id": "new-id",
            "url": "https://example.test/new",
        },
    )

    saved = json.loads(index.read_text(encoding="utf-8"))
    assert first == second == "4"
    assert len(saved["objects"]) == 1
    assert saved["objects"][0]["aliases"] == [
        {"remote_id": "old-id", "url": "https://example.test/old"},
        {"remote_id": "new-id", "url": "https://example.test/new"},
    ]


def test_strategy_ids_are_allocated_once(tmp_path: Path) -> None:
    from joinquant_sync.archive import resolve_local_id

    index = tmp_path / "index.json"
    first = resolve_local_id(
        index,
        "strategy",
        {"page_ordinal": "1", "remote_id": "old"},
    )
    same = resolve_local_id(
        index,
        "strategy",
        {"page_ordinal": "1", "remote_id": "new"},
    )
    second = resolve_local_id(
        index,
        "strategy",
        {"page_ordinal": "2", "remote_id": "other"},
    )

    assert (first, same, second) == ("strategy-001", "strategy-001", "strategy-002")


def test_backtest_identity_fingerprint_conflict_is_rejected(tmp_path: Path) -> None:
    from joinquant_sync.archive import IdentityConflict, resolve_local_id

    index = tmp_path / "index.json"
    resolve_local_id(
        index,
        "backtest",
        {
            "strategy_id": "strategy-001",
            "page_ordinal": "4",
            "remote_id": "old-id",
            "fingerprint": "code-and-params-a",
        },
    )

    with pytest.raises(IdentityConflict):
        resolve_local_id(
            index,
            "backtest",
            {
                "strategy_id": "strategy-001",
                "page_ordinal": "4",
                "remote_id": "new-id",
                "fingerprint": "code-and-params-b",
            },
        )


def test_incomplete_attribution_blocks_gate() -> None:
    from joinquant_sync.archive import evaluate_gate, expected_datasets

    datasets = expected_datasets("backtest", "done", True)
    _mark_collected(datasets)
    datasets["attribution_log"].update(status="failed")
    assert evaluate_gate(datasets)["status"] == "fail"


def test_missing_writer_is_explicit_exception() -> None:
    from joinquant_sync.archive import evaluate_gate, expected_datasets

    datasets = expected_datasets("backtest", "done", False)
    _mark_collected(datasets)
    assert datasets["attribution_log"]["status"] == "missing_at_source"
    gate = evaluate_gate(datasets)
    assert gate["status"] == "pass"
    assert gate["exceptions"] == ["attribution_log:missing_at_source"]


def test_missing_dataset_status_blocks_gate() -> None:
    from joinquant_sync.archive import evaluate_gate, expected_datasets

    datasets = expected_datasets("backtest", "done", True)
    _mark_collected(datasets)
    del datasets["orders"]["status"]
    assert evaluate_gate(datasets)["status"] == "fail"


def test_failed_run_uses_verified_empty_structured_datasets() -> None:
    from joinquant_sync.archive import evaluate_gate, expected_datasets

    datasets = expected_datasets("backtest", "failed", False)
    assert datasets["results"] == {
        "required": True,
        "status": "complete",
        "rows": 0,
        "verified_empty": True,
    }
    assert datasets["error_log"]["required"] is True
    _mark_collected(datasets)
    assert evaluate_gate(datasets)["status"] == "pass"


def test_capped_free_log_is_visible_but_does_not_fail_gate() -> None:
    from joinquant_sync.archive import evaluate_gate, expected_datasets

    datasets = expected_datasets("backtest", "done", True)
    _mark_collected(datasets)
    datasets["normal_log"].update(
        status="capped_free", pagination={"probed_offset": 1000}
    )
    gate = evaluate_gate(datasets)
    assert gate == {"status": "pass", "exceptions": ["normal_log:capped_free"]}


def test_uncollected_or_empty_dataset_map_cannot_pass_gate() -> None:
    from joinquant_sync.archive import evaluate_gate, expected_datasets

    assert evaluate_gate({})["status"] == "fail"
    assert (
        evaluate_gate(expected_datasets("backtest", "done", False))["status"] == "fail"
    )


def test_complete_status_requires_file_or_verified_empty_evidence() -> None:
    from joinquant_sync.archive import evaluate_gate

    assert (
        evaluate_gate({"results": {"required": True, "status": "complete"}})["status"]
        == "fail"
    )


def test_cli_rejects_non_page_target_before_sync() -> None:
    from jq_sync import main

    assert (
        main(
            [
                "sync-backtest",
                "--strategy",
                "strategy-001",
                "--target",
                "latest",
                "--stage-only",
                ".local/poc",
            ]
        )
        == 2
    )


def test_manifest_schema_declares_required_contract(repo_root: Path) -> None:
    schema_path = (
        repo_root
        / ".agents"
        / "skills"
        / "joinquant-archive-sync"
        / "references"
        / "manifest.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["required"] == [
        "schema_version",
        "object",
        "source",
        "fence",
        "code",
        "datasets",
        "gate",
    ]
    assert schema["$defs"]["dataset"]["properties"]["status"]["enum"] == [
        "complete",
        "capped_free",
        "missing_at_source",
        "unsupported_api_version",
        "failed",
    ]
    official = schema["$defs"]["backtestOfficialSummary"]
    assert official["properties"]["files"]["items"]["properties"]["path"] == {
        "const": "data/official-summary.csv"
    }
    assert official["properties"]["evidence"]["required"] == [
        "evidence_version",
        "source",
        "encoding",
        "header",
        "rows",
        "related_datasets",
    ]
    assert schema["allOf"][1]["then"]["properties"]["datasets"]["properties"][
        "official_summary"
    ] == {"$ref": "#/$defs/backtestOfficialSummary"}


def test_failed_batch_keeps_previous_manifest(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError, commit_manifest

    object_dir = tmp_path / "backtests" / "1"
    object_dir.mkdir(parents=True)
    old = {"schema_version": 1, "gate": {"status": "pass"}, "datasets": {}}
    (object_dir / "manifest.json").write_text(json.dumps(old), encoding="utf-8")

    with pytest.raises(IntegrityError):
        commit_manifest(object_dir, {"gate": {"status": "fail"}}, [])

    assert json.loads((object_dir / "manifest.json").read_text()) == old


def test_partial_commit_is_explicit_and_verifies_referenced_files(
    tmp_path: Path,
) -> None:
    from joinquant_sync.archive import (
        IntegrityError,
        commit_manifest,
        verify_existing_manifest,
        verify_partial_manifest,
    )

    object_dir = tmp_path / "backtests" / "1"
    staged = tmp_path / "stage" / "code.py"
    staged.parent.mkdir()
    staged.write_text("def initialize(context):\n    pass\n", encoding="utf-8")
    digest = hashlib.sha256(staged.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "object": {"kind": "strategy", "local_id": "1", "status": "current"},
        "source": {
            "url": "memory://strategy",
            "aliases": [],
            "observed_at": "2026-01-01T00:00:00Z",
        },
        "fence": {"before_sha256": "0" * 64, "after_sha256": "0" * 64},
        "code": {"path": "code.py", "sha256": digest},
        "datasets": {
            "page_metadata": {
                "required": True,
                "status": "failed",
                "files": [{"path": "code.py", "sha256": digest}],
                "evidence": {"error": "incomplete"},
            }
        },
        "gate": {"status": "fail", "exceptions": []},
    }

    commit_manifest(object_dir, manifest, [staged], allow_failed_gate=True)
    (object_dir / "default_code.py").write_text(
        "def initialize(context):\n    pass\n", encoding="utf-8"
    )

    assert verify_partial_manifest(object_dir) == manifest
    with pytest.raises(IntegrityError, match="gate"):
        verify_existing_manifest(object_dir)

    (object_dir / "code.py").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(IntegrityError, match="hash mismatch"):
        verify_partial_manifest(object_dir)

    (object_dir / "code.py").write_text(
        "def initialize(context):\n    pass\n", encoding="utf-8"
    )
    manifest["datasets"]["page_metadata"].update(
        status="complete", rows=1, evidence={"retry": "complete"}
    )
    manifest["gate"] = {"status": "pass", "exceptions": []}
    commit_manifest(object_dir, manifest, [])
    assert verify_existing_manifest(object_dir) == manifest


def test_raw_response_round_trips_and_hashes(tmp_path: Path) -> None:
    from joinquant_sync.archive import write_raw_gzip

    raw = b'{"x":1}'
    destination = tmp_path / "raw.json.gz"
    result = write_raw_gzip(raw, destination)

    assert gzip.decompress(destination.read_bytes()) == raw
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert (
        result["compressed_sha256"]
        == hashlib.sha256(destination.read_bytes()).hexdigest()
    )


def test_atomic_commit_moves_only_manifest_referenced_files(tmp_path: Path) -> None:
    from joinquant_sync.archive import commit_manifest, verify_existing_manifest

    object_dir = tmp_path / "backtests" / "1"
    staged = tmp_path / "stage" / "chunk.json"
    staged.parent.mkdir()
    staged.write_bytes(b'{"page":1}')
    digest = hashlib.sha256(staged.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "object": {"kind": "strategy", "local_id": "1", "status": "current"},
        "source": {
            "url": "memory://strategy",
            "aliases": [],
            "observed_at": "2026-01-01T00:00:00Z",
        },
        "fence": {"before_sha256": "0" * 64, "after_sha256": "0" * 64},
        "code": {"path": "raw/chunk.json", "sha256": digest},
        "datasets": {
            "page_metadata": {
                "required": True,
                "status": "complete",
                "files": [{"path": "raw/chunk.json", "sha256": digest}],
            }
        },
        "gate": {"status": "pass", "exceptions": []},
    }

    commit_manifest(object_dir, manifest, [staged])

    assert not staged.exists()
    assert (object_dir / "raw" / "chunk.json").read_bytes() == b'{"page":1}'
    (object_dir / "default_code.py").write_bytes(b'{"page":1}')
    assert verify_existing_manifest(object_dir) == manifest


def test_repeated_commit_with_same_content_is_idempotent(tmp_path: Path) -> None:
    from joinquant_sync.archive import commit_manifest

    object_dir = tmp_path / "backtests" / "1"
    payload = b'{"page":1}'
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema_version": 1,
        "object": {"kind": "strategy", "local_id": "1", "status": "current"},
        "source": {
            "url": "memory://strategy",
            "aliases": [],
            "observed_at": "2026-01-01T00:00:00Z",
        },
        "fence": {"before_sha256": "0" * 64, "after_sha256": "0" * 64},
        "code": {"path": "raw/chunk.json", "sha256": digest},
        "datasets": {
            "page_metadata": {
                "required": True,
                "status": "complete",
                "files": [{"path": "raw/chunk.json", "sha256": digest}],
            }
        },
        "gate": {"status": "pass", "exceptions": []},
    }
    for stage_name in ("stage-a", "stage-b"):
        staged = tmp_path / stage_name / "chunk.json"
        staged.parent.mkdir()
        staged.write_bytes(payload)
        commit_manifest(object_dir, manifest, [staged])
    assert (object_dir / "raw" / "chunk.json").read_bytes() == payload


def test_incremental_commit_distinguishes_same_named_partitions(tmp_path: Path) -> None:
    from joinquant_sync.archive import commit_manifest

    object_dir = tmp_path / "simulation"
    old = object_dir / "snapshots" / "old" / "data" / "results.parquet"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    stage_root = tmp_path / "stage"
    new = stage_root / "snapshots" / "new" / "data" / "results.parquet"
    new.parent.mkdir(parents=True)
    new.write_bytes(b"new")
    files = [
        {
            "path": "snapshots/old/data/results.parquet",
            "sha256": hashlib.sha256(b"old").hexdigest(),
        },
        {
            "path": "snapshots/new/data/results.parquet",
            "sha256": hashlib.sha256(b"new").hexdigest(),
        },
    ]
    manifest = {
        "schema_version": 1,
        "object": {"kind": "strategy", "local_id": "1", "status": "current"},
        "source": {
            "url": "memory://strategy",
            "aliases": [],
            "observed_at": "2026-01-01T00:00:00Z",
        },
        "fence": {"before_sha256": "0" * 64, "after_sha256": "0" * 64},
        "code": {
            "path": "snapshots/old/data/results.parquet",
            "sha256": hashlib.sha256(b"old").hexdigest(),
        },
        "datasets": {
            "page_metadata": {
                "required": True,
                "status": "complete",
                "files": files,
            }
        },
        "gate": {"status": "pass", "exceptions": []},
    }
    commit_manifest(object_dir, manifest, [new])
    assert (object_dir / files[1]["path"]).read_bytes() == b"new"


def test_existing_manifest_rejects_missing_referenced_file(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError, verify_existing_manifest

    object_dir = tmp_path / "backtests" / "1"
    object_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "datasets": {
            "results": {
                "required": True,
                "status": "complete",
                "files": [{"path": "raw/missing.json", "sha256": "0" * 64}],
            }
        },
        "gate": {"status": "pass", "exceptions": []},
    }
    (object_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(IntegrityError):
        verify_existing_manifest(object_dir)


def test_verify_rejects_manifest_missing_schema_contract(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError, verify_existing_manifest

    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "datasets": {
                    "fake": {
                        "required": False,
                        "status": "complete",
                        "rows": 0,
                        "verified_empty": True,
                    }
                },
                "gate": {"status": "pass", "exceptions": []},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(IntegrityError, match="manifest required field"):
        verify_existing_manifest(tmp_path)


def test_object_lock_conflict_is_retryable(tmp_path: Path) -> None:
    from joinquant_sync.archive import ObjectLocked, object_lock

    object_dir = tmp_path / "backtests" / "1"
    with object_lock(object_dir):
        with pytest.raises(ObjectLocked):
            with object_lock(object_dir):
                pass


def test_code_context_keeps_backtest_code_and_simulation_versions(
    tmp_path: Path,
) -> None:
    from joinquant_sync.archive import write_code_context

    backtest = write_code_context(
        tmp_path / "backtest",
        "backtest",
        "def initialize(context):\n    pass\n",
        {"start_date": "2026-01-01"},
    )
    simulation = write_code_context(
        tmp_path / "simulation",
        "simulation",
        "def initialize(context):\n    pass\n",
        {"frequency": "day"},
        source_backtest="4",
        versions=["# old\n", "def initialize(context):\n    pass\n"],
    )

    assert (tmp_path / "backtest" / "code.py").read_text(encoding="utf-8") == (
        "def initialize(context):\n    pass\n"
    )
    assert backtest["path"] == "code.py"
    assert (tmp_path / "backtest" / "params.json").is_file()
    assert backtest["params"]["path"] == "params.json"
    assert len(backtest["params"]["sha256"]) == 64
    assert simulation["path"] == "current_code.py"
    assert json.loads(
        (tmp_path / "simulation" / "source.json").read_text(encoding="utf-8")
    ) == {"backtest_id": "4"}
    assert simulation["source"]["path"] == "source.json"
    assert len(list((tmp_path / "simulation" / "code_versions").glob("*.py"))) == 2


def test_attribution_requires_contiguous_sequence_and_run_end() -> None:
    from joinquant_sync.archive import AttributionIncomplete, validate_attribution

    lines = [
        b'{"token":"t","seq":1,"event":"run_start"}',
        b'{"token":"t","seq":3,"event":"run_end"}',
    ]
    with pytest.raises(AttributionIncomplete):
        validate_attribution(lines, "done", True)


def test_active_simulation_may_lack_run_end() -> None:
    from joinquant_sync.archive import validate_attribution

    lines = [b'{"token":"t","seq":1,"event":"run_start"}']
    result = validate_attribution(lines, "active", True)
    assert result["status"] == "complete"
    assert result["last_seq"] == 1


def test_attribution_must_match_expected_run_identity() -> None:
    from joinquant_sync.archive import AttributionIncomplete, validate_attribution

    lines = [
        b'{"token":"run-a","seq":1,"event":"run_start","current_dt":"2026-01-02T00:00:00"}'
    ]
    with pytest.raises(AttributionIncomplete, match="expected token"):
        validate_attribution(
            lines,
            "active",
            True,
            expected_token="run-b",
            expected_path="audit/run-b.jsonl",
            expected_start="2026-01-02",
        )
    with pytest.raises(AttributionIncomplete, match="start time"):
        validate_attribution(
            lines,
            "active",
            True,
            expected_token="run-a",
            expected_path="audit/run-a.jsonl",
            expected_start="2026-01-03",
        )


def test_backtest_attribution_must_match_research_final_balance() -> None:
    import inspect

    from joinquant_sync.archive import AttributionIncomplete, validate_attribution

    assert (
        "expected_final_balance" in inspect.signature(validate_attribution).parameters
    )
    lines = [
        b'{"token":"run-a","seq":1,"event":"run_start","current_dt":"2026-01-01"}',
        b'{"token":"run-a","seq":2,"event":"run_end","current_dt":"2026-01-31","total_value":101.0,"cash":51.0}',
    ]
    with pytest.raises(AttributionIncomplete, match="final balance"):
        validate_attribution(
            lines,
            "done",
            True,
            expected_token="run-a",
            expected_path="audit/run-a.jsonl",
            expected_start="2026-01-01",
            expected_end="2026-01-31",
            expected_final_balance={"total_value": 100.0, "cash": 50.0},
        )


def test_attribution_path_must_name_the_expected_token() -> None:
    from joinquant_sync.archive import AttributionIncomplete, validate_attribution

    lines = [b'{"token":"run-a","seq":1,"event":"run_start"}']
    with pytest.raises(AttributionIncomplete, match="path token"):
        validate_attribution(
            lines,
            "active",
            True,
            expected_token="run-a",
            expected_path="audit/other.jsonl",
        )


def test_attribution_rejects_nonmonotonic_or_out_of_range_middle_time() -> None:
    from joinquant_sync.archive import AttributionIncomplete, validate_attribution

    lines = [
        b'{"token":"run-a","seq":1,"event":"run_start","current_dt":"2026-01-01"}',
        b'{"token":"run-a","seq":2,"event":"step","current_dt":"2099-01-01"}',
        b'{"token":"run-a","seq":3,"event":"run_end","current_dt":"2026-01-31"}',
    ]
    with pytest.raises(AttributionIncomplete, match="time"):
        validate_attribution(
            lines,
            "done",
            True,
            expected_token="run-a",
            expected_path="audit/run-a.jsonl",
            expected_start="2026-01-01",
            expected_end="2026-01-31",
        )


@pytest.mark.parametrize(
    "line",
    [
        b'{"token":"","seq":1,"event":"run_start"}',
        b'{"token":"t","seq":true,"event":"run_start"}',
    ],
)
def test_attribution_rejects_empty_token_or_non_integer_sequence(line: bytes) -> None:
    from joinquant_sync.archive import AttributionIncomplete, validate_attribution

    with pytest.raises(AttributionIncomplete):
        validate_attribution([line], "active", True)


def test_no_attribution_writer_is_explicit_missing_source() -> None:
    from joinquant_sync.archive import validate_attribution

    assert validate_attribution([], "done", False) == {
        "required": False,
        "status": "missing_at_source",
        "rows": 0,
        "evidence": {"code_writer": False},
    }


def test_malformed_json_recovery_reports_exact_bad_offset() -> None:
    from joinquant_sync.archive import recover_malformed_json

    rows, errors = recover_malformed_json(b'{"a":1}\nBAD\n{"a":2}\n')
    assert rows == [{"a": 1}, {"a": 2}]
    assert errors == [{"offset": 8, "bytes": 3, "error": "invalid_json"}]


def test_log_response_is_archived_before_recovery(tmp_path: Path) -> None:
    from joinquant_sync.archive import archive_log_response

    raw = b'{"a":1}\nBAD\n'
    result = archive_log_response(raw, tmp_path / "raw" / "logs.jsonl.gz")

    assert gzip.decompress((tmp_path / "raw" / "logs.jsonl.gz").read_bytes()) == raw
    assert result["rows"] == [{"a": 1}]
    assert result["recovery"] == {
        "source_lines": 2,
        "recovered_rows": 1,
        "errors": [{"offset": 8, "bytes": 3, "error": "invalid_json"}],
    }


def _simulation_streams(suffix: str) -> dict[str, dict[str, str]]:
    return {
        name: {
            "cursor": f"{name}-{suffix}",
            "sha256": hashlib.sha256(f"{name}-{suffix}".encode()).hexdigest(),
        }
        for name in ("code", "snapshots", "data", "logs")
    }


def test_simulations_advance_independent_cursors() -> None:
    from joinquant_sync.archive import sync_active_simulations

    simulations = [
        {
            "id": "sim-1",
            "tracking": "active",
            "object": {"status": "active"},
            "streams": _simulation_streams("old-1"),
        },
        {
            "id": "sim-2",
            "tracking": "active",
            "object": {"status": "active"},
            "streams": _simulation_streams("old-2"),
        },
    ]

    def fetch_remote(item: dict[str, object]) -> dict[str, object]:
        if item["id"] == "sim-2":
            raise ConnectionError("offline")
        return {
            "status": "active",
            "streams": _simulation_streams("new-1"),
            "writer_present": False,
            "attribution_lines": [],
        }

    results = sync_active_simulations(simulations, fetch_remote)
    assert results[0]["committed"] is True
    assert results[0]["manifest"]["streams"]["logs"]["cursor"] == "logs-new-1"
    assert results[1]["committed"] is False
    assert results[1]["manifest"]["streams"] == _simulation_streams("old-2")
    assert results[1]["resume"]["logs"] == "logs-old-2"
    assert simulations[0]["streams"] == _simulation_streams("old-1")


def test_same_cursor_changed_digest_is_an_increment() -> None:
    from joinquant_sync.archive import next_increment

    current = _simulation_streams("same")
    remote = _simulation_streams("same")
    remote["logs"]["sha256"] = hashlib.sha256(b"changed").hexdigest()
    increment = next_increment(
        {"streams": current}, {"status": "active", "streams": remote}
    )
    assert increment["changed"] == ["logs"]
    assert increment["requests"]["logs"]["after"] == "logs-same"


def test_closed_simulation_requires_one_final_sync() -> None:
    from joinquant_sync.archive import finalize_closed_simulation

    manifest = {
        "object": {"status": "active"},
        "tracking": "active",
        "streams": _simulation_streams("old"),
    }
    remote = {
        "status": "closed",
        "streams": _simulation_streams("final"),
        "writer_present": True,
        "attribution_lines": [
            b'{"token":"t","seq":1,"event":"run_start"}',
            b'{"token":"t","seq":2,"event":"run_end"}',
        ],
    }
    result = finalize_closed_simulation(manifest, remote)
    assert result["tracking"] == "stopped"
    assert result["final_sync"] == "complete"
    assert result["object"]["status"] == "closed"
    assert result["attribution"]["run_end"] is True


def test_closed_simulation_with_writer_rejects_missing_run_end() -> None:
    from joinquant_sync.archive import AttributionIncomplete, finalize_closed_simulation

    manifest = {
        "object": {"status": "active"},
        "tracking": "active",
        "streams": _simulation_streams("old"),
    }
    remote = {
        "status": "closed",
        "streams": _simulation_streams("final"),
        "writer_present": True,
        "attribution_lines": [b'{"token":"t","seq":1,"event":"run_start"}'],
    }
    with pytest.raises(AttributionIncomplete):
        finalize_closed_simulation(manifest, remote)


def test_attribution_writer_path_is_derived_from_literal_token_and_directory() -> None:
    from joinquant_sync.archive import detect_attribution_writer

    code = """
JQ_AUTO_AUDIT_TOKEN = "run-123"
JQ_AUTO_AUDIT_DIR = "jq_auto_audit"
def audit_event(event):
    write_file(g.audit_path, event, append=True)
"""
    assert detect_attribution_writer(code) == {
        "writer_present": True,
        "path": "jq_auto_audit/run-123.jsonl",
        "evidence": {"token": "run-123", "directory": "jq_auto_audit"},
    }


def test_simulation_attribution_writer_comes_from_lifecycle_start_code() -> None:
    import joinquant_sync.archive as archive

    selector = getattr(archive, "detect_simulation_attribution_writer", None)
    assert selector is not None
    old_code = """
JQ_AUTO_AUDIT_TOKEN = "run-start"
JQ_AUTO_AUDIT_DIR = "audit"
def audit_event(event):
    write_file(g.audit_path, event, append=True)
"""
    current_code = old_code.replace("run-start", "historical-backtest")
    earlier_code = old_code.replace("run-start", "older-experiment")
    writer = selector(
        [
            {
                "history_ordinal": 1,
                "add_time": "2026-07-10 16:06:38",
                "code": current_code,
            },
            {
                "history_ordinal": 2,
                "add_time": "2026-05-18 16:20:38",
                "code": old_code,
            },
            {
                "history_ordinal": 3,
                "add_time": "2026-05-01 12:00:00",
                "code": earlier_code,
            },
        ],
        start_date="2026-05-19",
    )

    assert writer["path"] == "audit/run-start.jsonl"
    assert writer["evidence"]["history_ordinal"] == 2


def test_code_without_attribution_writer_is_missing_at_source() -> None:
    from joinquant_sync.archive import detect_attribution_writer

    assert detect_attribution_writer("def initialize(context):\n    pass\n") == {
        "writer_present": False,
        "path": "",
        "evidence": {"code_writer": False},
    }


def test_ambiguous_attribution_write_signal_fails_closed() -> None:
    from joinquant_sync.archive import IntegrityError, detect_attribution_writer

    code = """
def persist_event(event):
    write_file("audit/run.jsonl", event, append=True)
"""
    with pytest.raises(
        IntegrityError, match="attribution writer evidence is ambiguous"
    ):
        detect_attribution_writer(code)


def test_performance_profile_is_missing_when_code_does_not_enable_it() -> None:
    from joinquant_sync.archive import classify_performance_profile

    result = classify_performance_profile("def initialize(context):\n    pass\n")

    assert result["status"] == "missing_at_source"
    assert result["evidence"]["enable_profile_call"] is False


def test_performance_profile_fails_closed_when_enabled_but_not_collected() -> None:
    from joinquant_sync.archive import classify_performance_profile

    result = classify_performance_profile(
        "enable_profile()\n\ndef initialize(context):\n    pass\n"
    )

    assert result["status"] == "failed"
    assert result["evidence"]["enable_profile_call"] is True


def test_performance_profile_is_complete_when_page_payload_is_collected() -> None:
    from joinquant_sync.archive import classify_performance_profile

    result = classify_performance_profile(
        "enable_profile()\n",
        payload=(
            b"Timer unit: 1e-06 s\nTotal time: 1 s\n"
            b"Line # Hits Time Per Hit % Time Line Contents\n"
        ),
        surface_supported=True,
    )

    assert result["status"] == "complete"
    assert result["rows"] == 3


def test_performance_profile_is_unsupported_only_with_page_capability_evidence() -> (
    None
):
    from joinquant_sync.archive import classify_performance_profile

    result = classify_performance_profile(
        "enable_profile()\n", payload=b"", surface_supported=False
    )

    assert result["status"] == "unsupported_api_version"
    assert result["evidence"]["profile_surface_supported"] is False


@pytest.mark.parametrize(
    ("code", "payload"),
    [
        ("def initialize(context):\n    pass\n", b"Timer unit: 1e-06 s\n"),
        ("enable_profile()\n", b"No performance data available"),
    ],
)
def test_performance_profile_rejects_unproven_or_placeholder_payload(
    code: str, payload: bytes
) -> None:
    from joinquant_sync.archive import classify_performance_profile

    result = classify_performance_profile(code, payload=payload, surface_supported=True)

    assert result["status"] == "failed"


def test_verify_cli_checks_manifest_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import jq_sync

    (tmp_path / "manifest.json").write_text(
        '{"schema_version":1,"gate":{"status":"fail"},"datasets":{}}',
        encoding="utf-8",
    )
    assert jq_sync.main(["verify", "--object", str(tmp_path)]) == 3
    assert "integrity_failed" in capsys.readouterr().out


def test_verify_cli_reports_a_valid_partial_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import jq_sync

    partial = {
        "gate": {"status": "fail", "exceptions": []},
        "datasets": {"normal_log": {"status": "failed"}},
    }
    monkeypatch.setattr(
        jq_sync,
        "verify_existing_manifest",
        lambda _path: (_ for _ in ()).throw(jq_sync.IntegrityError("gate failed")),
    )
    monkeypatch.setattr(jq_sync, "verify_partial_manifest", lambda _path: partial)

    assert jq_sync.main(["verify", "--object", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "partial"


def test_paid_log_range_keeps_only_requested_lines(tmp_path: Path) -> None:
    from joinquant_sync.archive import extract_paid_log_range

    archive = tmp_path / "log.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("log.txt", b"zero\none\ntwo\nthree\n")
    result = extract_paid_log_range(archive, "1:3", tmp_path / "selected.jsonl.gz")
    import gzip

    with gzip.open(result["path"], "rb") as stream:
        assert stream.read() == b"one\ntwo\n"
    assert result["actual_range"] == "1:3"


def test_paid_log_download_requires_explicit_confirmation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import jq_sync

    assert (
        jq_sync.main(
            [
                "paid-log",
                "download",
                "--preview-id",
                "0" * 32,
                "--destination",
                str(tmp_path / "selected.gz"),
            ]
        )
        == 4
    )
    assert "confirmation_required" in capsys.readouterr().out
