from __future__ import annotations

import gzip
import hashlib
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


def test_remote_alias_change_does_not_create_duplicate_directory(tmp_path: Path) -> None:
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
        evaluate_gate(expected_datasets("backtest", "done", False))["status"]
        == "fail"
    )


def test_complete_status_requires_file_or_verified_empty_evidence() -> None:
    from joinquant_sync.archive import evaluate_gate

    assert (
        evaluate_gate({"results": {"required": True, "status": "complete"}})[
            "status"
        ]
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


def test_failed_batch_keeps_previous_manifest(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError, commit_manifest

    object_dir = tmp_path / "backtests" / "1"
    object_dir.mkdir(parents=True)
    old = {"schema_version": 1, "gate": {"status": "pass"}, "datasets": {}}
    (object_dir / "manifest.json").write_text(json.dumps(old), encoding="utf-8")

    with pytest.raises(IntegrityError):
        commit_manifest(object_dir, {"gate": {"status": "fail"}}, [])

    assert json.loads((object_dir / "manifest.json").read_text()) == old


def test_raw_response_round_trips_and_hashes(tmp_path: Path) -> None:
    from joinquant_sync.archive import write_raw_gzip

    raw = b'{"x":1}'
    destination = tmp_path / "raw.json.gz"
    result = write_raw_gzip(raw, destination)

    assert gzip.decompress(destination.read_bytes()) == raw
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["compressed_sha256"] == hashlib.sha256(
        destination.read_bytes()
    ).hexdigest()


def test_atomic_commit_moves_only_manifest_referenced_files(tmp_path: Path) -> None:
    from joinquant_sync.archive import commit_manifest, verify_existing_manifest

    object_dir = tmp_path / "backtests" / "1"
    staged = tmp_path / "stage" / "chunk.json"
    staged.parent.mkdir()
    staged.write_bytes(b'{"page":1}')
    digest = hashlib.sha256(staged.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "datasets": {
            "results": {
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
    assert verify_existing_manifest(object_dir) == manifest


def test_repeated_commit_with_same_content_is_idempotent(tmp_path: Path) -> None:
    from joinquant_sync.archive import commit_manifest

    object_dir = tmp_path / "backtests" / "1"
    payload = b'{"page":1}'
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema_version": 1,
        "datasets": {
            "results": {
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
    assert simulation["path"] == "current_code.py"
    assert json.loads(
        (tmp_path / "simulation" / "source.json").read_text(encoding="utf-8")
    ) == {"backtest_id": "4"}
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
