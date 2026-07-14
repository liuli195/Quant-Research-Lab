from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from scripts.research.quant_analysis.attribution import calculate_attribution
from scripts.research.quant_analysis.benchmarks import (
    calculate_bundle_benchmark_statistics,
)
from scripts.research.quant_analysis.contracts import (
    AnalysisBundle,
    STANDARD_TABLES,
    validate_analysis_bundle,
    write_analysis_table,
)
from scripts.research.quant_analysis.metrics import calculate_performance
from scripts.research.quant_analysis.evidence import (
    ScenarioResult,
    evidence_digest,
    validate_evidence_matrix,
)


_SHA256 = re.compile(r"[0-9a-f]{64}")
_RECOMMENDATIONS = {
    "proceed_to_joinquant",
    "revise_and_reassess",
    "stop_evidence_insufficient",
}
_CANDIDATE_IDS = (
    "baseline",
    "entry-40",
    "entry-60",
    "stop-1.5n",
    "stop-2.5n",
    "covariance-120d",
    "covariance-ewma-30d",
)
_DISCLAIMER = "本地结果不是正式回测或最终验收结论。"
_REPORT_DIGEST_PREFIX = "<!-- report-evidence-sha256: "


class OutputValidationError(RuntimeError):
    """Raised when project output cannot prove its declared identity."""


@dataclass(frozen=True)
class RunIdentity:
    run_id: str
    snapshot_id: str
    code_sha256: str
    config_sha256: str

    def __post_init__(self) -> None:
        for value in (
            self.run_id,
            self.snapshot_id,
            self.code_sha256,
            self.config_sha256,
        ):
            if _SHA256.fullmatch(value) is None:
                raise ValueError("run identity values must be lowercase SHA256 digests")

    def to_document(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "code_sha256": self.code_sha256,
            "config_sha256": self.config_sha256,
        }


@dataclass(frozen=True)
class ResearchResult:
    identity: RunIdentity
    snapshot_normalized_sha256: str
    config: Mapping[str, object]
    candidates: tuple[Mapping[str, object], ...]
    audit_rows: tuple[Mapping[str, object], ...]
    trade_rows: tuple[Mapping[str, object], ...]
    position_rows: tuple[Mapping[str, object], ...]
    risk_rows: tuple[Mapping[str, object], ...]
    analysis_rows: Mapping[str, Sequence[Mapping[str, object]]]
    metrics: Mapping[str, object]
    recommendation: str
    reasons: tuple[str, ...]
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.snapshot_normalized_sha256) is None:
            raise ValueError("snapshot normalized digest is invalid")
        if self.recommendation not in _RECOMMENDATIONS:
            raise ValueError("unsupported research recommendation")
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "audit_rows", tuple(self.audit_rows))
        object.__setattr__(self, "trade_rows", tuple(self.trade_rows))
        object.__setattr__(self, "position_rows", tuple(self.position_rows))
        object.__setattr__(self, "risk_rows", tuple(self.risk_rows))
        object.__setattr__(
            self,
            "analysis_rows",
            MappingProxyType(
                {
                    name: tuple(dict(row) for row in rows)
                    for name, rows in self.analysis_rows.items()
                }
            ),
        )
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


