from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path

import pytest


RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))

from turtle_etf.vectorbt_callbacks import CallbackInputs, CallbackParams  # noqa: E402
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
        "delayed_backend": "vectorbt.Portfolio.from_orders",
        "adapter_version": "local-vectorbt-adapter/2",
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
        "joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_delayed.py",
        "joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py",
        "scripts/research/market_data/economic_returns.py",
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
        "signal": {"add_step_n": 0.5, "stop_n": 2.0, "max_units": 4},
        "risk": {
            "unit_risk_per_n": 0.005,
            "asset_group_unit_cap": 6.0,
            "portfolio_unit_cap": 12.0,
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
        "asset_group_ids",
    )
    assert CallbackParams._fields == (
        "lot_size",
        "unit_risk_per_n",
        "add_step_n",
        "stop_n",
        "max_units",
        "asset_group_unit_cap",
        "portfolio_unit_cap",
        "commission_multiplier",
        "one_way_slippage",
    )
    assert params.max_units == 4
    assert params.asset_group_unit_cap == 6.0
    assert params.portfolio_unit_cap == 12.0


def _new_config() -> dict[str, object]:
    return {
        "research": {"initial_cash": 1_000_000.0},
        "signal": {"add_step_n": 0.5, "stop_n": 2.0, "max_units": 4},
        "risk": {
            "unit_risk_per_n": 0.01,
            "asset_group_unit_cap": 6.0,
            "portfolio_unit_cap": 12.0,
        },
    }


def test_max_units_requires_exactly_four() -> None:
    config = _new_config()

    _, params = _params(config)
    assert params.unit_risk_per_n == 0.01
    assert params.max_units == 4

    for invalid in (None, True, False, 0, -1, 1.5, 3, 5):
        config["signal"]["max_units"] = invalid
        with pytest.raises(ValueError, match="max_units must equal four"):
            _params(config)

    del config["signal"]["max_units"]
    with pytest.raises(ValueError, match="missing config value: max_units"):
        _params(config)


@pytest.mark.parametrize(
    "legacy_field",
    (
        "security_risk_cap",
        "security_value_cap",
        "asset_group_risk_cap",
        "asset_group_value_cap",
        "portfolio_risk_cap",
        "portfolio_value_cap",
        "covariance",
        "target_volatility",
        "risk_reduction_target_volatility",
        "minimum_aligned_samples",
    ),
)
def test_legacy_risk_fields_are_rejected(legacy_field: str) -> None:
    config = _new_config()
    config["risk"][legacy_field] = (
        {"method": "sample", "window_days": 60}
        if legacy_field == "covariance"
        else 1.0
    )

    with pytest.raises(ValueError, match="legacy risk fields are not supported"):
        _params(config)
