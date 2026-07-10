from __future__ import annotations

import json
import time
import tracemalloc
from pathlib import Path
from tempfile import TemporaryDirectory

from .archive import (
    AttributionIncomplete,
    commit_manifest,
    evaluate_gate,
    expected_datasets,
    recover_malformed_json,
    stage_external_file,
    validate_attribution,
    write_code_context,
    write_raw_gzip,
)
from .browser import collect_free_logs
from .query import export_csv, query_rows, write_parquet


def _manifest_file(
    record: dict[str, object], relative: str, format_: str
) -> dict[str, object]:
    digest = record.get("compressed_sha256", record.get("sha256"))
    return {
        "path": relative,
        "sha256": digest,
        "bytes": record["bytes"],
        "format": format_,
    }


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
    _expect_attribution_failure(
        [b'{"token":"t","seq":1,"event":"run_start"}']
    )
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


def _build_batch(
    stage: Path, rows: list[dict[str, object]]
) -> tuple[dict[str, object], list[Path]]:
    code = write_code_context(
        stage,
        "backtest",
        "def initialize(context):\n    pass\n",
        {"start_date": "2026-01-01", "end_date": "2026-01-02"},
    )
    parquet = write_parquet(rows, stage / "data" / "results.parquet", root=stage)
    raw = write_raw_gzip(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        stage / "raw" / "results.json.gz",
    )
    normal_log = write_raw_gzip(
        b"2026-01-01 - INFO - self-test\n",
        stage / "raw" / "normal-log.jsonl.gz",
    )

    datasets = expected_datasets("backtest", "done", False)
    _complete_synthetic_datasets(datasets)
    datasets["results"] = {
        "required": True,
        "status": "complete",
        "rows": len(rows),
        "files": [
            parquet,
            _manifest_file(raw, "raw/results.json.gz", "json.gz"),
        ],
    }
    datasets["normal_log"] = {
        "required": False,
        "status": "complete",
        "rows": 1,
        "files": [
            _manifest_file(normal_log, "raw/normal-log.jsonl.gz", "jsonl.gz")
        ],
        "pagination": {"complete": True, "probe_after": 1},
    }
    gate = evaluate_gate(datasets)
    if gate["status"] != "pass":
        raise AssertionError("self-test manifest gate failed")
    manifest = {
        "schema_version": 1,
        "object": {"kind": "backtest", "local_id": "1", "status": "done"},
        "source": {"url": "memory://self-test", "aliases": []},
        "fence": {"before_sha256": "0" * 64, "after_sha256": "0" * 64},
        "code": {
            "path": "code.py",
            "sha256": code["sha256"],
            "bytes": code["bytes"],
        },
        "datasets": datasets,
        "gate": gate,
    }
    staged = [
        stage / "code.py",
        stage / "data" / "results.parquet",
        stage / "raw" / "results.json.gz",
        stage / "raw" / "normal-log.jsonl.gz",
    ]
    return manifest, staged


def _restage_committed(object_dir: Path, stage: Path, manifest: dict[str, object]) -> list[Path]:
    relative_paths = [str(manifest["code"]["path"])]
    for dataset in manifest["datasets"].values():
        relative_paths.extend(
            str(item["path"])
            for item in dataset.get("files") or []
            if isinstance(item, dict)
        )
    staged: list[Path] = []
    for relative in relative_paths:
        record = stage_external_file(object_dir / relative, stage)
        staged.append(Path(str(record["path"])))
    return staged


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
            object_dir = temporary_path / "object"
            rows = [
                {"id": 1, "time": "2026-01-01", "return": 0.01},
                {"id": 2, "time": "2026-01-02", "return": -0.02},
            ]
            manifest, staged = _build_batch(temporary_path / "stage-1", rows)
            commit_manifest(object_dir, manifest, staged)
            first_manifest = (object_dir / "manifest.json").read_bytes()

            staged_again = _restage_committed(
                object_dir, temporary_path / "stage-2", manifest
            )
            commit_manifest(object_dir, manifest, staged_again)
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
                "cases": {
                    "run_statuses": statuses,
                    "normal_log_boundaries": log_boundaries,
                    "attribution": attribution,
                    "malformed_json": True,
                    "unsupported_api_version": True,
                },
            }
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    result["temporary_removed"] = temporary_path is not None and not temporary_path.exists()
    result["elapsed_seconds"] = time.perf_counter() - started
    result["peak_bytes"] = peak
    return result