_CSV_FIELDS = {
    "daily-audit.csv": (
        "date",
        "sequence",
        "security",
        "action",
        "status",
        "requested_quantity",
        "filled_quantity",
        "fill_price",
        "reason",
        "allocation_sha256",
    ),
    "trades.csv": (
        "date",
        "sequence",
        "security",
        "action",
        "quantity",
        "fill_price",
        "reason",
    ),
    "positions.csv": (
        "date",
        "security",
        "asset_group",
        "quantity",
        "close",
        "market_value",
        "common_stop",
        "signal_n",
        "planned_loss",
        "stop_failure_loss",
    ),
    "risk.csv": (
        "date",
        "equity",
        "cash",
        "invested_ratio",
        "cash_ratio",
        "portfolio_planned_risk",
        "portfolio_risk_usage",
        "portfolio_volatility",
        "target_volatility_usage",
        "asset_group_value_usage",
        "asset_group_risk_usage",
        "eligible_securities",
        "cold_start_securities",
        "leave_cash_reasons",
    ),
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _semantic_digest(document: Mapping[str, object]) -> str:
    value = {key: item for key, item in document.items() if key != "document_sha256"}
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _write_json(path: Path, document: Mapping[str, object]) -> None:
    finalized = dict(document)
    finalized["document_sha256"] = _semantic_digest(finalized)
    path.write_text(
        json.dumps(
            finalized,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_csv(
    path: Path,
    fields: Sequence[str],
    rows: Sequence[Mapping[str, object]],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate_document(
    result: ResearchResult,
    *,
    recommendation: str | None = None,
) -> dict[str, object]:
    identity = result.identity.to_document()
    candidates = []
    for item in result.candidates:
        candidates.append(
            {
                "id": item["id"],
                "role": "baseline" if item["id"] == "baseline" else "challenger",
                "overrides": item["overrides"],
                "snapshot_id": result.identity.snapshot_id,
                "code_sha256": result.identity.code_sha256,
                "config_sha256": result.identity.config_sha256,
                "research_status": (
                    "retained_for_human_review"
                    if recommendation is not None
                    else "preset_not_ranked"
                ),
                "local_recommendation": recommendation,
            }
        )
    return {
        "schema_version": 1,
        "identity": identity,
        "optimizer": {
            "enabled": False,
            "status": "skipped",
            "reason": result.config["research"]["vibe_optimizer"]["reason"],
        },
        "selection_policy": "fixed_baseline_plus_six_single_factor_challenges",
        "candidates": candidates,
    }


def _conclusion_document(
    result: ResearchResult,
    *,
    metrics: Mapping[str, object],
    benchmark_statistics: Mapping[str, object],
    attribution: Sequence[Mapping[str, object]],
    analysis_bundle_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "identity": result.identity.to_document(),
        "snapshot_normalized_sha256": result.snapshot_normalized_sha256,
        "recommendation": result.recommendation,
        "reasons": list(result.reasons),
        "blockers": list(result.blockers),
        "metrics": dict(metrics),
        "benchmark_statistics": dict(benchmark_statistics),
        "attribution": [dict(row) for row in attribution],
        "analysis_bundle_sha256": analysis_bundle_sha256,
        "disclaimer": _DISCLAIMER,
    }


def _report_body(
    result: ResearchResult,
    artifact_digests: Mapping[str, str],
    *,
    metrics: Mapping[str, object],
    benchmark_statistics: Mapping[str, object],
    attribution: Sequence[Mapping[str, object]],
) -> str:
    group_values = metrics.get("maximum_asset_group_value_usage", {})
    group_risks = metrics.get("maximum_asset_group_risk_usage", {})
    reasons = metrics.get("leave_cash_reasons", {})
    artifacts = "\n".join(
        f"- `{name}`: `{digest}`" for name, digest in sorted(artifact_digests.items())
    )
    return f"""# 海龟 ETF 本地研究报告

## 方法

使用未复权日线、55 日收盘突破、20 日 N 值、固定 0.5N 加仓、共同止损、20 日退出、A1 共享预算和确定性次日开盘成交夹具。Vibe-Trading（AI 研究助理）组合优化器：已跳过；原因是当前版本存在已知前视偏差风险。

## 输入身份

- `run_id`: `{result.identity.run_id}`
- `snapshot_id`: `{result.identity.snapshot_id}`
- `snapshot_normalized_sha256`: `{result.snapshot_normalized_sha256}`
- `code_sha256`: `{result.identity.code_sha256}`
- `config_sha256`: `{result.identity.config_sha256}`

## 事件与交易

- 审计事件数：{metrics['audit_events']}
- 实际成交数：{metrics['filled_trades']}

## 实际仓位分布

- 平均仓位：{metrics['average_invested_ratio']}
- 中位仓位：{metrics['median_invested_ratio']}
- 低于 50% 仓位占比：{metrics['below_half_ratio']}
- 接近满仓占比：{metrics['near_full_ratio']}

## 现金占比与留现原因

- 平均现金占比：{metrics['average_cash_ratio']}
- 留现原因：`{json.dumps(reasons, ensure_ascii=False, sort_keys=True)}`

## 资产组风险使用率

- 资金上限使用率峰值：`{json.dumps(group_values, ensure_ascii=False, sort_keys=True)}`
- 计划风险上限使用率峰值：`{json.dumps(group_risks, ensure_ascii=False, sort_keys=True)}`

## 组合风险使用率

- 计划风险上限使用率峰值：{metrics['maximum_portfolio_risk_usage']}
- 目标波动率使用率峰值：{metrics['maximum_target_volatility_usage']}

## 收益与回撤

- 累计收益：{metrics['cumulative_return']}
- CAGR（复合年增长率）：{metrics['cagr']}
- 年化波动率：{metrics['annualized_volatility']}
- 最大回撤：{metrics['max_drawdown']}
- 最大回撤持续期：{metrics['max_drawdown_duration']} 个交易日
- Sharpe（夏普比率）：{metrics['sharpe']}
- Sortino（索提诺比率）：{metrics['sortino']}
- Calmar（卡玛比率）：{metrics['calmar']}

## Alpha（超额收益）与 Beta（市场暴露）

`{json.dumps(benchmark_statistics, ensure_ascii=False, sort_keys=True)}`

## 多维归因

- 归因事实数：{len(attribution)}
- 维度：ETF、资产组、时期、交易原因、仓位、现金、趋势过滤和风险约束。
- 归因采用逐证券真实损益与确定性守恒检查；任何维度无法勾稽时直接失败，不用残差补平。

## 限制

- 本地流程是方向性粗筛与确定性复算，不替代 JoinQuant（聚宽）正式回测。
- 成交夹具使用未复权开盘价；正式成本、滑点、分红和极端成交约束仍须在聚宽验证。
- {_DISCLAIMER}

## 产物摘要

{artifacts}
"""


def write_outputs(
    result: ResearchResult,
    output_dir: Path,
) -> Mapping[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows_by_file = {
        "daily-audit.csv": result.audit_rows,
        "trades.csv": result.trade_rows,
        "positions.csv": result.position_rows,
        "risk.csv": result.risk_rows,
    }
    for name, rows in rows_by_file.items():
        _write_csv(output_dir / name, _CSV_FIELDS[name], rows)
    if set(result.analysis_rows) != set(STANDARD_TABLES):
        raise OutputValidationError("standard analysis tables are incomplete")
    for name in STANDARD_TABLES:
        write_analysis_table(name, result.analysis_rows[name], output_dir)
    bundle = validate_analysis_bundle(output_dir)
    metrics = {**dict(result.metrics), **calculate_performance(bundle)}
    benchmark_statistics = calculate_bundle_benchmark_statistics(bundle)
    attribution = calculate_attribution(bundle)
    _write_json(
        output_dir / "conclusion.json",
        _conclusion_document(
            result,
            metrics=metrics,
            benchmark_statistics=benchmark_statistics,
            attribution=attribution,
            analysis_bundle_sha256=bundle.digest,
        ),
    )
    _write_json(
        output_dir / "candidate-strategies.json",
        _candidate_document(result),
    )
    digests = {
        name: _file_digest(output_dir / name)
        for name in (
            *rows_by_file,
            *(f"{name}.parquet" for name in STANDARD_TABLES),
            "conclusion.json",
            "candidate-strategies.json",
        )
    }
    body = _report_body(
        result,
        digests,
        metrics=metrics,
        benchmark_statistics=benchmark_statistics,
        attribution=attribution,
    )
    report_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    (output_dir / "research-report.md").write_text(
        body + f"\n{_REPORT_DIGEST_PREFIX}{report_digest} -->\n",
        encoding="utf-8",
    )
    return MappingProxyType(
        {
            name: _file_digest(output_dir / name)
            for name in (*digests, "research-report.md")
        }
    )


_COMPLETE_SCHEMAS = {
    "candidate-comparison": pa.schema(
        [
            pa.field("candidate_id", pa.string(), nullable=False),
            pa.field("candidate_order", pa.int16(), nullable=False),
            pa.field("is_baseline", pa.bool_(), nullable=False),
            pa.field("cumulative_return", pa.float64(), nullable=False),
            pa.field("cagr", pa.float64(), nullable=False),
            pa.field("annualized_volatility", pa.float64(), nullable=False),
            pa.field("sharpe", pa.float64()),
            pa.field("sortino", pa.float64()),
            pa.field("calmar", pa.float64()),
            pa.field("max_drawdown", pa.float64(), nullable=False),
            pa.field("trade_count", pa.int64(), nullable=False),
            pa.field("fees", pa.float64(), nullable=False),
            pa.field("average_invested_ratio", pa.float64(), nullable=False),
            pa.field("maximum_portfolio_risk_usage", pa.float64(), nullable=False),
            pa.field("csi300_alpha", pa.float64()),
            pa.field("csi300_beta", pa.float64()),
            pa.field("nasdaq100_alpha", pa.float64()),
            pa.field("nasdaq100_beta", pa.float64()),
        ]
    ),
    "candidate-screening": pa.schema(
        [
            pa.field("candidate_id", pa.string(), nullable=False),
            pa.field("candidate_order", pa.int16(), nullable=False),
            pa.field("retained", pa.bool_(), nullable=False),
            pa.field("local_status", pa.string(), nullable=False),
            pa.field("favorable_evidence_json", pa.string(), nullable=False),
            pa.field("adverse_evidence_json", pa.string(), nullable=False),
            pa.field("uncertainties_json", pa.string(), nullable=False),
        ]
    ),
    "attribution": pa.schema(
        [
            pa.field("candidate_id", pa.string(), nullable=False),
            pa.field("dimension", pa.string(), nullable=False),
            pa.field("key", pa.string(), nullable=False),
            pa.field("contribution", pa.float64(), nullable=False),
            pa.field("portfolio_return", pa.float64(), nullable=False),
            pa.field("reconciliation_error", pa.float64(), nullable=False),
        ]
    ),
}


def _write_complete_table(
    name: str,
    rows: Sequence[Mapping[str, object]],
    output_dir: Path,
) -> Path:
    schema = _COMPLETE_SCHEMAS[name]
    materialized = [dict(row) for row in rows]
    table = pa.Table.from_pylist(materialized, schema=schema)
    digest = evidence_digest(table.to_pylist())
    table = table.replace_schema_metadata(
        {
            b"schema_version": b"1",
            b"table_name": name.encode("ascii"),
            b"content_sha256": digest.encode("ascii"),
        }
    )
    path = Path(output_dir) / f"{name}.parquet"
    pq.write_table(table, path, compression="zstd", use_dictionary=False)
    return path


def _validate_complete_table(name: str, output_dir: Path) -> list[dict[str, object]]:
    path = Path(output_dir) / f"{name}.parquet"
    try:
        table = pq.read_table(path)
    except (OSError, pa.ArrowException) as exc:
        raise OutputValidationError(f"invalid complete report table: {name}") from exc
    if not table.schema.remove_metadata().equals(_COMPLETE_SCHEMAS[name]):
        raise OutputValidationError(f"complete report table schema mismatch: {name}")
    rows = table.to_pylist()
    expected = {
        b"schema_version": b"1",
        b"table_name": name.encode("ascii"),
        b"content_sha256": evidence_digest(rows).encode("ascii"),
    }
    if table.schema.metadata != expected:
        raise OutputValidationError(f"complete report table digest mismatch: {name}")
    return rows


def _json_array(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _markdown_with_digest(path: Path, body: str) -> None:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    path.write_text(
        body + f"\n{_REPORT_DIGEST_PREFIX}{digest} -->\n",
        encoding="utf-8",
    )


def write_complete_reports(
    *,
    baseline: ResearchResult,
    candidate_results: Sequence[tuple[str, ResearchResult]],
    robustness_results: Sequence[ScenarioResult],
    output_dir: Path,
) -> Mapping[str, str]:
    output_dir = Path(output_dir)
    if tuple(candidate_id for candidate_id, _ in candidate_results) != _CANDIDATE_IDS:
        raise OutputValidationError("candidate results differ from the frozen seven")
    metrics_by_id: dict[str, dict[str, object]] = {}
    benchmarks_by_id: dict[str, dict[str, object]] = {}
    comparison: list[dict[str, object]] = []
    attribution_rows: list[dict[str, object]] = []
    for order, (candidate_id, result) in enumerate(candidate_results):
        bundle = AnalysisBundle(
            path=output_dir,
            tables=result.analysis_rows,
            digest=evidence_digest(
                {
                    name: [dict(row) for row in rows]
                    for name, rows in result.analysis_rows.items()
                }
            ),
        )
        metrics = calculate_performance(bundle)
        benchmarks = calculate_bundle_benchmark_statistics(bundle)
        metrics_by_id[candidate_id] = metrics
        benchmarks_by_id[candidate_id] = benchmarks
        csi = benchmarks["csi300_total_return_cny"]
        nasdaq = benchmarks["nasdaq100_total_return_cny"]
        comparison.append(
            {
                "candidate_id": candidate_id,
                "candidate_order": order,
                "is_baseline": candidate_id == "baseline",
                "cumulative_return": metrics["cumulative_return"],
                "cagr": metrics["cagr"],
                "annualized_volatility": metrics["annualized_volatility"],
                "sharpe": metrics["sharpe"],
                "sortino": metrics["sortino"],
                "calmar": metrics["calmar"],
                "max_drawdown": metrics["max_drawdown"],
                "trade_count": metrics["trade_count"],
                "fees": metrics["fees"],
                "average_invested_ratio": metrics["average_invested_ratio"],
                "maximum_portfolio_risk_usage": metrics[
                    "maximum_portfolio_risk_usage"
                ],
                "csi300_alpha": csi["alpha"],
                "csi300_beta": csi["beta"],
                "nasdaq100_alpha": nasdaq["alpha"],
                "nasdaq100_beta": nasdaq["beta"],
            }
        )
        attribution_rows.extend(
            {"candidate_id": candidate_id, **dict(row)}
            for row in calculate_attribution(bundle)
        )

    baseline_metrics = metrics_by_id["baseline"]
    screening: list[dict[str, object]] = []
    for order, row in enumerate(comparison):
        favorable: list[str] = []
        adverse: list[str] = []
        if row["cagr"] > baseline_metrics["cagr"]:
            favorable.append("cagr_above_baseline")
        elif row["candidate_id"] != "baseline":
            adverse.append("cagr_not_above_baseline")
        if abs(float(row["max_drawdown"])) < abs(float(baseline_metrics["max_drawdown"])):
            favorable.append("drawdown_below_baseline")
        elif row["candidate_id"] != "baseline":
            adverse.append("drawdown_not_below_baseline")
        local_status = (
            "pass"
            if float(row["cagr"]) > 0 and abs(float(row["max_drawdown"])) <= 0.20
            else "fail"
        )
        screening.append(
            {
                "candidate_id": row["candidate_id"],
                "candidate_order": order,
                "retained": True,
                "local_status": local_status,
                "favorable_evidence_json": _json_array(favorable),
                "adverse_evidence_json": _json_array(adverse),
                "uncertainties_json": _json_array(
                    ["local_exploratory_not_formal_backtest"]
                ),
            }
        )

    failed = [row.scenario_id for row in robustness_results if row.status == "fail"]
    insufficient = [
        row.scenario_id
        for row in robustness_results
        if row.status == "evidence_insufficient"
    ]
    if any(
        baseline_metrics.get(key) is None
        for key in ("cagr", "max_drawdown", "calmar")
    ):
        recommendation = "stop_evidence_insufficient"
    elif (
        float(baseline_metrics["cagr"]) <= 0
        or abs(float(baseline_metrics["max_drawdown"])) > 0.20
        or failed
    ):
        recommendation = "revise_and_reassess"
    else:
        recommendation = "proceed_to_joinquant"
    challengers = sorted(
        comparison[1:],
        key=lambda row: (
            float("-inf") if row["calmar"] is None else float(row["calmar"]),
            float(row["cagr"]),
        ),
        reverse=True,
    )
    candidate_focus = [str(row["candidate_id"]) for row in challengers[:2]]
    recommendation_document = {
        "schema_version": 1,
        "identity": baseline.identity.to_document(),
        "recommendation": recommendation,
        "next_action": "human_confirmation_required",
        "baseline_action": "retain_frozen_baseline",
        "candidate_focus": candidate_focus,
        "deterministic_reasons": [
            f"baseline_cagr={baseline_metrics['cagr']}",
            f"baseline_max_drawdown={baseline_metrics['max_drawdown']}",
            f"robustness_failures={len(failed)}",
        ],
        "contrary_evidence": failed,
        "uncertainties": insufficient,
        "blockers": (
            ["baseline_evidence_incomplete"]
            if recommendation == "stop_evidence_insufficient"
            else []
        ),
        "vibe_trading": {
            "status": "unavailable",
            "reused_materials": [
                "docs/research/2026-07-13-turtle-edge-vibe-study.md"
            ],
            "optimizer": "skipped_known_lookahead_bias",
            "scope": "report_material_only",
        },
        "disclaimer": _DISCLAIMER,
    }

    _write_complete_table("candidate-comparison", comparison, output_dir)
    _write_complete_table("candidate-screening", screening, output_dir)
    _write_complete_table("attribution", attribution_rows, output_dir)
    _write_json(output_dir / "recommendation.json", recommendation_document)
    _write_json(
        output_dir / "candidate-strategies.json",
        _candidate_document(baseline, recommendation=recommendation),
    )
    conclusion = _read_json_object(output_dir / "conclusion.json")
    conclusion.pop("document_sha256", None)
    conclusion["recommendation"] = recommendation
    conclusion["reasons"] = recommendation_document["deterministic_reasons"]
    _write_json(output_dir / "conclusion.json", conclusion)

    comparison_lines = "\n".join(
        "| {candidate_id} | {cagr:.6f} | {max_drawdown:.6f} | {calmar} | {average_invested_ratio:.6f} |".format(
            **row
        )
        for row in comparison
    )
    challenge_body = f"""# 海龟 ETF 七方案挑战报告

| 候选 | CAGR（复合年增长率） | 最大回撤 | Calmar（卡玛比率） | 平均仓位 |
|---|---:|---:|---:|---:|
{comparison_lines}

七项候选全部保留，未自动删除、替换基线或生成新参数。候选关注项：{', '.join(candidate_focus)}。

稳健性失败：`{json.dumps(failed, ensure_ascii=False)}`

证据不足：`{json.dumps(insufficient, ensure_ascii=False)}`

{_DISCLAIMER}
"""
    _markdown_with_digest(output_dir / "challenge-report.md", challenge_body)
    artifact_names = (
        "candidate-comparison.parquet",
        "candidate-screening.parquet",
        "attribution.parquet",
        "local-evidence-matrix.parquet",
        "recommendation.json",
        "candidate-strategies.json",
        "challenge-report.md",
    )
    artifact_lines = "\n".join(
        f"- `{name}`: `{_file_digest(output_dir / name)}`" for name in artifact_names
    )
    local_body = f"""# 海龟 ETF 完整本地研究报告

## 推荐结论

- 建议：`{recommendation}`
- 下一步：`human_confirmation_required`（需要人工确认）
- 冻结基线行动：保留，不自动替换。
- 候选关注项：{', '.join(candidate_focus)}

## 基线收益、风险与仓位

`{json.dumps(baseline_metrics, ensure_ascii=False, sort_keys=True)}`

## 基准、Alpha（超额收益）与 Beta（市场暴露）

`{json.dumps(benchmarks_by_id['baseline'], ensure_ascii=False, sort_keys=True)}`

## 七方案挑战

共 7 项，全部真实运行并保留。详见 `candidate-comparison.parquet` 与 `challenge-report.md`。

## 归因

归因覆盖 ETF、资产组、时期、交易原因、仓位、现金、趋势过滤和风险约束；使用逐证券真实损益，无法勾稽时流程失败。

## 稳健性、压力与尾部风险

- 场景总数：{len(robustness_results)}
- 失败：{len(failed)}
- 证据不足：{len(insufficient)}
- 失败场景：`{json.dumps(failed, ensure_ascii=False)}`
- 证据不足场景：`{json.dumps(insufficient, ensure_ascii=False)}`

## 有利证据与反对证据

- 有利：基线完整绩效、真实逐证券归因、七候选和完整稳健性矩阵均可复算。
- 反对：`{json.dumps(failed, ensure_ascii=False)}`

## Vibe-Trading（AI 研究助理）材料

当前仓库没有可直接调用的 Vibe-Trading 程序接口；仅复用既有研究材料。存在前视偏差风险的组合优化器继续禁用。本变更未安装工具、未新增适配器、未修改职责边界。

## 限制

- 本地结果仅用于方向性研究与挑战筛选。
- 本流程未启动 JoinQuant（聚宽）正式回测、模拟交易或正式复核。
- 最终行动等待人工确认。
- {_DISCLAIMER}

## 产物摘要

{artifact_lines}
"""
    _markdown_with_digest(output_dir / "local-research-report.md", local_body)
    return MappingProxyType(
        {
            name: _file_digest(output_dir / name)
            for name in (*artifact_names, "local-research-report.md")
        }
    )


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OutputValidationError(f"invalid project output: {path.name}") from exc
    if not isinstance(value, dict):
        raise OutputValidationError(f"project output must be an object: {path.name}")
    digest = value.get("document_sha256")
    if not isinstance(digest, str) or digest != _semantic_digest(value):
        raise OutputValidationError(f"project output digest mismatch: {path.name}")
    return value


def validate_project_outputs(output_dir: Path, identity: RunIdentity) -> None:
    output_dir = Path(output_dir)
    try:
        bundle = validate_analysis_bundle(output_dir)
        evidence_rows = validate_evidence_matrix(
            output_dir / "local-evidence-matrix.parquet"
        )
    except ValueError as exc:
        raise OutputValidationError("standard analysis bundle is invalid") from exc
    if not evidence_rows:
        raise OutputValidationError("local evidence matrix is empty")
    comparison = _validate_complete_table("candidate-comparison", output_dir)
    screening = _validate_complete_table("candidate-screening", output_dir)
    attribution_rows = _validate_complete_table("attribution", output_dir)
    conclusion = _read_json_object(output_dir / "conclusion.json")
    candidates = _read_json_object(output_dir / "candidate-strategies.json")
    recommendation = _read_json_object(output_dir / "recommendation.json")
    expected_identity = identity.to_document()
    if conclusion.get("identity") != expected_identity:
        raise OutputValidationError("conclusion identity mismatch")
    if conclusion.get("recommendation") not in _RECOMMENDATIONS:
        raise OutputValidationError("conclusion recommendation is invalid")
    if conclusion.get("disclaimer") != _DISCLAIMER:
        raise OutputValidationError("conclusion disclaimer is missing")
    if conclusion.get("analysis_bundle_sha256") != bundle.digest:
        raise OutputValidationError("analysis bundle identity mismatch")
    recalculated_metrics = calculate_performance(bundle)
    metrics = conclusion.get("metrics")
    if not isinstance(metrics, Mapping) or any(
        metrics.get(key) != value for key, value in recalculated_metrics.items()
    ):
        raise OutputValidationError("performance metrics differ from analysis facts")
    if conclusion.get(
        "benchmark_statistics"
    ) != calculate_bundle_benchmark_statistics(bundle):
        raise OutputValidationError("benchmark statistics differ from analysis facts")
    if conclusion.get("attribution") != [
        dict(row) for row in calculate_attribution(bundle)
    ]:
        raise OutputValidationError("attribution differs from analysis facts")
    if candidates.get("identity") != expected_identity:
        raise OutputValidationError("candidate identity mismatch")
    if (
        recommendation.get("identity") != expected_identity
        or recommendation.get("recommendation") not in _RECOMMENDATIONS
        or recommendation.get("next_action") != "human_confirmation_required"
        or recommendation.get("baseline_action") != "retain_frozen_baseline"
        or recommendation.get("disclaimer") != _DISCLAIMER
    ):
        raise OutputValidationError("recommendation contract is invalid")
    items = candidates.get("candidates")
    if (
        not isinstance(items, list)
        or any(not isinstance(item, dict) for item in items)
        or tuple(item.get("id") for item in items) != _CANDIDATE_IDS
    ):
        raise OutputValidationError("candidate set differs from the frozen seven")
    for item in items:
        if (
            not isinstance(item, dict)
            or item.get("snapshot_id") != identity.snapshot_id
            or item.get("code_sha256") != identity.code_sha256
            or item.get("config_sha256") != identity.config_sha256
            or item.get("local_recommendation")
            != recommendation.get("recommendation")
            or "rank" in item
            or "score" in item
        ):
            raise OutputValidationError("candidate evidence is invalid")
    if tuple(row["candidate_id"] for row in comparison) != _CANDIDATE_IDS:
        raise OutputValidationError("candidate comparison differs from the frozen seven")
    if (
        tuple(row["candidate_id"] for row in screening) != _CANDIDATE_IDS
        or not all(row["retained"] for row in screening)
    ):
        raise OutputValidationError("candidate screening deleted or reordered a candidate")
    if (
        {row["candidate_id"] for row in attribution_rows} != set(_CANDIDATE_IDS)
        or not attribution_rows
    ):
        raise OutputValidationError("candidate attribution is incomplete")
    try:
        report = (output_dir / "research-report.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise OutputValidationError("research report is missing or invalid") from exc
    marker_start = report.rfind("\n" + _REPORT_DIGEST_PREFIX)
    if marker_start < 0 or not report.endswith(" -->\n"):
        raise OutputValidationError("research report digest marker is missing")
    body = report[:marker_start]
    declared = report[
        marker_start + len("\n" + _REPORT_DIGEST_PREFIX) : -len(" -->\n")
    ]
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != declared:
        raise OutputValidationError("research report digest mismatch")
    if _DISCLAIMER not in body:
        raise OutputValidationError("research report boundary is missing")
    for report_name in ("local-research-report.md", "challenge-report.md"):
        try:
            complete_report = (output_dir / report_name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise OutputValidationError(f"complete report is invalid: {report_name}") from exc
        report_marker = complete_report.rfind("\n" + _REPORT_DIGEST_PREFIX)
        if report_marker < 0 or not complete_report.endswith(" -->\n"):
            raise OutputValidationError(f"complete report digest is missing: {report_name}")
        report_body = complete_report[:report_marker]
        report_declared = complete_report[
            report_marker + len("\n" + _REPORT_DIGEST_PREFIX) : -len(" -->\n")
        ]
        if hashlib.sha256(report_body.encode("utf-8")).hexdigest() != report_declared:
            raise OutputValidationError(f"complete report digest mismatch: {report_name}")
        if _DISCLAIMER not in report_body:
            raise OutputValidationError(f"complete report boundary is missing: {report_name}")


def decimal_text(value: Decimal | float | int | None) -> str:
    if value is None:
        return ""
    return format(Decimal(str(value)), "f")
