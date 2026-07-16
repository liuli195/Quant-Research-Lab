from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


def enforce_vibe_boundary(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude the known-defective Vibe group-analysis path from conclusions."""

    corrected = deepcopy(dict(evidence))
    swarm = corrected.get("swarm")
    single_agent = corrected.get("single_agent")
    valid_single_agent = bool(
        isinstance(single_agent, dict)
        and single_agent.get("interface") == "vibe-trading-cli-run"
        and single_agent.get("status") == "completed"
        and isinstance(single_agent.get("run_id"), str)
        and bool(single_agent.get("run_id"))
        and isinstance(single_agent.get("assessment"), dict)
        and single_agent["assessment"].get("status") == "completed"
    )
    if isinstance(swarm, dict):
        preset = str(swarm.get("preset", "unknown"))
        forbidden = f"run_swarm:{preset}"
        called = list(corrected.get("forbidden_capabilities_called", []))
        if forbidden not in called:
            called.append(forbidden)
        corrected["forbidden_capabilities_called"] = called
        swarm["valid_evidence"] = False
        swarm["excluded_from_conclusions"] = True
        corrected["boundary_violation"] = {
            "occurred": True,
            "capability": forbidden,
            "reason": "Vibe 群体分析是已知缺陷路径；误调用结果无效。",
        }
        if isinstance(single_agent, dict):
            single_agent["valid_evidence"] = False
            single_agent["excluded_from_conclusions"] = True
    elif isinstance(single_agent, dict):
        single_agent["valid_evidence"] = valid_single_agent
        single_agent["qualitative_only"] = True

    use_result = dict(corrected.get("use_result", {}))
    capabilities_loaded = list(corrected.get("capabilities_loaded", []))
    vibe_called = bool(
        isinstance(swarm, dict)
        or isinstance(single_agent, dict)
        or capabilities_loaded
    )
    if "vibe_called" in use_result:
        vibe_called = bool(use_result["vibe_called"] or vibe_called)
    use_result["vibe_called"] = vibe_called
    use_result["vibe_conclusion_available"] = bool(
        valid_single_agent and not isinstance(swarm, dict)
    )
    use_result["loaded_capabilities_are_methodology_only"] = bool(
        capabilities_loaded and not valid_single_agent
    )
    if valid_single_agent and not isinstance(swarm, dict):
        use_result["reason"] = (
            "已通过公开 CLI 完成 Vibe 单体定性复核；结果只作审计，"
            "不替代确定性数值裁判。"
        )
    elif vibe_called:
        use_result["reason"] = (
            "没有完成可用的 Vibe 单体策略结果分析；已加载能力仅是方法文档，"
            "不得冒充实际复核。"
        )
    else:
        use_result["reason"] = "本次未调用 Vibe。"
    corrected["use_result"] = use_result
    corrected["authority"] = "audit_only"
    corrected["next_action"] = "generate_deterministic_local_report"
    return corrected


def _finite(value: object) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _pct(value: object, digits: int = 2) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number * 100:.{digits}f}%"


def _num(value: object, digits: int = 3) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number:.{digits}f}"


def _money(value: object) -> str:
    number = _finite(value)
    return "—" if number is None else f"¥{number:,.2f}"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _best_iteration_candidate(analysis: Mapping[str, Any]) -> Mapping[str, Any] | None:
    challenges = [
        row
        for row in _rows(analysis.get("challenge_results"))
        if row.get("scenario_id") != "baseline"
    ]
    measurable = [
        row
        for row in challenges
        if _finite(_mapping(row.get("metrics")).get("calmar")) is not None
    ]
    if not measurable:
        return None
    return max(
        measurable,
        key=lambda row: float(_mapping(row.get("metrics"))["calmar"]),
    )


def build_recommendation(analysis: Mapping[str, Any]) -> dict[str, Any]:
    baseline = _mapping(analysis.get("baseline"))
    evidence = _mapping(analysis.get("evidence_matrix"))
    best = _best_iteration_candidate(analysis)
    best_status = None if best is None else str(best.get("status"))
    baseline_passed = baseline.get("status") == "pass"
    no_failed_evidence = int(evidence.get("fail", 0)) == 0
    no_missing_evidence = int(evidence.get("evidence_insufficient", 0)) == 0
    accepted = bool(
        best is not None
        and best_status == "pass"
        and baseline_passed
        and no_failed_evidence
        and no_missing_evidence
    )
    decision = "proceed_to_joinquant" if accepted else "revise_and_reassess"
    candidate_id = None if best is None else str(best.get("scenario_id"))
    reasons = [
        "冻结基线未达到全部研究门槛。" if not baseline_passed else "冻结基线通过门槛。",
        (
            f"证据矩阵有 {int(evidence.get('fail', 0))} 项失败，"
            f"{int(evidence.get('evidence_insufficient', 0))} 项证据不足。"
        ),
    ]
    if candidate_id is not None:
        reasons.append(
            f"{candidate_id} 是已测试场景中 Calmar（年化收益/最大回撤）最高者，"
            "但只作为下一轮迭代候选。"
        )
    return {
        "schema_version": "strategy-analysis-recommendation/1",
        "analysis_id": str(analysis.get("analysis_id", "")),
        "strategy_id": str(analysis.get("strategy_id", "")),
        "decision": decision,
        "recommended_iteration_candidate": candidate_id,
        "candidate_accepted": accepted,
        "baseline_status": str(baseline.get("status", "unknown")),
        "reasons": reasons,
        "authority": "local_exploratory",
        "not_formal_joinquant_backtest": True,
        "vibe_group_analysis_used": False,
        "next_action": "human_confirmation_required",
    }


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    result = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    result.extend("| " + " | ".join(row) + " |" for row in rows)
    return result


def _challenge_table(analysis: Mapping[str, Any]) -> list[str]:
    rows: list[list[str]] = []
    for item in _rows(analysis.get("challenge_results")):
        metrics = _mapping(item.get("metrics"))
        rows.append(
            [
                str(item.get("scenario_id", "")),
                str(item.get("status", "")),
                _pct(metrics.get("cumulative_return")),
                _pct(metrics.get("cagr")),
                _pct(metrics.get("max_drawdown")),
                _num(metrics.get("calmar")),
                _pct(metrics.get("average_invested_ratio")),
                _num(item.get("cold_seconds"), 2),
                _num(item.get("warm_seconds"), 2),
                ", ".join(str(reason) for reason in item.get("reasons", [])) or "—",
            ]
        )
    return _table(
        [
            "场景",
            "状态",
            "累计收益",
            "年化收益",
            "最大回撤",
            "Calmar",
            "平均仓位",
            "冷启动秒",
            "预热秒",
            "未通过原因",
        ],
        rows,
    )


def _robustness_table(items: object) -> list[str]:
    rows: list[list[str]] = []
    for item in _rows(items):
        metrics = _mapping(item.get("metrics"))
        detail = (
            str(item.get("removed"))
            if item.get("removed") is not None
            else f"{item.get('start', '')}~{item.get('end', '')}".strip("~")
        )
        if not detail:
            detail_parts = []
            for key in (
                "worst_account_loss",
                "cvar",
                "probability_drawdown_over_20pct",
                "probability_drawdown_over_30pct",
            ):
                if key in metrics:
                    detail_parts.append(f"{key}={_pct(metrics[key])}")
            detail = "; ".join(detail_parts) or "—"
        rows.append(
            [
                str(item.get("scenario_id", "")),
                str(item.get("dimension", "")),
                str(item.get("status", "")),
                _pct(metrics.get("cumulative_return")),
                _pct(metrics.get("cagr")),
                _pct(metrics.get("max_drawdown")),
                _num(metrics.get("calmar")),
                detail,
                ", ".join(str(reason) for reason in item.get("reasons", [])) or "—",
            ]
        )
    if not rows:
        return ["无场景。"]
    return _table(
        [
            "场景",
            "维度",
            "状态",
            "累计收益",
            "年化收益",
            "最大回撤",
            "Calmar",
            "补充结果",
            "原因",
        ],
        rows,
    )


def _attribution_table(items: object) -> list[str]:
    rows = [
        [str(item.get("key", "")), _pct(item.get("contribution"), 3)]
        for item in _rows(items)
    ]
    return _table(["项目", "算术收益贡献"], rows) if rows else ["无归因结果。"]


def _count_status(items: object) -> str:
    rows = _rows(items)
    passed = sum(row.get("status") == "pass" for row in rows)
    failed = sum(row.get("status") == "fail" for row in rows)
    missing = sum(row.get("status") == "evidence_insufficient" for row in rows)
    return f"共 {len(rows)} 项：通过 {passed}，失败 {failed}，证据不足 {missing}。"


def render_analysis_report(
    analysis: Mapping[str, Any],
    recommendation: Mapping[str, Any],
    vibe_evidence: Mapping[str, Any],
) -> str:
    baseline = _mapping(analysis.get("baseline"))
    metrics = _mapping(baseline.get("metrics"))
    risk = _mapping(baseline.get("risk_control"))
    benchmark_block = _mapping(analysis.get("benchmarks"))
    benchmark_stats = _mapping(benchmark_block.get("statistics"))
    attribution = _mapping(analysis.get("attribution"))
    robustness = _mapping(analysis.get("robustness"))
    evidence = _mapping(analysis.get("evidence_matrix"))
    best = recommendation.get("recommended_iteration_candidate") or "无"
    baseline_passed = baseline.get("status") == "pass"
    candidate_accepted = bool(recommendation.get("candidate_accepted"))
    if baseline_passed and candidate_accepted:
        decision_summary = (
            "冻结基线已通过全部研究门槛，证据矩阵也没有失败或证据不足；"
            "当前仅可进入人工确认。"
        )
        candidate_summary = (
            f"已测试场景中，`{best}` 的 Calmar（年化收益/最大回撤）最高且已通过门槛，"
            "但仍不自动替换基线或启动聚宽正式复核。"
        )
    elif baseline_passed:
        decision_summary = (
            "冻结基线已通过自身研究门槛，但其他挑战或证据仍未全部通过，"
            "当前方案不应视为已完成验证。"
        )
        candidate_summary = (
            f"已测试场景中，`{best}` 的 Calmar 最高，但只能作为后续人工复核对象。"
        )
    else:
        decision_summary = (
            "冻结基线未通过全部研究门槛，当前方案不应视为已通过。"
        )
        candidate_summary = (
            f"已测试场景中，`{best}` 表现最好，但仍只作为下一轮迭代候选，"
            "不自动替换基线。"
        )

    challenge_rows = _rows(analysis.get("challenge_results"))
    challenge_pass = sum(row.get("status") == "pass" for row in challenge_rows)
    challenge_fail = sum(row.get("status") == "fail" for row in challenge_rows)
    challenge_missing = sum(
        row.get("status") == "evidence_insufficient" for row in challenge_rows
    )
    if challenge_rows and challenge_pass == len(challenge_rows):
        challenge_summary = (
            f"{len(challenge_rows)} 个基础场景全部通过各自研究门槛；"
            "是否进入下一阶段仍需人工确认。"
        )
    elif challenge_rows and challenge_fail == len(challenge_rows):
        challenge_summary = (
            f"{len(challenge_rows)} 个基础场景均未通过研究门槛。"
            "表现最好的场景仍不能被视为已验证候选。"
        )
    else:
        challenge_summary = (
            f"基础场景中 {challenge_pass} 项通过、{challenge_fail} 项失败、"
            f"{challenge_missing} 项证据不足；不能用单个最佳场景替代整体判断。"
        )

    lines = [
        "# 本地策略完整分析报告",
        "",
        "## 1. 结论与推荐",
        "",
        f"推荐结论：`{recommendation.get('decision')}`。{decision_summary}",
        "",
        f"{candidate_summary} 下一步固定为 `{recommendation.get('next_action')}`，等待人工确认。",
        "",
        "## 2. 研究身份与边界",
        "",
        f"- 分析标识：`{analysis.get('analysis_id')}`",
        f"- 策略标识：`{analysis.get('strategy_id')}`",
        f"- 确定性分析耗时：{_num(analysis.get('analysis_seconds'), 2)} 秒",
        "- 权限：本地探索性研究，不是 JoinQuant（聚宽）正式回测、模拟交易或最终验收。",
        "- 事实源：标准分析数据包、双基准集和确定性证据矩阵。",
        "",
        "## 3. 收益与回撤",
        "",
        *_table(
            ["指标", "结果"],
            [
                ["累计收益", _pct(metrics.get("cumulative_return"))],
                ["年化收益", _pct(metrics.get("cagr"))],
                ["最大回撤", _pct(metrics.get("max_drawdown"))],
                ["最长回撤期（交易日）", _num(metrics.get("max_drawdown_duration"), 0)],
                ["年化波动率", _pct(metrics.get("annualized_volatility"))],
                ["Sharpe（夏普比率）", _num(metrics.get("sharpe"))],
                ["Sortino（索提诺比率）", _num(metrics.get("sortino"))],
                ["Calmar（年化收益/最大回撤）", _num(metrics.get("calmar"))],
                ["门槛状态", str(baseline.get("status", "unknown"))],
                ["未通过原因", ", ".join(baseline.get("reasons", [])) or "—"],
            ],
        ),
        "",
        "## 4. 双基准与 Alpha/Beta（超额收益/市场暴露）",
        "",
    ]
    benchmark_rows: list[list[str]] = []
    for benchmark_id, raw in benchmark_stats.items():
        item = _mapping(raw)
        benchmark_rows.append(
            [
                str(benchmark_id),
                _pct(item.get("strategy_return")),
                _pct(item.get("benchmark_return")),
                _pct(item.get("active_return")),
                _pct(item.get("alpha")),
                _num(item.get("beta"), 4),
                _num(item.get("correlation"), 4),
                _num(item.get("information_ratio"), 4),
                _num(item.get("up_capture"), 4),
                _num(item.get("down_capture"), 4),
            ]
        )
    lines.extend(
        _table(
            [
                "基准",
                "共同日策略收益",
                "基准收益",
                "主动收益",
                "Alpha",
                "Beta",
                "相关性",
                "信息比率",
                "上涨捕获",
                "下跌捕获",
            ],
            benchmark_rows,
        )
    )
    active_returns = [
        value
        for raw in benchmark_stats.values()
        if (value := _finite(_mapping(raw).get("active_return"))) is not None
    ]
    if active_returns and all(value > 0 for value in active_returns):
        benchmark_summary = (
            "全部基准的主动收益为正；仍需结合 Alpha（超额收益）、Beta（市场暴露）"
            "和风险指标判断是否具有可持续竞争力。"
        )
    elif active_returns and all(value < 0 for value in active_returns):
        benchmark_summary = (
            "全部基准的主动收益为负；低 Beta 或正 Alpha 不能抵消累计收益落后的事实。"
        )
    elif active_returns and any(value == 0 for value in active_returns):
        benchmark_summary = (
            "至少一个基准的主动收益为零；其余基准应分别结合 Alpha（超额收益）、"
            "Beta（市场暴露）和风险指标判断。"
        )
    elif active_returns:
        benchmark_summary = (
            "不同基准的主动收益有正有负；应分别结合 Alpha（超额收益）、"
            "Beta（市场暴露）和风险指标判断。"
        )
    else:
        benchmark_summary = "缺少可比较的主动收益，基准竞争力证据不足。"
    lines.extend(
        [
            "",
            benchmark_summary,
            "",
            "## 5. 仓位与风险控制",
            "",
            *_table(
                ["指标", "结果"],
                [
                    ["平均仓位", _pct(risk.get("average_invested_ratio"))],
                    ["中位仓位", _pct(risk.get("median_invested_ratio"))],
                    ["低于半仓的日期占比", _pct(risk.get("below_half_ratio"))],
                    ["接近满仓的日期占比", _pct(risk.get("near_full_ratio"))],
                    ["平均现金", _pct(risk.get("average_cash_ratio"))],
                    ["最高仓位", _pct(risk.get("maximum_invested_ratio"))],
                    ["最高单标的权重", _pct(risk.get("maximum_security_weight"))],
                    ["最高资产组权重", _pct(risk.get("maximum_asset_group_weight"))],
                    ["计划风险覆盖率", _pct(risk.get("planned_risk_coverage"))],
                    [
                        "最高计划损失比例",
                        _pct(risk.get("maximum_planned_loss_ratio")),
                    ],
                    [
                        "最高有效 N 风险单位",
                        _num(risk.get("maximum_effective_risk_units"), 2),
                    ],
                    [
                        "组合单位预算最高利用率",
                        _pct(risk.get("maximum_portfolio_unit_utilization")),
                    ],
                    ["最高60日已实现波动率", _pct(risk.get("maximum_realized_60d_volatility"))],
                ],
            ),
            "",
            "### 交易与成本",
            "",
            *_table(
                ["指标", "结果"],
                [
                    ["成交订单", _num(risk.get("filled_order_count"), 0)],
                    ["平仓订单", _num(risk.get("closed_order_count"), 0)],
                    ["平仓胜率", _pct(risk.get("closed_order_win_rate"))],
                    ["费用", _money(risk.get("fees"))],
                    ["保护止损事件", _num(risk.get("protective_stop_events"), 0)],
                    [
                        "全量仓位再分配事件",
                        _num(risk.get("redistribution_event_count"), 0),
                    ],
                ],
            ),
            "",
            "## 6. 归因分析",
            "",
            f"口径：{attribution.get('method', '—')}。限制：{attribution.get('limitation', '—')}。勾稽误差：{_num(attribution.get('reconciliation_error'), 8)}。",
            "",
            "### 证券归因",
            "",
            *_attribution_table(attribution.get("security")),
            "",
            "### 资产组归因",
            "",
            *_attribution_table(attribution.get("asset_group")),
            "",
            "### 交易原因归因",
            "",
            *_attribution_table(attribution.get("trading_reason")),
            "",
            "### 年度归因",
            "",
            *_attribution_table(attribution.get("period")),
            "",
            "## 7. 基础场景挑战",
            "",
            *_challenge_table(analysis),
            "",
            challenge_summary,
            "",
            "## 8. 稳健性与压力测试",
            "",
            (
                f"证据矩阵共 {int(evidence.get('rows', 0))} 项：通过 "
                f"{int(evidence.get('pass', 0))}，失败 {int(evidence.get('fail', 0))}，"
                f"证据不足 {int(evidence.get('evidence_insufficient', 0))}。"
            ),
            "",
        ]
    )
    robustness_sections = (
        ("时期与滚动窗口", "periods"),
        ("资产与资产组删除", "asset_deletions"),
        ("成本与延迟执行", "cost_execution"),
        ("区块抽样", "bootstrap"),
        ("历史压力", "historical_stress"),
        ("持仓冲击", "position_shocks"),
        ("CVaR（条件风险价值）", "cvar"),
    )
    for title, key in robustness_sections:
        items = robustness.get(key)
        lines.extend(
            [
                f"### {title}",
                "",
                _count_status(items),
                "",
                *_robustness_table(items),
                "",
            ]
        )
    opposing = _rows(analysis.get("opposing_evidence"))
    lines.extend(["## 9. 反对证据", ""])
    if opposing:
        for item in opposing:
            kind = str(item.get("kind", "unknown"))
            identity = item.get("scenario_id") or item.get("benchmark_id") or ""
            value = (
                _pct(item.get("active_return"))
                if item.get("active_return") is not None
                else ", ".join(str(reason) for reason in item.get("reasons", []))
            )
            lines.append(f"- {kind}：{identity}；{value or '—'}")
    else:
        lines.append("- 无。")
    violation = _mapping(vibe_evidence.get("boundary_violation"))
    swarm = _mapping(vibe_evidence.get("swarm"))
    single_agent = _mapping(vibe_evidence.get("single_agent"))
    use_result = _mapping(vibe_evidence.get("use_result"))
    if violation or swarm:
        vibe_lines = [
            "- Vibe 群体分析是已知缺陷路径，本次误调用已作为边界违规记录。",
            f"- 运行 `{swarm.get('run_id', '—')}` 的 `valid_evidence` 为 `{swarm.get('valid_evidence', False)}`，并已排除出全部结论。",
            f"- 边界原因：{violation.get('reason', '群体分析结果不得使用。')}",
            "- 已加载的绩效归因、风险分析和报告能力只是方法文档，不是实际单体分析结果。",
            "- 当前公开接口没有可用的单体策略结果分析入口，因此本报告不采用任何 Vibe 结论。",
        ]
    elif use_result.get("vibe_conclusion_available") and single_agent:
        assessment = _mapping(single_agent.get("assessment"))
        alignment = str(
            assessment.get("recommendation_alignment", "未提供推荐一致性说明。")
        )
        capabilities = ", ".join(
            str(item) for item in vibe_evidence.get("capabilities_loaded", [])
        )
        vibe_lines = [
            "- 本次未调用 Vibe 群体分析，群体结论未进入证据矩阵、报告或推荐。",
            f"- Vibe 单体复核运行 `{single_agent.get('run_id', '—')}` 已完成；公开入口为 `{single_agent.get('interface', '—')}`。",
            f"- 实际加载能力：{capabilities or '—'}。",
            f"- 定性复核：{alignment}",
            "- Vibe 结果权限为审计辅助，不替代确定性数值裁判，也不改变任何门槛状态。",
        ]
    else:
        vibe_lines = [
            "- 本次未调用 Vibe 群体分析，Vibe 群体结论未进入证据矩阵、报告或推荐。",
            f"- {use_result.get('reason', '本报告只采用确定性本地分析。')}",
        ]
    uncertainty_lines = [
        "- 本结果是本地探索性模拟，不是聚宽正式回测；平台撮合差异尚未复核。",
        "- 收益与权益采用连续经济总回报近似：连续因子只由上一交易日原始收盘价与当日原始前收盘价生成，公司行动元数据只用于审计，且可能包含事后核对记录。",
        "- 经济单位不等同于真实 ETF 份额；现金分红按除权日隐含再投资，未模拟支付日现金、税费和零碎份额，因此不能与聚宽逐日账户精确对账。",
        "- 固定时期和滚动窗口是基线既有路径切片，不是从空仓和初始资金重新回测。",
        "- 资产与资产组删除是收益贡献删除敏感性，不重新分配资金。",
        "- 成本与延迟场景采用一阶订单级敏感性估算，不是完整交易路径重跑。",
        "- 归因为日度算术贡献，不是几何链式归因；不能直接与复利累计收益逐项相加解释。",
    ]
    stop_failure = next(
        (
            row
            for row in _rows(_mapping(analysis.get("robustness")).get("position_shocks"))
            if row.get("scenario_id") == "shock-stop-failure"
        ),
        None,
    )
    if stop_failure is not None and stop_failure.get("status") == "evidence_insufficient":
        uncertainty_lines.append(
            "- `shock-stop-failure` 缺少所需来源输入，保留为证据不足，未用假设值补齐。"
        )
    uncertainty_lines.extend(
        [
            "- 60日已实现波动率是事后诊断，不等同于下单时协方差预测门禁。",
            "- 双基准只在三方共同交易日计算，策略共同日收益与全样本收益不同。",
        ]
    )
    if recommendation.get("decision") == "proceed_to_joinquant":
        confirmation_summary = (
            "建议人工确认是否进入 JoinQuant（聚宽）正式复核；该确认不等于冻结策略、"
            "启动模拟交易或接受本地结果为正式结论。"
        )
    else:
        confirmation_summary = (
            f"建议人工确认“修改后再评估”，并把 `{best}` 作为下一轮研究起点，"
            "而不是直接采用、冻结或送入聚宽正式回测。"
        )
    lines.extend(
        [
            "",
            "## 10. Vibe 安全边界",
            "",
            *vibe_lines,
            "",
            "## 11. 不确定性",
            "",
            *uncertainty_lines,
            "",
            "## 12. 人工确认",
            "",
            f"当前停止状态：`{recommendation.get('next_action')}`。{confirmation_summary}",
            "",
        ]
    )
    return "\n".join(lines)


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


def write_analysis_delivery(workspace: Path) -> dict[str, Any]:
    root = Path(workspace).resolve()
    analysis_path = root / "deterministic-analysis.json"
    vibe_path = root / "vibe-evidence.json"
    report_path = root / "local-strategy-analysis-report.md"
    recommendation_path = root / "recommendation.json"

    analysis = _read_document(analysis_path, "deterministic analysis")
    vibe = enforce_vibe_boundary(_read_document(vibe_path, "Vibe evidence"))
    analysis_seconds = _finite(analysis.get("analysis_seconds"))
    if analysis_seconds is None or analysis_seconds < 0:
        raise ValueError("deterministic analysis must record analysis_seconds")
    vibe["analysis_seconds"] = analysis_seconds
    recommendation = build_recommendation(analysis)
    report = render_analysis_report(analysis, recommendation, vibe)

    _write_json(vibe_path, vibe)
    _atomic_write(report_path, report)
    recommendation["artifacts"] = {
        "deterministic_analysis_sha256": _sha256(analysis_path),
        "vibe_evidence_sha256": _sha256(vibe_path),
        "report_sha256": _sha256(report_path),
    }
    _write_json(recommendation_path, recommendation)
    return {
        "analysis_id": recommendation["analysis_id"],
        "decision": recommendation["decision"],
        "report": report_path.as_posix(),
        "recommendation": recommendation_path.as_posix(),
        "vibe_evidence": vibe_path.as_posix(),
        "next_action": recommendation["next_action"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic local strategy analysis delivery"
    )
    parser.add_argument("--workspace", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    print(
        json.dumps(
            write_analysis_delivery(args.workspace),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
