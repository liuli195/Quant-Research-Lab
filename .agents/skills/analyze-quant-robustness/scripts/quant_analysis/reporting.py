from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    return [
        f"| {' | '.join(headers)} |",
        f"| {' | '.join('---' for _ in headers)} |",
        *(f"| {' | '.join(row)} |" for row in rows),
    ]


def _count_status(items: object) -> str:
    rows = _rows(items)
    counts = {
        status: sum(row.get("status") == status for row in rows)
        for status in ("pass", "fail", "evidence_insufficient")
    }
    return (
        f"通过 {counts['pass']}，失败 {counts['fail']}，"
        f"证据不足 {counts['evidence_insufficient']}。"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _read_document(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    _atomic_write(path, content + "\n")


def build_standard_recommendation(analysis: Mapping[str, Any]) -> dict[str, Any]:
    gate = _mapping(analysis.get("pre_vibe_recommendation"))
    decision = str(gate.get("decision", "revise_before_joinquant"))
    return {
        "schema_version": "standard-strategy-analysis-recommendation/1",
        "analysis_id": str(analysis.get("analysis_id", "")),
        "strategy_id": str(analysis.get("strategy_id", "")),
        "decision": decision,
        "failure_count": int(gate.get("failure_count", 0)),
        "evidence_insufficient_count": int(gate.get("evidence_insufficient_count", 0)),
        "authority": "read_only_standard_result_packages",
        "next_action": "human_confirmation_required",
    }


def render_standard_analysis_report(
    analysis: Mapping[str, Any], recommendation: Mapping[str, Any]
) -> str:
    sources = _mapping(analysis.get("sources"))
    source_rows: list[list[str]] = []
    for package in _rows(sources.get("packages")):
        capabilities = _mapping(package.get("capabilities"))
        capability_text = ", ".join(
            f"{key}={_mapping(value).get('status', 'unknown')}"
            for key, value in sorted(capabilities.items())
        )
        source_rows.append(
            [
                str(package.get("scenario_id", "—")),
                str(package.get("content_sha256", "—")),
                str(package.get("manifest_sha256", "—")),
                capability_text or "—",
            ]
        )
    evidence = _mapping(analysis.get("evidence_matrix"))
    configuration = _mapping(analysis.get("analysis_configuration"))
    analysis_plan = _mapping(configuration.get("analysis_plan"))
    baseline_config = _mapping(configuration.get("baseline_config"))
    script = _mapping(analysis.get("script"))
    scenario_rows = [
        [
            str(item.get("scenario_id", "—")),
            str(item.get("dimension", "—")),
            str(item.get("params_sha256", "—")),
        ]
        for item in _rows(configuration.get("scenario_params"))
    ]
    baseline = _mapping(analysis.get("baseline"))
    baseline_rows = [
        [str(key), str(value)]
        for key, value in sorted(_mapping(baseline.get("metrics")).items())
    ]
    evidence_rows = [
        [
            str(item.get("dimension", "—")),
            str(item.get("scenario_id", "—")),
            str(item.get("status", "—")),
            ", ".join(str(reason) for reason in item.get("reasons", [])) or "—",
            str(item.get("input_sha256", "—")),
        ]
        for item in _rows(analysis.get("evidence_rows"))
    ]
    attribution = _mapping(analysis.get("attribution"))
    robustness = _mapping(analysis.get("robustness"))
    lines = [
        "# 标准策略分析报告",
        "",
        f"分析标识：`{analysis.get('analysis_id', '—')}`",
        f"策略：`{analysis.get('strategy_id', '—')}`",
        "",
        "## 结果包与能力",
        "",
        *_table(["场景", "内容摘要", "清单摘要", "能力"], source_rows),
        "",
        "## 分析配置与版本",
        "",
        *_table(
            ["项目", "值", "摘要"],
            [
                [
                    "分析计划",
                    str(analysis_plan.get("path", "—")),
                    str(analysis_plan.get("sha256", "—")),
                ],
                [
                    "基线配置",
                    str(baseline_config.get("path", "—")),
                    str(baseline_config.get("sha256", "—")),
                ],
                [
                    "分析脚本",
                    str(script.get("entry", "—")),
                    str(script.get("version", "—")),
                ],
                ["公式版本", str(analysis.get("formula_version", "—")), "—"],
                [
                    "分析定义",
                    json.dumps(
                        configuration.get("analyses", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "—",
                ],
                [
                    "门槛",
                    json.dumps(
                        configuration.get("thresholds", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "—",
                ],
            ],
        ),
        "",
        *_table(["场景", "维度", "参数摘要"], scenario_rows),
        "",
        "## 共同分析结论",
        "",
        f"基线状态：`{baseline.get('status', '—')}`；"
        f"原因：{', '.join(str(reason) for reason in baseline.get('reasons', [])) or '—'}。"
        f"证据矩阵：通过 {int(evidence.get('pass', 0))}，"
        f"失败 {int(evidence.get('fail', 0))}，"
        f"证据不足 {int(evidence.get('evidence_insufficient', 0))}。",
        "",
        *_table(["基线指标", "值"], baseline_rows),
        "",
        "## 深度归因",
        "",
        f"状态：`{attribution.get('status', '—')}`；"
        f"方法：{attribution.get('method', '—')}；"
        f"原因：{attribution.get('reason', '—')}。",
        "",
        "## 稳健性分析",
        "",
    ]
    for title, key in (
        ("时期与滚动窗口", "periods"),
        ("资产删除", "asset_deletions"),
        ("成本与执行", "cost_execution"),
        ("区块抽样", "bootstrap"),
        ("历史压力", "historical_stress"),
        ("持仓冲击", "position_shocks"),
        ("CVaR（条件风险价值）", "cvar"),
    ):
        lines.extend([f"### {title}", "", _count_status(robustness.get(key)), ""])
    lines.extend(
        [
            "## 逐项证据矩阵",
            "",
            *_table(
                ["维度", "场景", "状态", "原因", "输入摘要"], evidence_rows
            ),
            "",
            "## 人工确认",
            "",
            f"建议：`{recommendation.get('decision', 'revise_before_joinquant')}`。"
            "本报告只读取显式提供的标准结果包；不得修改结果包或启动任何研究、"
            "回测、模拟交易、提交或同步操作。",
            "",
        ]
    )
    return "\n".join(lines)


def _validated_workspace(
    repository: Path, workspace: Path
) -> tuple[Path, dict[str, Any]]:
    root = Path(repository).resolve()
    output = Path(workspace).resolve()
    if output.parent != root / ".local" / "standard-strategy-analysis":
        raise ValueError("workspace must be inside .local/standard-strategy-analysis")
    analysis = _read_document(
        output / "deterministic-analysis.json", "standard deterministic analysis"
    )
    analysis_id = analysis.get("analysis_id")
    if not isinstance(analysis_id, str) or not analysis_id or output.name != analysis_id:
        raise ValueError("standard-strategy-analysis workspace identity mismatch")
    return output, analysis


def write_standard_analysis_delivery(
    repository: Path, workspace: Path
) -> dict[str, Any]:
    root, analysis = _validated_workspace(repository, workspace)
    analysis_path = root / "deterministic-analysis.json"
    report_path = root / "standard-strategy-analysis-report.md"
    recommendation_path = root / "recommendation.json"
    recommendation = build_standard_recommendation(analysis)
    report = render_standard_analysis_report(analysis, recommendation)
    _atomic_write(report_path, report)
    recommendation["artifacts"] = {
        "deterministic_analysis_sha256": _sha256(analysis_path),
        "report_sha256": _sha256(report_path),
    }
    _write_json(recommendation_path, recommendation)
    return {
        "status": "complete",
        "analysis_id": recommendation["analysis_id"],
        "decision": recommendation["decision"],
        "report": report_path.as_posix(),
        "recommendation": recommendation_path.as_posix(),
        "next_action": recommendation["next_action"],
    }
