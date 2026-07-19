from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quant_analysis.source_registry import (
    SourceRegistryError,
    load_source_registry,
)
from scripts.research.local_quant_research.contracts import ResultExtension
from tests.local_quant_research.test_analysis_data_views import _write_result_package


SNAPSHOT_ID = "5cc582a778eca2ddc481282b"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(
    root: Path,
    source: Path,
    scenario_id: str,
    source_type: str,
    *,
    snapshot_id: str | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "scenario_id": scenario_id,
        "path": source.relative_to(root).as_posix(),
        "source_type": source_type,
        "manifest_sha256": _sha256(source / "manifest.json"),
    }
    if snapshot_id is not None:
        entry["snapshot_id"] = snapshot_id
    return entry


def _registry(root: Path, entries: list[dict[str, object]]) -> Path:
    (root / "config").mkdir()
    (root / "config/analysis-plan.json").write_text("{}", encoding="utf-8")
    (root / "config/benchmark-manifest.json").write_text("{}", encoding="utf-8")
    path = root / "source-registry.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "standard-analysis-source-registry/1",
                "analysis_plan": "config/analysis-plan.json",
                "benchmark_manifest": "config/benchmark-manifest.json",
                "baseline_scenario_id": "baseline",
                "sources": entries,
            }
        ),
        encoding="utf-8",
    )
    return path


def _prepared_sources(
    repo_root: Path, tmp_path: Path
) -> tuple[Path, list[dict[str, object]]]:
    root = tmp_path / "repository"
    root.mkdir()
    local = root / "sources/local"
    local.parent.mkdir()
    local = _write_result_package(local)
    backtest = root / "sources/backtest"
    simulation = root / "sources/simulation"
    shutil.copytree(
        repo_root / "joinquant/strategies/strategy-001/backtests/111", backtest
    )
    shutil.copytree(
        repo_root / "joinquant/strategies/strategy-001/simulations/simulation-001",
        simulation,
    )
    return root, [
        _entry(root, local, "baseline", "local_research"),
        _entry(root, backtest, "backtest", "joinquant_backtest"),
        _entry(
            root,
            simulation,
            "simulation",
            "joinquant_simulation",
            snapshot_id=SNAPSHOT_ID,
        ),
    ]


def test_registry_opens_only_three_explicit_source_kinds(
    repo_root: Path, tmp_path: Path
) -> None:
    root, entries = _prepared_sources(repo_root, tmp_path)

    registry = load_source_registry(
        root,
        _registry(root, entries),
    )

    assert [item.source.kind for item in registry.sources] == [
        "local_research",
        "joinquant_backtest",
        "joinquant_simulation",
    ]
    assert registry.sources[0].capabilities["official_risk"]["status"] == (
        "missing_at_source"
    )
    assert registry.sources[2].capabilities["official_risk"][
        "source_only_extra_fields"
    ] == ["intraday_return", "monthly_return"]
    for source in registry.sources[1:]:
        attribution = source.capabilities["attribution"]
        assert attribution["status"] == "available"
        assert attribution["time_field"] == "current_dt"
        assert attribution["event_field"] == "event"
        assert attribution["rows"] > 0
        assert attribution["time_range"]["start"] <= attribution["time_range"]["end"]


