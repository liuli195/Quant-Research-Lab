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
    }
    assert baseline["risk"] == {
        "risk_per_unit": 0.005,
        "security_risk_cap": 0.0125,
        "security_value_cap": 0.30,
        "asset_group_risk_cap": 0.025,
        "asset_group_value_cap": 0.50,
        "portfolio_risk_cap": 0.05,
        "portfolio_value_cap": 1.0,
        "covariance": {"method": "sample", "window_days": 60},
        "target_volatility": 0.10,
        "risk_reduction_target_volatility": 0.095,
        "minimum_aligned_samples": 60,
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
            "money",
            "factor",
            "paused",
            "high_limit",
            "low_limit",
        ],
    }
    assert baseline["joinquant_export"] == {
        "api_source": "research_runtime_injected",
        "apis": ["get_price", "write_file", "read_file"],
        "pandas_version": "0.23.4",
        "csv_line_terminator_argument": "line_terminator",
        "paused_source_dtype": "float64",
        "paused_normalized_dtype": "bool",
        "start_date_policy": "security_first_complete_trading_day",
        "end_date_field": "snapshot_end_date",
        "remote_readback_sha256_required": True,
        "remote_cleanup_required": True,
    }
    assert baseline["execution"]["order_priority"] == [
        "full_exit",
        "mandatory_risk_reduction",
        "entry_or_addition",
    ]
    assert baseline["execution"]["allocation"] == "a1_uniform_completion"
    assert baseline["execution"]["acceptance_fixture"] == {
        "same_security_exit_cancels_buys": True,
        "standard_request_limit": "one_u0",
        "uniform_completion_ratio": True,
        "capped_budget_redistribution": True,
        "lot_rounding": "floor_then_largest_remainder",
        "recheck_hard_caps_each_lot": True,
        "tie_breaker": "security_code_ascending",
        "input_order_invariant": True,
    }


def test_candidates_are_baseline_plus_six_single_factor_challenges(
    repo_root: Path,
) -> None:
    document = json.loads(
        (_research_dir(repo_root) / "candidates.json").read_text(encoding="utf-8")
    )
    candidates = document["candidates"]

    assert document["schema_version"] == 1
    assert document["baseline_config"] == "baseline.json"
    assert [item["id"] for item in candidates] == [
        "baseline",
        "entry-40",
        "entry-60",
        "stop-1.5n",
        "stop-2.5n",
        "covariance-120d",
        "covariance-ewma-30d",
    ]
    assert [item["overrides"] for item in candidates] == [
        {},
        {"signal.entry_days": 40},
        {"signal.entry_days": 60},
        {"signal.stop_n": 1.5},
        {"signal.stop_n": 2.5},
        {"risk.covariance": {"method": "sample", "window_days": 120}},
        {"risk.covariance": {"method": "ewma", "half_life_days": 30}},
    ]
    assert all(len(item["overrides"]) <= 1 for item in candidates)
    assert all("rank" not in item and "score" not in item for item in candidates)
