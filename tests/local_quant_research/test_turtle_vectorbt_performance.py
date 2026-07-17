from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path

import pytest


REFERENCE_SCENARIOS = (
    "immediate-11-etf",
    "immediate-17-etf",
    "delayed-11-etf-1d",
)
_REQUIRED_CLI_SAMPLE_FIELDS = {
    "scenario",
    "sample_type",
    "sample_index",
    "pid",
    "run_id",
    "package_sha256",
    "reused",
    "post_publish_validation",
    "cold_cli_total_seconds",
}


def test_release_performance_baseline_has_v2_engine_and_cli_samples(
    repo_root: Path,
) -> None:
    fixture = json.loads(
        (
            repo_root
            / "tests/local_quant_research/fixtures/performance-baseline.json"
        ).read_text(encoding="utf-8")
    )

    assert fixture["protocol_version"] == "local-research-release/2"
    environment_bytes = json.dumps(
        fixture["environment"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert fixture["environment_identity_sha256"] == hashlib.sha256(
        environment_bytes
    ).hexdigest()
    assert set(fixture["environment"]) == {
        "architecture",
        "dependencies",
        "logical_cpu_count",
        "os",
        "os_release",
        "os_version",
        "physical_memory_bytes",
        "processor",
        "python",
    }
    assert tuple(fixture["scenarios"]) == REFERENCE_SCENARIOS
    assert fixture["sampling"] == {
        "cold_processes": 3,
        "full_cli_cold_processes": 3,
        "statistic": "median",
        "warm_runs": 5,
    }
    for scenario in REFERENCE_SCENARIOS:
        metrics = fixture["scenarios"][scenario]
        cold_samples = metrics["cold_process"]["samples"]
        warm_samples = metrics["warm"]["samples"]
        cli_samples = metrics["full_cli_cold"]["samples"]

        assert len(cold_samples) == 3
        assert len(warm_samples) == 5
        assert len(cli_samples) == 3
        assert len({sample["process_id"] for sample in cold_samples}) == 3
        assert len({sample["process_id"] for sample in warm_samples}) == 1
        assert [sample["sample_index"] for sample in cli_samples] == [1, 2, 3]
        assert len({sample["pid"] for sample in cli_samples}) == 3
        assert all(set(sample) == _REQUIRED_CLI_SAMPLE_FIELDS for sample in cli_samples)
        assert all(sample["scenario"] == scenario for sample in cli_samples)
        assert all(sample["sample_type"] == "full_cli_cold" for sample in cli_samples)
        assert all(sample["reused"] is False for sample in cli_samples)
        assert all(
            sample["post_publish_validation"] == "passed"
            for sample in cli_samples
        )
        assert all(len(sample["package_sha256"]) == 64 for sample in cli_samples)
        assert all(sample["cold_cli_total_seconds"] > 0.0 for sample in cli_samples)
        assert metrics["full_cli_cold"]["median_seconds"] == pytest.approx(
            statistics.median(
                sample["cold_cli_total_seconds"] for sample in cli_samples
            )
        )
        assert metrics["parquet_payload_bytes"] > 0
        assert metrics["fixed_files_bytes"] > 0
        assert metrics["collected_files"]
        assert metrics["cleanup"] == {
            "isolated_output_roots_removed": True,
            "verified": True,
        }
