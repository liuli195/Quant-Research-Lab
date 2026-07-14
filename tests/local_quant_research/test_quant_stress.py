from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from scripts.research.quant_analysis.contracts import (
    STANDARD_TABLES,
    validate_analysis_bundle,
    write_analysis_table,
)
from scripts.research.quant_analysis.cvar import (
    calculate_cvar,
    calculate_cvar_scenarios,
    rolling_compound_returns,
)
from scripts.research.quant_analysis.stress import (
    HISTORICAL_WINDOWS,
    POSITION_SHOCKS,
    calculate_historical_stress,
    calculate_position_shocks,
)


def test_fixed_stress_catalog_has_five_windows_and_four_position_shocks() -> None:
    assert len(HISTORICAL_WINDOWS) == 5
    assert len(POSITION_SHOCKS) == 4
    assert {row["scenario_id"] for row in POSITION_SHOCKS} == {
        "shock-synchronous-equity-crash",
        "shock-market-liquidity",
        "shock-cross-border-gap",
        "shock-stop-failure",
    }


def _bundle(
    tmp_path: Path,
    rows: dict[str, list[dict[str, object]]],
):
    for name in STANDARD_TABLES:
        write_analysis_table(name, rows[name], tmp_path)
    return validate_analysis_bundle(tmp_path)


def test_historical_stress_uses_actual_window_and_reports_missing_evidence(
    tmp_path: Path,
    analysis_rows: dict[str, list[dict[str, object]]],
) -> None:
    bundle = _bundle(tmp_path, analysis_rows)
    windows = (
        {
            "scenario_id": "covered",
            "start_date": "2026-01-05",
            "end_date": "2026-01-08",
            "max_drawdown_abs_max": 0.15,
        },
        {
            "scenario_id": "missing",
            "start_date": "2020-01-01",
            "end_date": "2020-12-31",
            "max_drawdown_abs_max": 0.15,
        },
    )

    covered, missing = calculate_historical_stress(bundle, windows)

    assert covered.metrics["max_drawdown"] == pytest.approx(-0.1)
    assert covered.metrics["recovery_duration"] == 1
    assert covered.metrics["unfilled_events"] == 0
    assert covered.status == "pass"
    assert missing.status == "evidence_insufficient"
    assert missing.reasons == ("missing_window",)


def test_position_shocks_include_group_security_and_stop_failure_losses() -> None:
    positions = (
        {
            "date": "2026-01-05",
            "security": "ETF-A",
            "asset_group": "equity",
            "weight": 0.6,
            "market_value": 60.0,
            "stop_failure_loss": 12.0,
        },
        {
            "date": "2026-01-05",
            "security": "ETF-B",
            "asset_group": "bond",
            "weight": 0.4,
            "market_value": 40.0,
            "stop_failure_loss": 4.0,
        },
    )
    shocks = (
        {
            "scenario_id": "group-shock",
            "asset_group_shocks": {"equity": -0.2, "bond": 0.0},
            "maximum_loss_abs_max": 0.15,
        },
        {
            "scenario_id": "security-shock",
            "asset_group_shocks": {"equity": -0.08, "bond": 0.0},
            "security_shocks": {"ETF-A": -0.25},
            "maximum_loss_abs_max": 0.15,
        },
        {
            "scenario_id": "stop-failure",
            "use_stop_failure_loss": True,
            "maximum_loss_abs_max": 0.15,
        },
    )

    group, security, stop = calculate_position_shocks(positions, shocks)

    assert group.metrics["worst_account_loss"] == pytest.approx(0.12)
    assert group.status == "pass"
    assert security.metrics["worst_account_loss"] == pytest.approx(0.15)
    assert security.status == "pass"
    assert stop.metrics["worst_account_loss"] == pytest.approx(0.16)
    assert stop.status == "fail"


def test_cvar_uses_exact_tail_mass_and_compounds_five_days() -> None:
    returns = np.array([-0.10, -0.10, -0.05, 0.0, 0.01], dtype=np.float64)

    assert calculate_cvar(returns, 0.8) == pytest.approx(0.10)
    compounded = rolling_compound_returns(
        np.array([0.1, -0.1, 0.1, -0.1, 0.1, 0.0], dtype=np.float64),
        window=5,
    )
    assert compounded[0] == pytest.approx(1.1 * 0.9 * 1.1 * 0.9 * 1.1 - 1.0)
    assert compounded.shape == (2,)

    tied_boundary = np.array(
        [-0.10, -0.05, -0.05, -0.05, 0.0, 0.0, 0.0, 0.0, 0.01, 0.01],
        dtype=np.float64,
    )
    assert calculate_cvar(tied_boundary, 0.7) == pytest.approx(0.20 / 3.0)


def test_three_cvar_scenarios_use_fixed_thresholds() -> None:
    returns = np.array(
        [-0.05, -0.03, -0.02, 0.0, 0.01, 0.02] * 20,
        dtype=np.float64,
    )

    results = calculate_cvar_scenarios(returns)

    assert [row.scenario_id for row in results] == [
        "cvar-1d-95",
        "cvar-1d-99",
        "cvar-5d-95",
    ]
    assert [row.metrics["threshold"] for row in results] == [0.025, 0.04, 0.05]


def test_cvar_scenarios_require_twenty_effective_tail_observations() -> None:
    results = calculate_cvar_scenarios(np.zeros(500, dtype=np.float64))

    statuses = {row.scenario_id: row.status for row in results}
    assert statuses == {
        "cvar-1d-95": "pass",
        "cvar-1d-99": "evidence_insufficient",
        "cvar-5d-95": "pass",
    }
    assert calculate_cvar_scenarios(np.zeros(2_500, dtype=np.float64))[1].status == "pass"
