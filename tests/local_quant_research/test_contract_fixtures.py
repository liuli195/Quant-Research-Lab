from __future__ import annotations

import json
from pathlib import Path


EXPECTED_UNIVERSE = {
    "510300.XSHG": "china_sync_equity",
    "512100.XSHG": "china_sync_equity",
    "512480.XSHG": "china_sync_equity",
    "159819.XSHE": "china_sync_equity",
    "516160.XSHG": "china_sync_equity",
    "513100.XSHG": "cross_border_tech_equity",
    "513180.XSHG": "cross_border_tech_equity",
    "515180.XSHG": "china_dividend",
    "516080.XSHG": "china_innovative_drug",
    "518880.XSHG": "gold",
    "511010.XSHG": "treasury_bond",
}


def _research_dir(repo_root: Path) -> Path:
    return repo_root / "joinquant" / "strategies" / "strategy-003" / "research"


def test_baseline_freezes_universe_rules_and_price_semantics(repo_root: Path) -> None:
    baseline = json.loads(
        (_research_dir(repo_root) / "baseline.json").read_text(encoding="utf-8")
    )

    assert baseline["schema_version"] == 1
    assert baseline["project_id"] == "strategy-003"
    assert {
        item["security"]: item["asset_group"] for item in baseline["universe"]
    } == EXPECTED_UNIVERSE
    assert baseline["signal"] == {
        "entry_days": 55,
        "exit_days": 20,
        "n_days": 20,
        "add_step_n": 0.5,
        "stop_n": 2.0,
        "max_units": 4,
    }
    assert baseline["risk"] == {
        "unit_risk_per_n": 0.01,
        "asset_group_unit_cap": 6.0,
        "portfolio_unit_cap": 12.0,
    }
    assert baseline["market_data"] == {
        "source": "joinquant",
        "asset_type": "etf",
        "frequency": "1d",
        "fq": None,
        "skip_paused": False,
        "use_real_price": False,
        "fields": [
            "date",
            "security",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "volume",
            "factor",
            "paused",
            "high_limit",
            "low_limit",
        ],
    }
    assert baseline["joinquant_export"] == {
        "api_source": "research_runtime_and_jqdata",
        "apis": [
            "get_price",
            "query",
            "finance.FUND_DIVIDEND",
            "write_file",
            "read_file",
        ],
        "pandas_version": "0.23.4",
        "csv_line_terminator_argument": "line_terminator",
        "paused_source_dtype": "float64",
        "paused_normalized_dtype": "bool",
        "start_date_policy": "security_first_complete_trading_day",
        "end_date_field": "snapshot_end_date",
        "remote_readback_sha256_required": True,
        "remote_cleanup_required": True,
    }
    assert baseline["execution"] == {
        "additional_delay_days": 0,
        "order_priority": [
            "full_exit",
            "redistribution_sell",
            "entry_or_addition",
            "redistribution_buy",
        ],
        "allocation": "full_position_redistribution",
        "acceptance_fixture": {
            "same_security_exit_cancels_buys": True,
            "candidate_requires_net_buy_lot": True,
            "group_unit_cap": 6.0,
            "portfolio_unit_cap": 12.0,
            "cash_scaling": "uniform",
            "lot_rounding": "floor",
            "residual_cash_redistribution": False,
            "input_order_invariant": True,
        },
    }


def test_analysis_plan_is_baseline_plus_six_single_factor_challenges(
    repo_root: Path,
) -> None:
    document = json.loads(
        (_research_dir(repo_root) / "analysis-plan.json").read_text(encoding="utf-8")
    )
    scenarios = document["scenarios"]

    assert document["schema_version"] == "strategy-analysis-plan/1"
    assert document["baseline_config"].endswith("/baseline.json")
    assert [item["scenario_id"] for item in scenarios] == [
        "baseline",
        "entry-40",
        "entry-60",
        "stop-1-5n",
        "stop-2-5n",
        "group-unit-cap-5",
        "portfolio-unit-cap-10",
    ]
    assert [item["overrides"] for item in scenarios] == [
        {},
        {"signal": {"entry_days": 40}},
        {"signal": {"entry_days": 60}},
        {"signal": {"stop_n": 1.5}},
        {"signal": {"stop_n": 2.5}},
        {"risk": {"asset_group_unit_cap": 5.0}},
        {"risk": {"portfolio_unit_cap": 10.0}},
    ]
    assert all(len(item["overrides"]) <= 1 for item in scenarios)
    assert all("rank" not in item and "score" not in item for item in scenarios)


def test_obsolete_challenge_analysis_plan_is_absent(repo_root: Path) -> None:
    assert not (_research_dir(repo_root) / "challenge-analysis-plan.json").exists()


def test_authoritative_docs_confirm_the_same_new_baseline(repo_root: Path) -> None:
    paths = (
        repo_root
        / "docs/superpowers/specs/2026-07-16-turtle-full-position-redistribution-design.md",
        repo_root / "docs/research/2026-07-13-turtle-etf-system-final-plan.md",
        repo_root
        / "openspec/changes/build-turtle-etf-local-research-workflow/design.md",
        repo_root
        / "openspec/changes/build-turtle-etf-local-research-workflow/specs/turtle-etf-local-research/spec.md",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "已确认" in text
        assert "11 只 ETF" in text
        assert "55/20/20" in text
        assert "全量仓位再分配" in text
        assert "4/6/12" in text
        assert "180 秒" in text


def test_local_research_performance_baseline_freezes_release_gate(
    repo_root: Path,
) -> None:
    fixture = json.loads(
        (
            repo_root
            / "tests/local_quant_research/fixtures/performance-baseline.json"
        ).read_text(encoding="utf-8")
    )

    assert fixture["schema_version"] == 1
    assert fixture["environment"] == {"python": "3.12", "vectorbt": "1.1.0"}
    assert fixture["sampling"] == {
        "cold_processes": 3,
        "warm_runs": 5,
        "statistic": "median",
    }
    assert fixture["limits"] == {
        "relative_ratio": 1.05,
        "absolute_seconds": 180.0,
    }
    assert fixture["collection"] == {
        "python": ".venv/Scripts/python.exe",
        "entrypoint": (
            "joinquant/strategies/strategy-003/research/"
            "turtle_etf/vectorbt_cli.py"
        ),
        "memory_method": "ctypes.GetProcessMemoryInfo",
        "cold_process_model": "independent_process_per_sample",
        "warm_process_model": "same_process_for_all_samples",
    }
    assert tuple(fixture["scenarios"]) == (
        "immediate-11-etf",
        "immediate-17-etf",
        "delayed-11-etf-1d",
    )
