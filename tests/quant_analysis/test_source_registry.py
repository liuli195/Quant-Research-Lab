from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.research.quant_analysis.source_registry import (
    SourceRegistryError,
    load_source_registry,
)
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
