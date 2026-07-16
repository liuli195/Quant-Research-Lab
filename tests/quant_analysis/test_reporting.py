from __future__ import annotations

import json
from pathlib import Path

from scripts.research.quant_analysis.reporting import (
    build_recommendation,
    enforce_vibe_boundary,
    render_analysis_report,
    write_analysis_delivery,
)


def test_vibe_swarm_is_recorded_as_invalid_and_excluded() -> None:
    evidence = {
        "capabilities_loaded": ["performance-attribution", "risk-analysis"],
        "forbidden_capabilities_called": [],
        "swarm": {
            "run_id": "swarm-1",
            "preset": "portfolio_review_board",
            "status": "running_without_progress",
            "final_report": None,
        },
        "use_result": {},
    }

    corrected = enforce_vibe_boundary(evidence)

    assert corrected["forbidden_capabilities_called"] == [
        "run_swarm:portfolio_review_board"
    ]
    assert corrected["swarm"]["valid_evidence"] is False
    assert corrected["swarm"]["excluded_from_conclusions"] is True
    assert corrected["boundary_violation"]["occurred"] is True
    assert corrected["use_result"]["vibe_conclusion_available"] is False
    assert corrected["use_result"]["loaded_capabilities_are_methodology_only"] is True
    assert corrected["next_action"] == "generate_deterministic_local_report"


def test_vibe_not_called_is_recorded_without_a_boundary_violation() -> None:
    corrected = enforce_vibe_boundary(
        {
            "capabilities_loaded": [],
            "forbidden_capabilities_called": [],
            "use_result": {"vibe_called": False},
        }
    )

    assert corrected["forbidden_capabilities_called"] == []
    assert "boundary_violation" not in corrected
    assert corrected["use_result"]["vibe_called"] is False
    assert corrected["use_result"]["vibe_conclusion_available"] is False
    assert corrected["use_result"]["loaded_capabilities_are_methodology_only"] is False
    assert corrected["use_result"]["reason"] == "本次未调用 Vibe。"


def test_vibe_public_single_agent_is_valid_qualitative_audit_evidence() -> None:
    corrected = enforce_vibe_boundary(
        {
            "capabilities_loaded": [
                "performance-attribution",
                "risk-analysis",
                "report-generate",
            ],
            "forbidden_capabilities_called": [],
            "single_agent": {
                "interface": "vibe-trading-cli-run",
                "run_id": "vibe-run-1",
                "status": "completed",
                "assessment": {
                    "status": "completed",
                    "recommendation_alignment": "建议修改后再评估。",
                },
            },
        }
    )

    assert corrected["single_agent"]["valid_evidence"] is True
    assert corrected["single_agent"]["qualitative_only"] is True
    assert corrected["use_result"]["vibe_called"] is True
    assert corrected["use_result"]["vibe_conclusion_available"] is True
    assert corrected["use_result"]["loaded_capabilities_are_methodology_only"] is False
    assert corrected["authority"] == "audit_only"
    assert "boundary_violation" not in corrected


def test_report_includes_real_vibe_single_agent_review_without_numerical_authority() -> None:
    vibe = enforce_vibe_boundary(
        {
            "capabilities_loaded": ["performance-attribution", "risk-analysis"],
            "forbidden_capabilities_called": [],
            "single_agent": {
                "interface": "vibe-trading-cli-run",
                "run_id": "vibe-run-1",
                "status": "completed",
                "assessment": {
                    "status": "completed",
                    "recommendation_alignment": "修改后再评估与证据一致。",
                },
            },
        }
    )

    report = render_analysis_report(_analysis(), build_recommendation(_analysis()), vibe)

    assert "Vibe 单体复核" in report
    assert "vibe-run-1" in report
    assert "修改后再评估与证据一致" in report
    assert "不替代确定性数值裁判" in report


