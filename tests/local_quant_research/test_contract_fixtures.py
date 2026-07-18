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
    assert baseline["execution"]["additional_delay_days"] == 0
