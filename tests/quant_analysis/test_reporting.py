from __future__ import annotations

import json
from pathlib import Path

import pytest

from quant_analysis.reporting import (
    build_standard_recommendation,
    render_standard_analysis_report,
    write_standard_analysis_delivery,
)


def _standard_analysis() -> dict[str, object]:
    return {
        "schema_version": "standard-strategy-analysis/1",
        "formula_version": "standard-strategy-analysis/1",
        "analysis_id": "standard-1",
        "strategy_id": "strategy-1",
        "source_mutation": "forbidden",
        "sources": {
            "package_count": 1,
            "packages": [
                {
                    "scenario_id": "baseline",
                    "content_sha256": "a" * 64,
                    "manifest_sha256": "b" * 64,
                    "capabilities": {
                        "attribution": {"status": "missing_at_source"},
                        "official_risk": {"status": "available"},
                    },
                }
            ],
        },
        "baseline": {
            "status": "pass",
            "reasons": [],
            "metrics": {"cagr": 0.1},
        },
        "analysis_configuration": {
            "analysis_plan": {
                "path": "config/analysis-plan.json",
                "sha256": "d" * 64,
            },
            "baseline_config": {
                "path": "config/baseline.json",
                "sha256": "e" * 64,
            },
            "scenario_params": [
                {
                    "scenario_id": "baseline",
                    "dimension": "baseline",
                    "params_sha256": "f" * 64,
                }
            ],
            "analyses": {"bootstrap": {"seed": 20260719}},
            "thresholds": {"calmar_min": 0.5},
        },
        "script": {
            "version": "analyze-quant-robustness/1",
            "entry": ".agents/skills/analyze-quant-robustness/scripts/analyze_quant_robustness.py",
        },
        "attribution": {
            "status": "evidence_insufficient",
            "reason": "missing_at_source",
        },
        "cross_scenario": {
            "status": "evidence_insufficient",
            "reasons": ["single_result_package"],
        },
        "robustness": {
            "periods": [],
            "asset_deletions": [],
            "cost_execution": [
                {
                    "scenario_id": "double-commission",
                    "status": "pass",
                    "reasons": [],
                    "metrics": {"cagr": 0.08, "max_drawdown": -0.1, "calmar": 0.8},
                }
            ],
            "bootstrap": [{"status": "pass", "reasons": []}],
            "historical_stress": [],
            "position_shocks": [],
            "cvar": [],
        },
        "evidence_matrix": {
            "rows": 2,
            "pass": 1,
            "fail": 0,
            "evidence_insufficient": 1,
        },
        "evidence_rows": [
            {
                "scenario_id": "baseline-performance",
                "dimension": "baseline_performance",
                "status": "pass",
                "reasons": [],
                "metrics": {"cagr": 0.1},
                "input_sha256": "3" * 64,
            },
            {
                "scenario_id": "challenge-double-commission",
                "dimension": "cost_execution",
                "status": "pass",
                "reasons": [],
                "metrics": {"cagr": 0.08, "max_drawdown": -0.1, "calmar": 0.8},
                "input_sha256": "4" * 64,
            },
        ],
        "pre_vibe_recommendation": {
            "decision": "revise_before_joinquant",
            "failure_count": 0,
            "evidence_insufficient_count": 1,
        },
    }


def test_standard_report_lists_package_identity_capabilities_and_evidence_gaps(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    workspace = (
        repository / ".local" / "standard-strategy-analysis" / "standard-1"
    )
    workspace.mkdir(parents=True)
    analysis = _standard_analysis()
    report = render_standard_analysis_report(
        analysis, build_standard_recommendation(analysis)
    )
    (workspace / "deterministic-analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False), encoding="utf-8"
    )

    delivery = write_standard_analysis_delivery(repository, workspace)

    assert "结果包与能力" in report
    assert "a" * 64 in report
    assert "证据不足" in report
    assert "config/analysis-plan.json" in report
    assert "standard-strategy-analysis/1" in report
    assert "market_snapshot_missing_at_source" not in report
    assert "4" * 64 in report
    assert "cagr" in report
    assert delivery["decision"] == "revise_before_joinquant"
    assert (workspace / "standard-strategy-analysis-report.md").is_file()


def test_standard_delivery_rejects_workspace_outside_repository(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    workspace = tmp_path / "outside" / "standard-1"
    workspace.mkdir(parents=True)
    (workspace / "deterministic-analysis.json").write_text(
        json.dumps(_standard_analysis()), encoding="utf-8"
    )
    report = workspace / "standard-strategy-analysis-report.md"
    report.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="standard-strategy-analysis"):
        write_standard_analysis_delivery(repository, workspace)

    assert report.read_text(encoding="utf-8") == "keep"