def _analysis() -> dict[str, object]:
    return {
        "analysis_id": "analysis-1",
        "strategy_id": "strategy-003",
        "authority": "local_exploratory",
        "not_formal_joinquant_backtest": True,
        "analysis_seconds": 1.5,
        "baseline": {
            "status": "fail",
            "reasons": ["calmar_min"],
            "metrics": {
                "cumulative_return": 0.293995,
                "cagr": 0.019105,
                "max_drawdown": -0.100971,
                "calmar": 0.18921,
                "sharpe": 0.3901,
                "sortino": 0.5111,
                "annualized_volatility": 0.05201,
                "max_drawdown_duration": 946,
            },
            "risk_control": {
                "average_invested_ratio": 0.263,
                "median_invested_ratio": 0.303,
                "below_half_ratio": 0.855,
                "near_full_ratio": 0.0017,
                "average_cash_ratio": 0.737,
                "maximum_invested_ratio": 1.0,
                "maximum_security_weight": 0.4,
                "maximum_asset_group_weight": 0.5,
                "planned_risk_coverage": 1.0,
                "maximum_planned_loss_ratio": 0.0325,
                "maximum_effective_risk_units": 12.0,
                "maximum_portfolio_unit_utilization": 1.0,
                "maximum_realized_60d_volatility": 0.1835,
                "filled_order_count": 438,
                "closed_order_count": 201,
                "closed_order_win_rate": 0.5423,
                "fees": 7454.77,
                "protective_stop_events": 65,
                "redistribution_event_count": 3245,
            },
        },
        "benchmarks": {
            "alignment": {"common_samples": 3276},
            "statistics": {
                "CSI300_CNY_TOTAL_RETURN": {
                    "strategy_return": 0.3309,
                    "benchmark_return": 0.8766,
                    "active_return": -0.5457,
                    "alpha": 0.0175,
                    "beta": 0.0789,
                    "correlation": 0.3527,
                    "information_ratio": -0.2389,
                    "up_capture": 0.0262,
                    "down_capture": 0.1808,
                }
            },
        },
        "attribution": {
            "method": "arithmetic",
            "limitation": "not geometric linking",
            "reconciliation_error": 0.0,
            "security": [{"key": "513100.XSHG", "contribution": 0.11}],
            "asset_group": [{"key": "equity", "contribution": 0.14}],
            "trading_reason": [{"key": "entry_breakout", "contribution": 0.15}],
            "period": [{"key": "2024", "contribution": 0.06}],
            "event_counts": {"entry_breakout": 99},
        },
        "challenge_results": [
            {
                "scenario_id": "baseline",
                "status": "fail",
                "metrics": {
                    "cumulative_return": 0.294,
                    "cagr": 0.0191,
                    "max_drawdown": -0.101,
                    "calmar": 0.189,
                    "average_invested_ratio": 0.263,
                },
                "reasons": ["calmar_min"],
                "cold_seconds": 28.4,
                "warm_seconds": 3.7,
            },
            {
                "scenario_id": "covariance-ewma-30d",
                "status": "fail",
                "metrics": {
                    "cumulative_return": 0.3338,
                    "cagr": 0.0214,
                    "max_drawdown": -0.0956,
                    "calmar": 0.224,
                    "average_invested_ratio": 0.2517,
                },
                "reasons": ["calmar_min"],
                "cold_seconds": 27.9,
                "warm_seconds": 3.5,
            },
        ],
        "robustness": {
            "periods": [
                {
                    "scenario_id": "period-a",
                    "dimension": "fixed_period",
                    "status": "fail",
                    "metrics": {"cagr": 0.01, "max_drawdown": -0.05, "calmar": 0.2},
                    "reasons": ["calmar_min"],
                }
            ],
            "asset_deletions": [],
            "cost_execution": [],
            "bootstrap": [],
            "historical_stress": [],
            "position_shocks": [
                {
                    "scenario_id": "shock-stop-failure",
                    "dimension": "position_shock",
                    "status": "evidence_insufficient",
                    "metrics": {},
                    "reasons": ["missing_source_input"],
                }
            ],
            "cvar": [],
        },
        "evidence_matrix": {
            "rows": 3,
            "pass": 0,
            "fail": 2,
            "evidence_insufficient": 1,
        },
        "opposing_evidence": [
            {
                "kind": "benchmark_underperformance",
                "benchmark_id": "CSI300_CNY_TOTAL_RETURN",
                "active_return": -0.5457,
            }
        ],
    }


def test_recommendation_keeps_best_failed_challenge_as_iteration_only() -> None:
    recommendation = build_recommendation(_analysis())

    assert recommendation["decision"] == "revise_and_reassess"
    assert recommendation["recommended_iteration_candidate"] == "covariance-ewma-30d"
    assert recommendation["candidate_accepted"] is False
    assert recommendation["next_action"] == "human_confirmation_required"


def test_report_is_complete_and_explicitly_excludes_vibe_group_analysis() -> None:
    vibe = enforce_vibe_boundary(
        {
            "capabilities_loaded": ["report-generate"],
            "swarm": {"preset": "portfolio_review_board", "run_id": "swarm-1"},
        }
    )
    recommendation = build_recommendation(_analysis())

    report = render_analysis_report(_analysis(), recommendation, vibe)

    for heading in (
        "结论与推荐",
        "收益与回撤",
        "双基准与 Alpha/Beta",
        "仓位与风险控制",
        "归因分析",
        "基础场景挑战",
        "稳健性与压力测试",
        "反对证据",
        "Vibe 安全边界",
        "不确定性",
        "人工确认",
    ):
        assert heading in report
    assert "covariance-ewma-30d" in report
    assert "群体分析" in report
    assert "排除" in report
    assert "既有路径切片" in report
    assert "不重新分配资金" in report
    assert "连续经济总回报近似" in report
    assert "公司行动元数据只用于审计" in report
    assert "经济单位不等同于真实 ETF 份额" in report
    assert "不能与聚宽逐日账户精确对账" in report
    assert "最高计划损失比例" in report
    assert "最高有效 N 风险单位" in report
    assert "组合单位预算最高利用率" in report
    assert "全量仓位再分配事件" in report
    assert "高于入场上限" not in report
    assert "高于目标" not in report
    assert "human_confirmation_required" in report


