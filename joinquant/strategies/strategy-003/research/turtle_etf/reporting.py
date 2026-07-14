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

from scripts.research.quant_analysis.attribution import calculate_attribution
from scripts.research.quant_analysis.benchmarks import (
    calculate_bundle_benchmark_statistics,
)
from scripts.research.quant_analysis.contracts import (
    STANDARD_TABLES,
    validate_analysis_bundle,
    write_analysis_table,
)
from scripts.research.quant_analysis.metrics import calculate_performance
from scripts.research.quant_analysis.evidence import validate_evidence_matrix


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


def _candidate_document(result: ResearchResult) -> dict[str, object]:
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
                "research_status": "preset_not_ranked",
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
    conclusion = _read_json_object(output_dir / "conclusion.json")
    candidates = _read_json_object(output_dir / "candidate-strategies.json")
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
            or "rank" in item
            or "score" in item
        ):
            raise OutputValidationError("candidate evidence is invalid")
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


def decimal_text(value: Decimal | float | int | None) -> str:
    if value is None:
        return ""
    return format(Decimal(str(value)), "f")