def test_registry_marks_tampered_attribution_evidence_insufficient(
    repo_root: Path, tmp_path: Path
) -> None:
    root, entries = _prepared_sources(repo_root, tmp_path)
    backtest = root / "sources/backtest"
    attribution_path = next(
        (backtest / str(reference["path"]))
        for reference in json.loads(
            (backtest / "manifest.json").read_text(encoding="utf-8")
        )["datasets"]["attribution_log"]["files"]
        if reference["format"] == "parquet"
    )
    attribution_path.write_bytes(attribution_path.read_bytes() + b"drift")

    registry = load_source_registry(root, _registry(root, entries))

    attribution = registry.sources[1].capabilities["attribution"]
    assert attribution["status"] == "evidence_insufficient"
    assert attribution["reason"] == "digest_or_size_mismatch"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("seq", 1, "invalid_event_sequence"),
        ("audit_token", "another-run", "invalid_event_token"),
        ("current_dt", "2099-01-01T00:00:00", "event_time_range_mismatch"),
    ],
)
def test_registry_degrades_invalid_joinquant_event_identity_or_range(
    repo_root: Path,
    tmp_path: Path,
    field: str,
    value: object,
    reason: str,
) -> None:
    root, entries = _prepared_sources(repo_root, tmp_path)
    backtest = root / "sources/backtest"
    manifest_path = backtest / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reference = next(
        item
        for item in manifest["datasets"]["attribution_log"]["files"]
        if item["format"] == "parquet"
    )
    path = backtest / reference["path"]
    table = pq.read_table(path)
    index = table.schema.get_field_index(field)
    values = table[field].to_pylist()
    values[-1] = value
    table = table.set_column(
        index,
        table.schema.field(index),
        pa.array(values, type=table.schema.field(index).type),
    )
    pq.write_table(table, path, compression="zstd")
    reference.update(
        sha256=_sha256(path), bytes=path.stat().st_size, rows=table.num_rows
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    entries[1] = _entry(root, backtest, "backtest", "joinquant_backtest")

    registry = load_source_registry(root, _registry(root, entries))

    attribution = registry.sources[1].capabilities["attribution"]
    assert attribution["status"] == "evidence_insufficient"
    assert attribution["reason"] == reason


def test_registry_degrades_invalid_local_attribution_event_id(
    repo_root: Path, tmp_path: Path
) -> None:
    root, entries = _prepared_sources(repo_root, tmp_path)
    local = root / "sources/local"
    shutil.rmtree(local)
    _write_result_package(
        local,
        extensions=(
            ResultExtension(
                name="attribution_log",
                schema_version="attribution-log/1",
                table=pa.table(
                    {
                        "time": [
                            "2024-01-02 16:00:00",
                            "2024-01-03 16:00:00",
                        ],
                        "event_id": ["", "event-2"],
                        "event_type": ["valuation", "valuation"],
                    }
                ),
                unique_key=("event_id",),
                evidence={"status": "complete"},
            ),
        ),
    )
    entries[0] = _entry(root, local, "baseline", "local_research")

    registry = load_source_registry(root, _registry(root, entries))

    attribution = registry.sources[0].capabilities["attribution"]
    assert attribution["status"] == "evidence_insufficient"
    assert attribution["reason"] == "invalid_event_id"


def test_registry_accepts_valid_local_attribution_identity_and_range(
    repo_root: Path, tmp_path: Path
) -> None:
    root, entries = _prepared_sources(repo_root, tmp_path)
    local = root / "sources/local"
    shutil.rmtree(local)
    _write_result_package(
        local,
        extensions=(
            ResultExtension(
                name="attribution_log",
                schema_version="attribution-log/1",
                table=pa.table(
                    {
                        "time": [
                            "2024-01-02 16:00:00",
                            "2024-01-03 16:00:00",
                        ],
                        "event_id": ["event-1", "event-2"],
                        "event_type": ["valuation", "valuation"],
                    }
                ),
                unique_key=("event_id",),
                evidence={"status": "complete"},
            ),
        ),
    )
    entries[0] = _entry(root, local, "baseline", "local_research")

    registry = load_source_registry(root, _registry(root, entries))

    attribution = registry.sources[0].capabilities["attribution"]
    assert attribution["status"] == "available"
    assert attribution["identity_fields"] == ["event_id"]


def test_registry_degrades_local_attribution_outside_result_range(
    repo_root: Path, tmp_path: Path
) -> None:
    root, entries = _prepared_sources(repo_root, tmp_path)
    local = root / "sources/local"
    shutil.rmtree(local)
    _write_result_package(
        local,
        extensions=(
            ResultExtension(
                name="attribution_log",
                schema_version="attribution-log/1",
                table=pa.table(
                    {
                        "time": [
                            "2099-01-02 16:00:00",
                            "2099-01-03 16:00:00",
                        ],
                        "event_id": ["event-1", "event-2"],
                        "event_type": ["valuation", "valuation"],
                    }
                ),
                unique_key=("event_id",),
                evidence={"status": "complete"},
            ),
        ),
    )
    entries[0] = _entry(root, local, "baseline", "local_research")

    registry = load_source_registry(root, _registry(root, entries))

    attribution = registry.sources[0].capabilities["attribution"]
    assert attribution["status"] == "evidence_insufficient"
    assert attribution["reason"] == "event_time_range_mismatch"


@pytest.mark.parametrize(
    ("entry_index", "field", "value", "message"),
    [
        (0, "path", "latest", "repository-relative"),
        (0, "path", "../outside", "repository-relative"),
        (0, "manifest_sha256", "0" * 64, "manifest digest"),
        (0, "source_type", "joinquant_backtest", "declared source_type"),
        (2, "snapshot_id", "other-snapshot", "registered snapshot"),
    ],
)
def test_registry_rejects_unpinned_or_mismatched_sources(
    repo_root: Path,
    tmp_path: Path,
    entry_index: int,
    field: str,
    value: str,
    message: str,
) -> None:
    root, entries = _prepared_sources(repo_root, tmp_path)
    mutated = deepcopy(entries)
    mutated[entry_index][field] = value

    with pytest.raises(SourceRegistryError, match=message):
        load_source_registry(root, _registry(root, mutated))
