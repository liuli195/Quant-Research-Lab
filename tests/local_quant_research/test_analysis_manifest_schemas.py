from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.research.analysis_data.manifest import (
    AnalysisManifestError,
    open_analysis_source,
    validate_analysis_source,
    validate_local_manifest_document,
)


SHA = "a" * 64


def _dataset(name: str, rows: int = 1) -> dict[str, object]:
    return {
        "required": True,
        "status": "complete",
        "rows": rows,
        "verified_empty": rows == 0,
        "time_range": {
            "start": None if rows == 0 else "2024-01-02",
            "end": None if rows == 0 else "2024-01-02",
        },
        "files": [
            {
                "path": f"data/{name}.parquet",
                "sha256": SHA,
                "bytes": 100,
                "rows": rows,
                "format": "parquet",
                "compression": "zstd",
            }
        ],
        "evidence": {"fields": ["time"], "unique_key": ["time"]},
    }


def _local_manifest() -> dict[str, object]:
    return {
        "schema_version": "local-backtest/1",
        "object": {
            "kind": "local_backtest",
            "local_id": "local-bt-001",
            "status": "complete",
        },
        "source": {
            "kind": "local_vectorbt",
            "engine": {
                "backend": "vectorbt.Portfolio.from_order_func",
                "adapter_version": "local-vectorbt-adapter/1",
                "vectorbt": "1.1.0",
                "numba": "0.66.0",
                "numpy": "2.4.6",
                "pandas": "3.0.3",
            },
            "accounting": {
                "version": "turtle-etf-corporate-actions/1",
                "corporate_action_mode": "point_in_time_total_return_approximation",
                "continuity_factor_basis": "raw_previous_close_over_current_pre_close",
                "corporate_action_metadata_timing": "audit_only_may_be_retrospective",
                "price_basis": "continuous_economic_price",
                "quantity_basis": "economic_units",
                "cash_dividend_mode": "implicit_reinvestment_on_ex_date",
                "pay_date_cash_supported": False,
                "exact_joinquant_reconciliation": False,
                "corporate_actions_sha256": SHA,
            },
        },
        "authority": "local_research",
        "run": {
            "run_id": "run-001",
            "scenario_id": "baseline",
            "snapshot_id": SHA,
        },
        "code": {
            "path": "code.py",
            "sha256": SHA,
            "bytes": 100,
        },
        "params": {
            "current": {
                "path": "params.json",
                "sha256": SHA,
                "bytes": 100,
            },
            "version": {
                "path": f"params_versions/{SHA}.json",
                "sha256": SHA,
                "bytes": 100,
            },
        },
        "performance": {
            "path": "performance.json",
            "sha256": SHA,
            "bytes": 100,
        },
        "datasets": {
            **{name: _dataset(name) for name in ("results", "balances", "positions", "orders")},
            "risk": {
                "required": False,
                "status": "missing_at_source",
                "reason": "computed_by_strategy_analysis",
                "rows": 0,
                "verified_empty": True,
                "files": [],
            },
            "period_risks": {
                "required": False,
                "status": "missing_at_source",
                "reason": "computed_by_strategy_analysis",
                "rows": 0,
                "verified_empty": True,
                "files": [],
            },
        },
        "source_benchmark_returns": {
            "status": "missing_at_source",
            "reason": "independent_benchmark_set",
            "null_rows": 1,
        },
        "gate": {"status": "pass", "exceptions": [], "checks": ["file_digests"]},
    }


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def test_local_manifest_schema_is_strict_and_excludes_joinquant_evidence(
    repo_root: Path,
) -> None:
    schema = json.loads(
        (
            repo_root
            / "scripts/research/analysis_data/schemas/local-backtest-manifest.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["properties"]["schema_version"]["const"] == "local-backtest/1"
    assert schema["properties"]["object"]["properties"]["kind"]["const"] == "local_backtest"
    assert schema["properties"]["source"]["properties"]["kind"]["const"] == "local_vectorbt"
    assert schema["properties"]["authority"]["const"] == "local_research"
    assert set(schema["required"]) >= {
        "authority",
        "run",
        "params",
        "source_benchmark_returns",
    }

    valid = _local_manifest()
    validate_local_manifest_document(valid)
    for forbidden in (
        "collection_fence",
        "research_response",
        "research_lineage",
        "official_summary",
    ):
        invalid = {**valid, forbidden: {}}
        with pytest.raises(AnalysisManifestError):
            validate_local_manifest_document(invalid)
    invalid = copy.deepcopy(valid)
    invalid["source"]["url"] = "https://www.joinquant.com/backtest/fake"
    with pytest.raises(AnalysisManifestError):
        validate_local_manifest_document(invalid)


def test_local_accounting_precision_boundary_is_mandatory_and_closed() -> None:
    valid = _local_manifest()
    validate_local_manifest_document(valid)
    accounting = valid["source"]["accounting"]
    for field in tuple(accounting):
        invalid = copy.deepcopy(valid)
        del invalid["source"]["accounting"][field]
        with pytest.raises(AnalysisManifestError):
            validate_local_manifest_document(invalid)

    invalid = copy.deepcopy(valid)
    invalid["source"]["accounting"]["corporate_action_mode"] = "exact"
    with pytest.raises(AnalysisManifestError):
        validate_local_manifest_document(invalid)

    invalid = copy.deepcopy(valid)
    invalid["source"]["accounting"]["exact_joinquant_reconciliation"] = True
    with pytest.raises(AnalysisManifestError):
        validate_local_manifest_document(invalid)


def test_local_missing_source_references_are_not_fake_physical_tables() -> None:
    valid = _local_manifest()
    for name in ("risk", "period_risks"):
        invalid = copy.deepcopy(valid)
        invalid["datasets"][name] = _dataset(name)
        with pytest.raises(AnalysisManifestError):
            validate_local_manifest_document(invalid)
    invalid = copy.deepcopy(valid)
    invalid["source_benchmark_returns"]["reason"] = "filled_with_zero"
    with pytest.raises(AnalysisManifestError):
        validate_local_manifest_document(invalid)


def test_reader_selects_only_by_top_level_version_and_never_falls_back(
    tmp_path: Path,
) -> None:
    root = tmp_path / "backtest"
    root.mkdir()
    document = _local_manifest()
    document["schema_version"] = 1
    (root / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AnalysisManifestError, match="joinquant"):
        open_analysis_source(root)

    document["schema_version"] = "unknown/1"
    (root / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AnalysisManifestError, match="unsupported"):
        open_analysis_source(root)


def test_existing_joinquant_backtest_validates_without_modification(
    repo_root: Path,
) -> None:
    root = repo_root / "joinquant/strategies/strategy-001/backtests/111"
    before = _tree_digest(root)

    source = open_analysis_source(root)
    result = validate_analysis_source(source)

    assert source.kind == "joinquant_backtest"
    assert result.status == "pass"
    assert tuple(result.datasets) == (
        "results",
        "balances",
        "positions",
        "orders",
        "risk",
        "period_risks",
    )
    assert _tree_digest(root) == before
