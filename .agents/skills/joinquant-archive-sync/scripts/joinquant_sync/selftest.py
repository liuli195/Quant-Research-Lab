from __future__ import annotations

import json
import time
import tracemalloc
from pathlib import Path
from tempfile import TemporaryDirectory

from .archive import (
    AttributionIncomplete,
    detect_attribution_writer,
    evaluate_gate,
    expected_datasets,
    recover_malformed_json,
    validate_attribution,
)
from .browser import collect_free_logs
from .query import export_csv, query_rows
from .sync_pipeline import commit_simulation_evidence


def _complete_synthetic_datasets(
    datasets: dict[str, dict[str, object]],
) -> None:
    for name, item in datasets.items():
        if item.get("status") != "failed":
            continue
        if name == "performance_profile":
            item.update(
                status="unsupported_api_version",
                evidence={"api_version": "self-test"},
            )
        else:
            item.update(status="complete", rows=0, verified_empty=True)


def _exercise_statuses() -> list[str]:
    statuses = ["cancelled", "done", "failed"]
    for status in statuses:
        datasets = expected_datasets("backtest", status, True)
        _complete_synthetic_datasets(datasets)
        if evaluate_gate(datasets)["status"] != "pass":
            raise AssertionError(f"self-test status gate failed: {status}")
    return statuses


def _free_log_rows(total: int) -> tuple[list[dict[str, object]], str]:
    def fetch(offset: int) -> dict[str, object]:
        remaining = total - offset
        if remaining <= 0:
            return {"rows": [], "end": True}
        count = min(100, remaining)
        return {"rows": [{"seq": offset + index + 1} for index in range(count)]}

    return collect_free_logs(fetch)


def _exercise_logs() -> list[int]:
    boundaries = [999, 1000, 1001]
    for total in boundaries:
        rows, status = _free_log_rows(total)
        if len(rows) != total or status != "complete":
            raise AssertionError(f"self-test log boundary failed: {total}")
    capped_rows, capped = collect_free_logs(
        lambda offset: (
            {"rows": [{"seq": offset + index + 1} for index in range(100)]}
            if offset < 1000
            else {"rows": [], "blocked_free": True}
        )
    )
    if len(capped_rows) != 1000 or capped != "capped_free":
        raise AssertionError("self-test capped_free boundary failed")
    return boundaries


def _expect_attribution_failure(lines: list[bytes], status: str = "done") -> None:
    try:
        validate_attribution(lines, status, True)
    except AttributionIncomplete:
        return
    raise AssertionError("self-test expected attribution failure")


def _exercise_attribution() -> list[str]:
    complete = [
        b'{"token":"t","seq":1,"event":"run_start"}',
        b'{"token":"t","seq":2,"event":"run_end"}',
    ]
    if not validate_attribution(complete, "done", True)["run_end"]:
        raise AssertionError("self-test complete attribution failed")
    _expect_attribution_failure([])
    _expect_attribution_failure([b'{"token":"t","seq":1,"event":"run_start"}'])
    missing_writer = validate_attribution([], "done", False)
    if missing_writer["status"] != "missing_at_source":
        raise AssertionError("self-test missing writer failed")
    _expect_attribution_failure(
        [
            b'{"token":"t","seq":1,"event":"run_start"}',
            b'{"token":"t","seq":3,"event":"run_end"}',
        ]
    )
    return [
        "complete",
        "missing_page",
        "missing_run_end",
        "missing_writer",
        "sequence_gap",
    ]


def _simulation_evidence() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    code = "def initialize(context):\n    pass\n"
    log_record = {"offset": 0, "text": "2026-01-01 - INFO - self-test"}
    browser = {
        "normal_log": (json.dumps(log_record, separators=(",", ":")) + "\n").encode(),
        "normal_log_records": [log_record],
        "normal_log_raw_pages": [
            {
                "offset": 0,
                "response": {"data": {"logArr": [log_record["text"]]}},
            }
        ],
        "normal_log_status": "complete",
        "normal_log_rows": 1,
        "log_pages": [{"cursor": 0, "rows": 1}],
        "code": code,
        "params": {"start_date": "2026-01-01"},
        "source_backtest": "self-test-source",
        "research_id": "self-test-source",
        "source_raw": b'{"code":"self-test"}',
        "code_versions": [code],
        "code_history_versions": [
            {
                "history_ordinal": 1,
                "live_history_id": "self-test-history",
                "source_backtest_id": "self-test-source",
                "add_time": "2026-01-01 00:00:00",
                "mod_time": "2026-01-01 00:00:00",
                "code": code,
            }
        ],
        "code_history_pages": [
            {
                "data": {
                    "totalCount": 1,
                    "list": [
                        {
                            "liveHistoryId": "self-test-history",
                            "sourceBacktestId": "self-test-source",
                            "addTime": "2026-01-01 00:00:00",
                            "modTime": "2026-01-01 00:00:00",
                            "code": 0,
                        }
                    ],
                }
            }
        ],
        "code_history_total": 1,
    }
    research = {
        "bundle": {
            "metadata": {
                "schema_version": 1,
                "backtest_id": "self-test-source",
                "generated_at": "2026-01-01T00:00:00",
                "extraction_method": "joinquant_research_get_backtest",
                "incremental_after": {},
                "transfer_modes": {
                    name: "full"
                    for name in (
                        "results",
                        "positions",
                        "orders",
                        "records",
                        "balances",
                    )
                },
            },
            "params": {"start_date": "2026-01-01", "end_date": "2026-01-02"},
            "status": "running",
            "results": [
                {"id": 1, "time": "2026-01-01", "return": 0.01},
                {"id": 2, "time": "2026-01-02", "return": -0.02},
            ],
            "balances": [
                {"time": "2026-01-01", "cash": 100.0},
                {"time": "2026-01-02", "cash": 98.0},
            ],
            "positions": [],
            "orders": [],
            "records": [],
            "risk": {"sharpe": 0.5},
            "period_risks": {},
        },
        "attribution": b"",
    }
    research["raw"] = json.dumps(
        research["bundle"], ensure_ascii=False, separators=(",", ":")
    ).encode()
    candidate = {
        "local_id": "simulation-001",
        "page_space_id": "self-test-space",
        "status": "active",
        "detail_url": "memory://self-test",
        "aliases": ["self-test-space"],
        "collection_fence": {
            "collection_before_sha256": "0" * 64,
            "collection_after_sha256": "0" * 64,
        },
    }
    return candidate, browser, research, detect_attribution_writer(code)


