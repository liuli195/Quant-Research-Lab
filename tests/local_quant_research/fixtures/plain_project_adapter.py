from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.research.market_data.query import open_snapshot
from scripts.research.quant_analysis.contracts import (
    BENCHMARK_IDS,
    STANDARD_TABLES,
    validate_analysis_bundle,
    write_analysis_table,
)
from scripts.research.quant_analysis.evidence import (
    ScenarioResult,
    build_evidence_matrix,
    evidence_digest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-manifest", required=True)
    parser.add_argument("--market-data-root", required=True)
    parser.add_argument("--project-config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--code-sha256", required=True)
    parser.add_argument("--config-sha256", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    output = Path(args.output_dir)
    snapshot = open_snapshot(args.snapshot_id, root=Path(args.market_data_root))
    dates = sorted({str(row["date"]) for row in snapshot.rows})
    if len(dates) != 2:
        raise ValueError("plain fixture expects two dates")
    first, second = dates
    rows: dict[str, list[dict[str, object]]] = {
        "equity": [
            {
                "date": first,
                "portfolio_id": "plain",
                "currency": "CNY",
                "equity": 100.0,
                "cash": 90.0,
                "positions_value": 10.0,
                "daily_pnl": 0.0,
                "fees": 0.0,
            },
            {
                "date": second,
                "portfolio_id": "plain",
                "currency": "CNY",
                "equity": 100.1,
                "cash": 90.0,
                "positions_value": 10.1,
                "daily_pnl": 0.1,
                "fees": 0.0,
            },
        ],
        "returns": [
            {
                "date": first,
                "portfolio_id": "plain",
                "return": 0.0,
                "equity": 100.0,
                "cash_return_contribution": 0.0,
            },
            {
                "date": second,
                "portfolio_id": "plain",
                "return": 0.001,
                "equity": 100.1,
                "cash_return_contribution": 0.0,
            },
        ],
        "trades": [],
        "orders": [],
        "positions": [
            {
                "date": first,
                "security": "000001.XSHG",
                "asset_group": "plain",
                "quantity": 1.0,
                "close": 10.0,
                "market_value": 10.0,
                "weight": 0.1,
                "planned_loss": 1.0,
                "common_stop": 9.0,
                "signal_n": 0.5,
                "stop_failure_loss": 2.0,
                "attribution_reason": "holding",
                "pnl_contribution": 0.0,
                "return_contribution": 0.0,
            },
            {
                "date": second,
                "security": "000001.XSHG",
                "asset_group": "plain",
                "quantity": 1.0,
                "close": 10.1,
                "market_value": 10.1,
                "weight": 0.1,
                "planned_loss": 1.0,
                "common_stop": 9.0,
                "signal_n": 0.5,
                "stop_failure_loss": 2.0,
                "attribution_reason": "holding",
                "pnl_contribution": 0.1,
                "return_contribution": 0.001,
            },
        ],
        "risk": [
            {
                "date": first,
                "portfolio_id": "plain",
                "equity": 100.0,
                "cash": 90.0,
                "invested_ratio": 0.1,
                "cash_ratio": 0.9,
                "planned_risk": 1.0,
                "portfolio_risk_usage": 0.1,
                "portfolio_volatility": 0.05,
                "target_volatility_usage": 0.5,
            },
            {
                "date": second,
                "portfolio_id": "plain",
                "equity": 100.1,
                "cash": 90.0,
                "invested_ratio": 0.1,
                "cash_ratio": 0.9,
                "planned_risk": 1.0,
                "portfolio_risk_usage": 0.1,
                "portfolio_volatility": 0.05,
                "target_volatility_usage": 0.5,
            },
        ],
        "events": [],
        "benchmarks": [],
    }
    for benchmark_id in BENCHMARK_IDS:
        source_id = f"fixture-{benchmark_id}"
        rows["benchmarks"].extend(
            [
                {
                    "date": first,
                    "benchmark_id": benchmark_id,
                    "currency": "CNY",
                    "total_return_index": 100.0,
                    "return": 0.0,
                    "source_id": source_id,
                },
                {
                    "date": second,
                    "benchmark_id": benchmark_id,
                    "currency": "CNY",
                    "total_return_index": 101.0,
                    "return": 0.01,
                    "source_id": source_id,
                },
            ]
        )
    for table in STANDARD_TABLES:
        write_analysis_table(table, rows[table], output)
    bundle = validate_analysis_bundle(output)
    build_evidence_matrix(
        (
            ScenarioResult(
                scenario_id="plain-project-evidence",
                dimension="fixture",
                status="pass",
                metrics={"samples": 2},
                input_sha256=evidence_digest({"snapshot_id": args.snapshot_id}),
            ),
        ),
        output / "local-evidence-matrix.parquet",
    )
    (output / "local-research-report.md").write_text(
        "# 通用本地研究报告\n\nVibe-Trading（AI 研究助理）能力不可用；未伪造结果。\n",
        encoding="utf-8",
    )
    (output / "recommendation.json").write_text(
        json.dumps(
            {
                "run_id": args.run_id,
                "recommendation": "proceed_to_joinquant",
                "next_action": "human_confirmation_required",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "result.json").write_text(
        json.dumps(
            {
                "snapshot_id": snapshot.snapshot_id,
                "run_id": args.run_id,
                "analysis_bundle_sha256": bundle.digest,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "project-status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "reason_codes": [],
                "next_action": "human_confirmation_required",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
