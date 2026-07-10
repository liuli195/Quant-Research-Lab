from __future__ import annotations

import json
from pathlib import Path

import pytest


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


def test_incomplete_attribution_blocks_gate() -> None:
    from joinquant_sync.archive import evaluate_gate, expected_datasets

    datasets = expected_datasets("backtest", "done", True)
    datasets["attribution_log"].update(status="failed")
    assert evaluate_gate(datasets)["status"] == "fail"


def test_missing_writer_is_explicit_exception() -> None:
    from joinquant_sync.archive import evaluate_gate, expected_datasets

    datasets = expected_datasets("backtest", "done", False)
    assert datasets["attribution_log"]["status"] == "missing_at_source"
    gate = evaluate_gate(datasets)
    assert gate["status"] == "pass"
    assert gate["exceptions"] == ["attribution_log:missing_at_source"]


def test_missing_dataset_status_blocks_gate() -> None:
    from joinquant_sync.archive import evaluate_gate, expected_datasets

    datasets = expected_datasets("backtest", "done", True)
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
    assert evaluate_gate(datasets)["status"] == "pass"


def test_capped_free_log_is_visible_but_does_not_fail_gate() -> None:
    from joinquant_sync.archive import evaluate_gate, expected_datasets

    datasets = expected_datasets("backtest", "done", True)
    datasets["normal_log"].update(
        status="capped_free", pagination={"probed_offset": 1000}
    )
    gate = evaluate_gate(datasets)
    assert gate == {"status": "pass", "exceptions": ["normal_log:capped_free"]}


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