def _exercise_orchestration(repository: Path) -> str:
    from . import sync_pipeline

    candidate, browser, _, _ = _simulation_evidence()
    candidate.update(name="self-test", page_ordinal="1")
    original = {
        name: getattr(sync_pipeline, name)
        for name in (
            "discover_all_simulations",
            "discover_active_simulations",
            "fetch_strategy_default_code",
            "fetch_simulation_browser_evidence",
            "fetch_research_backtest",
        )
    }
    try:
        sync_pipeline.discover_all_simulations = lambda _page: [dict(candidate)]
        sync_pipeline.discover_active_simulations = lambda _page: [dict(candidate)]
        sync_pipeline.fetch_strategy_default_code = lambda _page, _name: {
            "name": "self-test",
            "edit_url": "https://www.joinquant.com/algorithm/index/edit?algorithmId=self-test",
            "code": browser["code"],
        }
        sync_pipeline.fetch_simulation_browser_evidence = (
            lambda _page, _candidate, _incremental=None: dict(
                _simulation_evidence()[1]
            )
        )
        sync_pipeline.fetch_research_backtest = lambda _page, _backtest_id, **_kwargs: (
            _simulation_evidence()[2]
        )
        result = sync_pipeline.sync_all_active_simulations(object(), repository)
    finally:
        for name, value in original.items():
            setattr(sync_pipeline, name, value)
    if len(result) != 1 or result[0].get("status") != "committed":
        raise AssertionError(f"self-test production orchestration failed: {result}")
    return "committed"


def run_self_test() -> dict[str, object]:
    started = time.perf_counter()
    tracemalloc.start()
    temporary_path: Path | None = None
    result: dict[str, object]
    try:
        statuses = _exercise_statuses()
        log_boundaries = _exercise_logs()
        attribution = _exercise_attribution()
        recovered, recovery_errors = recover_malformed_json(b'{"ok":1}\nBAD\n')
        if recovered != [{"ok": 1}] or not recovery_errors:
            raise AssertionError("self-test malformed JSON recovery failed")

        with TemporaryDirectory(prefix="joinquant-self-test-") as directory:
            temporary_path = Path(directory)
            orchestration = _exercise_orchestration(
                temporary_path / "orchestrated-repository"
            )
            object_dir = temporary_path / "object"
            first = commit_simulation_evidence(
                object_dir,
                temporary_path / "stage-1",
                *_simulation_evidence(),
            )
            manifest = first["manifest"]
            first_manifest = (object_dir / "manifest.json").read_bytes()
            second = commit_simulation_evidence(
                object_dir,
                temporary_path / "stage-2",
                *_simulation_evidence(),
            )
            idempotent = first_manifest == (object_dir / "manifest.json").read_bytes()

            queried = query_rows(object_dir / "manifest.json", "results", 100)
            csv_path = temporary_path / "export" / "results.csv"
            csv = export_csv(
                object_dir / "manifest.json",
                "results",
                ["id", "time", "return"],
                None,
                None,
                csv_path,
            )
            result = {
                "gate": manifest["gate"]["status"],
                "idempotent": idempotent,
                "duckdb": ":memory:",
                "csv_rows": csv["rows"],
                "manifest_rows": manifest["datasets"]["results"]["rows"],
                "query_rows": len(queried),
                "sync_statuses": [first["status"], second["status"]],
                "object_kind": manifest["object"]["kind"],
                "cases": {
                    "run_statuses": statuses,
                    "normal_log_boundaries": log_boundaries,
                    "attribution": attribution,
                    "malformed_json": True,
                    "unsupported_api_version": True,
                    "production_orchestration": orchestration,
                },
            }
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    result["temporary_removed"] = (
        temporary_path is not None and not temporary_path.exists()
    )
    result["elapsed_seconds"] = time.perf_counter() - started
    result["peak_bytes"] = peak
    return result