def test_report_states_vibe_was_not_called_and_uses_actual_stop_shock_status() -> None:
    analysis = _analysis()
    analysis["robustness"]["position_shocks"][0] = {
        "scenario_id": "shock-stop-failure",
        "dimension": "position_shock",
        "status": "pass",
        "metrics": {"maximum_loss": -0.08},
        "reasons": [],
    }
    vibe = enforce_vibe_boundary(
        {
            "capabilities_loaded": [],
            "forbidden_capabilities_called": [],
            "use_result": {"vibe_called": False},
        }
    )

    report = render_analysis_report(analysis, build_recommendation(analysis), vibe)

    assert "本次未调用 Vibe 群体分析" in report
    assert "本次误调用" not in report
    assert "`shock-stop-failure` 缺少所需来源输入" not in report


def test_report_adapts_to_all_passed_results_and_positive_benchmarks() -> None:
    analysis = _analysis()
    analysis["baseline"]["status"] = "pass"
    analysis["baseline"]["reasons"] = []
    for challenge in analysis["challenge_results"]:
        challenge["status"] = "pass"
        challenge["reasons"] = []
        challenge["metrics"]["calmar"] = 0.8
    analysis["evidence_matrix"] = {
        "rows": 3,
        "pass": 3,
        "fail": 0,
        "evidence_insufficient": 0,
    }
    analysis["benchmarks"]["statistics"]["CSI300_CNY_TOTAL_RETURN"][
        "active_return"
    ] = 0.05
    analysis["opposing_evidence"] = []
    recommendation = build_recommendation(analysis)

    report = render_analysis_report(analysis, recommendation, {})

    assert recommendation["decision"] == "proceed_to_joinquant"
    assert "冻结基线已通过全部研究门槛" in report
    assert "基础场景全部通过" in report
    assert "全部基准的主动收益为正" in report
    assert "冻结基线未通过" not in report
    assert "基础场景均未通过" not in report
    assert "均明显落后" not in report


def test_report_adapts_to_mixed_challenge_and_benchmark_results() -> None:
    analysis = _analysis()
    analysis["baseline"]["status"] = "pass"
    analysis["baseline"]["reasons"] = []
    analysis["challenge_results"][0]["status"] = "pass"
    analysis["challenge_results"][0]["reasons"] = []
    analysis["benchmarks"]["statistics"]["SECOND_BENCHMARK"] = {
        **analysis["benchmarks"]["statistics"]["CSI300_CNY_TOTAL_RETURN"],
        "active_return": 0.04,
    }

    report = render_analysis_report(
        analysis,
        build_recommendation(analysis),
        {},
    )

    assert "基础场景中 1 项通过、1 项失败、0 项证据不足" in report
    assert "不同基准的主动收益有正有负" in report
    assert "基础场景均未通过" not in report
    assert "均明显落后" not in report


def test_report_describes_zero_active_return_without_calling_it_mixed() -> None:
    analysis = _analysis()
    analysis["benchmarks"]["statistics"]["CSI300_CNY_TOTAL_RETURN"][
        "active_return"
    ] = 0.0

    report = render_analysis_report(analysis, build_recommendation(analysis), {})

    assert "至少一个基准的主动收益为零" in report
    assert "不同基准的主动收益有正有负" not in report


def test_write_delivery_records_analysis_seconds_in_report_and_vibe_evidence(
    tmp_path: Path,
) -> None:
    analysis = _analysis()
    analysis["analysis_seconds"] = 7.25
    (tmp_path / "deterministic-analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "vibe-evidence.json").write_text(
        json.dumps(
            {
                "capabilities_loaded": [],
                "forbidden_capabilities_called": [],
                "use_result": {"vibe_called": False},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    write_analysis_delivery(tmp_path)

    vibe = json.loads((tmp_path / "vibe-evidence.json").read_text(encoding="utf-8"))
    report = (tmp_path / "local-strategy-analysis-report.md").read_text(
        encoding="utf-8"
    )
    assert vibe["analysis_seconds"] == 7.25
    assert "确定性分析耗时：7.25 秒" in report


def test_write_delivery_persists_report_recommendation_and_corrected_vibe(
    tmp_path: Path,
) -> None:
    (tmp_path / "deterministic-analysis.json").write_text(
        json.dumps(_analysis(), ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "vibe-evidence.json").write_text(
        json.dumps(
            {
                "capabilities_loaded": ["report-generate"],
                "swarm": {
                    "preset": "portfolio_review_board",
                    "run_id": "swarm-1",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = write_analysis_delivery(tmp_path)

    assert result["next_action"] == "human_confirmation_required"
    assert (tmp_path / "local-strategy-analysis-report.md").is_file()
    recommendation = json.loads(
        (tmp_path / "recommendation.json").read_text(encoding="utf-8")
    )
    vibe = json.loads((tmp_path / "vibe-evidence.json").read_text(encoding="utf-8"))
    assert recommendation["decision"] == "revise_and_reassess"
    assert recommendation["artifacts"]["report_sha256"]
    assert vibe["swarm"]["excluded_from_conclusions"] is True
