from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path


RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))

from turtle_etf.vectorbt_callbacks import CallbackInputs  # noqa: E402
from turtle_etf.vectorbt_engine import _params  # noqa: E402


def test_vectorbt_runtime_is_pinned_and_available(repo_root: Path) -> None:
    requirements = {
        line.strip()
        for line in (repo_root / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    shared_requirements = {
        line.strip()
        for line in (
            repo_root / ".agents/skills/joinquant-archive-sync/requirements.txt"
        ).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "vectorbt==1.1.0" in requirements
    assert "numpy==2.4.6" in shared_requirements
    assert "pandas==3.0.3" in shared_requirements
    assert not any(line.startswith(("numpy==", "pandas==")) for line in requirements)
    assert importlib.util.find_spec("vectorbt") is not None
    assert importlib.metadata.version("vectorbt") == "1.1.0"


def test_vectorbt_execution_identity_and_license_are_auditable(repo_root: Path) -> None:
    research_root = repo_root / "joinquant/strategies/strategy-003/research"
    callback_path = research_root / "turtle_etf/vectorbt_callbacks.py"
    identity = json.loads(
        (research_root / "code-identity.json").read_text(encoding="utf-8")
    )
    execution = identity["execution"]

    assert execution == {
        "backend": "vectorbt.Portfolio.from_order_func",
        "adapter_version": "local-vectorbt-adapter/1",
        "dependencies": {
            "vectorbt": "1.1.0",
            "numba": "0.66.0",
            "numpy": "2.4.6",
            "pandas": "3.0.3",
        },
        "callbacks_sha256": hashlib.sha256(callback_path.read_bytes()).hexdigest(),
        "accounting": {
            "version": "turtle-etf-corporate-actions/1",
            "corporate_action_mode": "point_in_time_total_return_approximation",
            "continuity_factor_basis": "raw_previous_close_over_current_pre_close",
            "corporate_action_metadata_timing": "audit_only_may_be_retrospective",
            "price_basis": "continuous_economic_price",
            "quantity_basis": "economic_units",
            "cash_dividend_mode": "implicit_reinvestment_on_ex_date",
            "pay_date_cash_supported": False,
            "exact_joinquant_reconciliation": False,
        },
        "license": {
            "expression": "Apache-2.0 WITH Commons-Clause",
            "usage": "internal_research_only",
            "resale_prohibited": True,
        },
    }
    identity_paths = {item["path"] for item in identity["files"]}
    assert {
        "joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_inputs.py",
        "joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_callbacks.py",
        "joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py",
    }.issubset(identity_paths)

    distribution = importlib.metadata.distribution("vectorbt")
    license_file = next(
        file for file in distribution.files or () if str(file).endswith("LICENSE.md")
    )
    license_text = distribution.locate_file(license_file).read_text(encoding="utf-8")
    assert "Apache License" in license_text
    assert "Commons Clause" in license_text


def test_callback_contract_contains_only_strategy_inputs_and_risk_parameters() -> None:
    config = {
        "research": {"initial_cash": 1_000_000.0},
        "signal": {"add_step_n": 0.5, "stop_n": 2.0},
        "risk": {
            "risk_per_unit": 0.01,
            "security_risk_cap": 0.02,
            "security_value_cap": 0.30,
            "asset_group_risk_cap": 0.04,
            "asset_group_value_cap": 0.50,
            "portfolio_risk_cap": 0.10,
            "portfolio_value_cap": 1.00,
            "target_volatility": 0.20,
            "risk_reduction_target_volatility": 0.15,
        },
    }

    _, params = _params(config)

    assert CallbackInputs._fields == (
        "execution_open",
        "signal_close",
        "signal_entry_high",
        "signal_exit_low",
        "signal_n",
        "paused",
        "high_limit",
        "low_limit",
        "covariance",
        "covariance_eligible",
        "asset_group_ids",
    )
    assert params._fields == (
        "lot_size",
        "risk_per_unit",
        "add_step_n",
        "stop_n",
        "security_risk_cap",
        "security_value_cap",
        "asset_group_risk_cap",
        "asset_group_value_cap",
        "portfolio_risk_cap",
        "portfolio_value_cap",
        "target_volatility",
        "risk_reduction_target_volatility",
        "commission_multiplier",
        "one_way_slippage",
    )
