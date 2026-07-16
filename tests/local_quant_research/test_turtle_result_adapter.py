from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest


RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))

from scripts.research.analysis_data import open_analysis_source  # noqa: E402
from turtle_etf.result_adapter import (  # noqa: E402
    ATTRIBUTION_FIELDS,
    ATTRIBUTION_SCHEMA_VERSION,
    ResultContractError,
    to_joinquant_facts,
    validate_turtle_result,
    validate_turtle_attribution,
    write_local_result,
)
from turtle_etf.vectorbt_callbacks import (  # noqa: E402
    ACTION_ADDITION,
    ACTION_ENTRY,
    ACTION_FULL_EXIT,
    ACTION_REDISTRIBUTION_SELL,
    REASON_ENTRY_BREAKOUT,
    REASON_FULL_POSITION_REDISTRIBUTION,
    REASON_ORDER_REJECTED,
    REASON_PROTECTIVE_STOP,
    REASON_TREND_EXIT,
)
from turtle_etf.vectorbt_engine import run_vectorbt_simulation  # noqa: E402
from turtle_etf.vectorbt_inputs import SimulationInputs  # noqa: E402


class _Portfolio:
    def value(self) -> pd.Series:
        return pd.Series([9_990.0, 10_180.0])

    def cash(self) -> pd.Series:
        return pd.Series([6_990.0, 9_130.0])


def _readonly(values: object, dtype: str) -> np.ndarray:
    result = np.ascontiguousarray(values, dtype=dtype)
    result.setflags(write=False)
    return result


def _simulation() -> tuple[SimpleNamespace, SimpleNamespace]:
    inputs = SimpleNamespace(
        dates=np.asarray(["2026-01-05", "2026-01-06"], dtype="datetime64[D]"),
        securities=("ETF-A", "ETF-B"),
        close=np.asarray([[10.0, 20.0], [11.0, 21.0]], dtype=np.float64),
        signal_n=np.asarray([[1.0, 1.0], [1.0, 1.0]], dtype=np.float64),
    )
    simulation = SimpleNamespace(
        initial_cash=10_000.0,
        portfolio=_Portfolio(),
        action_codes=np.asarray(
            [
                [ACTION_ENTRY, ACTION_ENTRY],
                [ACTION_FULL_EXIT, ACTION_REDISTRIBUTION_SELL],
            ],
            dtype=np.int16,
        ),
        reason_codes=np.asarray(
            [
                [REASON_ENTRY_BREAKOUT, REASON_ENTRY_BREAKOUT],
                [
                    REASON_PROTECTIVE_STOP,
                    REASON_FULL_POSITION_REDISTRIBUTION,
                ],
            ],
            dtype=np.int16,
        ),
        requested_quantities=np.asarray([[100, 100], [100, 50]], dtype=np.int64),
        planned_quantities=np.asarray([[100, 100], [100, 50]], dtype=np.int64),
        filled_quantities=np.asarray([[100, 100], [100, 50]], dtype=np.int64),
        fill_prices=np.asarray([[10.0, 20.0], [11.0, 21.0]], dtype=np.float64),
        fees=np.asarray([[5.0, 5.0], [5.0, 5.0]], dtype=np.float64),
        state_quantities=np.asarray([[100, 100], [0, 50]], dtype=np.int64),
        state_common_stop=np.asarray([[8.0, 18.0], [np.nan, 18.0]], dtype=np.float64),
        state_next_add_index=np.asarray([[1, 1], [0, 1]], dtype=np.int64),
        state_unit_counts=np.asarray([[1, 1], [0, 1]], dtype=np.int64),
        candidate_base_quantities=np.asarray(
            [[100, 100], [0, 0]], dtype=np.int64
        ),
        event_group_scales=np.asarray(
            [[1.0, 1.0], [1.0, 0.75]], dtype=np.float64
        ),
        event_portfolio_scales=np.asarray(
            [1.0, 12.0 / 13.0], dtype=np.float64
        ),
        event_cash_scales=np.asarray([1.0, 0.9], dtype=np.float64),
        portfolio_unit_cap=12.0,
    )
    return inputs, simulation


def test_attribution_exposes_unit_and_redistribution_evidence() -> None:
    inputs, simulation = _simulation()

    facts = to_joinquant_facts(inputs, simulation, scenario_id="baseline")
    decisions = [
        row
        for row in facts.attribution.to_pylist()
        if row["event_type"] == "decision"
    ]
    entry = next(
        row
        for row in decisions
        if row["time"].startswith("2026-01-05")
        and row["security"] == "ETF-A"
    )
    entry_details = json.loads(entry["details_json"])
    assert entry_details["candidate_base_quantity"] == 100
    assert entry_details["frozen_signal_n"] == 1.0
    assert entry_details["actual_fill_price"] == 10.0
    assert entry_details["unit_count_after"] == 1
    assert entry_details["common_stop_after"] == 8.0

    redistribution = next(
        row
        for row in decisions
        if row["reason_code"] == "full_position_redistribution"
    )
    details = json.loads(redistribution["details_json"])
    assert details["unit_count_after"] == 1
    assert details["group_scale"] == pytest.approx(0.75)
    assert details["portfolio_scale"] == pytest.approx(12.0 / 13.0)
    assert details["cash_scale"] == pytest.approx(0.9)
    assert details["redistribution_state_changed"] is False


def _delayed_inputs(
    *,
    delayed_open: float = 12.0,
    rows: int = 3,
) -> SimulationInputs:
    dates = np.arange(
        np.datetime64("2026-01-05"),
        np.datetime64("2026-01-05") + np.timedelta64(rows, "D"),
    )
    opens = np.asarray([[10.0], *([[delayed_open]] * (rows - 1))], dtype=np.float64)
    signal_close = np.full((rows, 1), np.nan, dtype=np.float64)
    signal_entry_high = np.full((rows, 1), np.nan, dtype=np.float64)
    signal_n = np.full((rows, 1), 999.0, dtype=np.float64)
    signal_close[0, 0] = 11.0
    signal_entry_high[0, 0] = 10.0
    signal_n[0, 0] = 1.5
    return SimulationInputs(
        dates=_readonly(dates, "datetime64[D]"),
        securities=("ETF-A",),
        asset_groups=("group-a",),
        asset_group_ids=_readonly([0], "int64"),
        raw_open=_readonly(opens, "float64"),
        raw_high=_readonly(opens, "float64"),
        raw_low=_readonly(opens, "float64"),
        raw_close=_readonly(opens, "float64"),
        raw_pre_close=_readonly(opens, "float64"),
        continuous_open=_readonly(opens, "float64"),
        continuous_high=_readonly(opens, "float64"),
        continuous_low=_readonly(opens, "float64"),
        continuous_close=_readonly(opens, "float64"),
        continuous_pre_close=_readonly(opens, "float64"),
        continuity_factor=_readonly(np.ones((rows, 1)), "float64"),
        corporate_action_applied=_readonly(np.zeros((rows, 1)), "bool"),
        corporate_actions_digest="4" * 64,
        corporate_action_applications=(),
        paused=_readonly(np.zeros((rows, 1)), "bool"),
        high_limit=_readonly(np.full((rows, 1), np.nan), "float64"),
        low_limit=_readonly(np.full((rows, 1), np.nan), "float64"),
        signal_source_index=_readonly(np.arange(rows) - 1, "int64"),
        signal_close=_readonly(signal_close, "float64"),
        signal_entry_high=_readonly(signal_entry_high, "float64"),
        signal_exit_low=_readonly(np.full((rows, 1), np.nan), "float64"),
        signal_n=_readonly(signal_n, "float64"),
    )


def _delayed_config(*, initial_cash: float = 100_000.0) -> dict[str, object]:
    return {
        "research": {"initial_cash": initial_cash},
        "signal": {"add_step_n": 0.5, "stop_n": 2.0, "max_units": 4},
        "risk": {
            "unit_risk_per_n": 0.025,
            "asset_group_unit_cap": 6.0,
            "portfolio_unit_cap": 12.0,
        },
        "costs": {"commission_multiplier": 1.0, "one_way_slippage": 0.0},
        "execution": {"additional_delay_days": 1},
    }


def test_result_adapter_writes_joinquant_shaped_package(tmp_path: Path) -> None:
    inputs, simulation = _simulation()
    facts = to_joinquant_facts(inputs, simulation, scenario_id="baseline")
    code = tmp_path / "entry.py"
    code.write_text("print('local research')\n", encoding="utf-8")
    backtest_dir = tmp_path / "run-1" / "backtests" / "local-1"

    package = write_local_result(
        backtest_dir,
        facts=facts,
        run_id="run-1",
        local_backtest_id="local-1",
        scenario_id="baseline",
        snapshot_id="a" * 64,
        corporate_actions_sha256="e" * 64,
        code_path=code,
        params={"scenario_id": "baseline", "research": {"initial_cash": 10_000}},
        performance={"status": "pass", "cold_seconds": 1.2, "warm_seconds": 0.4},
    )

    assert package.root == backtest_dir.resolve()
    expected = {
        "manifest.json",
        "code.py",
        "params.json",
        "performance.json",
        f"params_versions/{package.params_sha256}.json",
        "data/results.parquet",
        "data/balances.parquet",
        "data/positions.parquet",
        "data/orders.parquet",
        f"data/attribution_log-{package.attribution_sha256}.parquet",
    }
    actual = {
        path.relative_to(backtest_dir).as_posix()
        for path in backtest_dir.rglob("*")
        if path.is_file()
    }
    assert actual == expected
    assert not any(
        (backtest_dir / relative).exists()
        for relative in (
            "data/risk.parquet",
            "data/period_risks.parquet",
            "data/equity.parquet",
            "data/trades.parquet",
            "raw",
        )
    )

    source = open_analysis_source(backtest_dir)
    assert source.kind == "local_backtest"
    manifest = source.manifest
    assert manifest["source"]["accounting"] == {
        "version": "turtle-etf-corporate-actions/1",
        "corporate_action_mode": "point_in_time_total_return_approximation",
        "continuity_factor_basis": "raw_previous_close_over_current_pre_close",
        "corporate_action_metadata_timing": "audit_only_may_be_retrospective",
        "price_basis": "continuous_economic_price",
        "quantity_basis": "economic_units",
        "cash_dividend_mode": "implicit_reinvestment_on_ex_date",
        "pay_date_cash_supported": False,
        "exact_joinquant_reconciliation": False,
        "corporate_actions_sha256": "e" * 64,
    }
    assert manifest["run"] == {
        "run_id": "run-1",
        "scenario_id": "baseline",
        "snapshot_id": "a" * 64,
    }
    attribution = manifest["extensions"]["turtle_etf"]["attribution_log"]
    reference = attribution["files"][0]
    assert attribution["required"] is True
    assert attribution["status"] == "complete"
    assert attribution["schema_version"] == ATTRIBUTION_SCHEMA_VERSION
    assert reference["path"].endswith(f"{reference['sha256']}.parquet")
    assert hashlib.sha256((backtest_dir / reference["path"]).read_bytes()).hexdigest() == reference[
        "sha256"
    ]


def test_adapter_accepts_real_vectorbt_portfolio() -> None:
    inputs = SimulationInputs(
        dates=_readonly(["2026-01-05", "2026-01-06"], "datetime64[D]"),
        securities=("ETF-A",),
        asset_groups=("group-a",),
        asset_group_ids=_readonly([0], "int64"),
        raw_open=_readonly([[10.0], [10.5]], "float64"),
        raw_high=_readonly([[10.0], [10.5]], "float64"),
        raw_low=_readonly([[10.0], [10.5]], "float64"),
        raw_close=_readonly([[10.0], [10.5]], "float64"),
        raw_pre_close=_readonly([[10.0], [10.0]], "float64"),
        continuous_open=_readonly([[10.0], [10.5]], "float64"),
        continuous_high=_readonly([[10.0], [10.5]], "float64"),
        continuous_low=_readonly([[10.0], [10.5]], "float64"),
        continuous_close=_readonly([[10.0], [10.5]], "float64"),
        continuous_pre_close=_readonly([[10.0], [10.0]], "float64"),
        continuity_factor=_readonly([[1.0], [1.0]], "float64"),
        corporate_action_applied=_readonly([[False], [False]], "bool"),
        corporate_actions_digest="4" * 64,
        corporate_action_applications=(),
        paused=_readonly([[False], [False]], "bool"),
        high_limit=_readonly([[np.nan], [np.nan]], "float64"),
        low_limit=_readonly([[np.nan], [np.nan]], "float64"),
        signal_source_index=_readonly([-1, 0], "int64"),
        signal_close=_readonly([[11.0], [np.nan]], "float64"),
        signal_entry_high=_readonly([[10.0], [np.nan]], "float64"),
        signal_exit_low=_readonly([[np.nan], [np.nan]], "float64"),
        signal_n=_readonly([[1.0], [1.0]], "float64"),
    )
    config = {
        "research": {"initial_cash": 10_000.0},
        "signal": {"add_step_n": 0.5, "stop_n": 2.0, "max_units": 4},
        "risk": {
            "unit_risk_per_n": 0.025,
            "asset_group_unit_cap": 6.0,
            "portfolio_unit_cap": 12.0,
        },
        "costs": {"commission_multiplier": 1.0, "one_way_slippage": 0.0},
    }

    simulation = run_vectorbt_simulation(inputs, config)
    facts = to_joinquant_facts(inputs, simulation, scenario_id="real-vectorbt")

    assert facts.orders.num_rows == 1
    assert facts.positions.num_rows == 2
    assert facts.results["returns"].to_pylist()[0] == pytest.approx(-0.0005)
    validate_turtle_attribution(facts)


def test_delayed_order_keeps_planned_and_execution_dates_and_frozen_evidence() -> None:
    inputs = _delayed_inputs()
    simulation = run_vectorbt_simulation(inputs, _delayed_config())

    facts = to_joinquant_facts(inputs, simulation, scenario_id="delayed")

    order = facts.orders.to_pylist()[0]
    assert order["entrust_time"] == "2026-01-05 09:30:00"
    assert order["match_time"] == "2026-01-06 09:30:00"
    assert order["finish_time"] == order["match_time"]
    assert order["time"] == order["match_time"]
    assert order["amount"] == int(simulation.planned_quantities[1, 0])
    assert order["filled"] == int(simulation.filled_quantities[1, 0])
    decision = next(
        row
        for row in facts.attribution.to_pylist()
        if row["event_type"] == "decision"
    )
    details = json.loads(decision["details_json"])
    assert details["planned_date"] == "2026-01-05"
    assert details["execution_date"] == "2026-01-06"
    assert details["delay_days"] == 1
    assert details["frozen_reason"] == "entry_breakout"
    assert details["frozen_target_amount"] == order["amount"]
    assert details["frozen_signal_n"] == 1.5
    assert details["execution_adjustment"] == "none"


def test_delayed_partial_fill_preserves_frozen_order_amount() -> None:
    inputs = _delayed_inputs(delayed_open=200.0)
    simulation = run_vectorbt_simulation(
        inputs, _delayed_config(initial_cash=25_000.0)
    )

    facts = to_joinquant_facts(inputs, simulation, scenario_id="delayed-partial")

    order = facts.orders.to_pylist()[0]
    assert order["status"] == "done"
    assert order["comment"] == "cash_truncated"
    assert order["filled"] == 100
    assert order["amount"] > order["filled"]
    decision = next(
        row
        for row in facts.attribution.to_pylist()
        if row["event_type"] == "decision"
    )
    assert json.loads(decision["details_json"])["execution_adjustment"] == (
        "cash_truncated"
    )


def test_delayed_horizon_expiry_is_attribution_only() -> None:
    inputs = _delayed_inputs(rows=1)
    simulation = run_vectorbt_simulation(inputs, _delayed_config())

    facts = to_joinquant_facts(inputs, simulation, scenario_id="delayed-expired")

    assert facts.orders.num_rows == 0
    expired = [
        row
        for row in facts.attribution.to_pylist()
        if json.loads(row["details_json"]).get("execution_adjustment")
        == "horizon_expired"
    ]
    assert len(expired) == 1
    assert expired[0]["time"] == "2026-01-05 09:30:00"
    assert expired[0]["requested_amount"] > 0
    assert expired[0]["executed_amount"] == 0


def test_physical_fields_and_cross_table_facts_match_joinquant_contract(
    tmp_path: Path,
) -> None:
    inputs, simulation = _simulation()
    facts = to_joinquant_facts(inputs, simulation, scenario_id="baseline")

    assert facts.results.schema.names == ["benchmark_returns", "returns", "time"]
    assert facts.balances.schema.names == [
        "total_value",
        "net_value",
        "cash",
        "aval_cash",
        "time",
    ]
    assert facts.positions.schema.names == [
        "pindex",
        "avg_cost",
        "margin",
        "amount",
        "today_amount",
        "hold_cost",
        "side",
        "price",
        "gains",
        "daily_gains",
        "closeable_amount",
        "time",
        "security_name",
        "security",
    ]
    assert facts.orders.schema.names == [
        "match_time",
        "pindex",
        "cancel_time",
        "action",
        "limit_price",
        "comment",
        "entrust_time",
        "finish_time",
        "side",
        "price",
        "commission",
        "gains",
        "type",
        "time",
        "security_name",
        "security",
        "filled",
        "amount",
        "status",
    ]
    assert facts.attribution.schema.names == list(ATTRIBUTION_FIELDS)
    assert facts.results["benchmark_returns"].null_count == facts.results.num_rows
    assert facts.results["returns"].to_pylist() == pytest.approx([-0.001, 0.018])
    assert facts.balances["total_value"].to_pylist() == [9_990.0, 10_180.0]
    assert facts.orders.num_rows == 4
    assert sum(facts.orders["filled"].to_pylist()) == 350
    assert facts.attribution.num_rows == 8
    validate_turtle_attribution(facts)


def test_corporate_action_application_is_audited_without_fake_order_or_cash() -> None:
    inputs, simulation = _simulation()
    inputs.corporate_action_applications = (
        SimpleNamespace(
            source_event_id="FUND_DIVIDEND:101",
            security="ETF-A",
            event_type="split",
            effective_date="2026-01-05",
            application_date="2026-01-06",
            announcement_date="2026-01-05",
            knowledge_cutoff_date="2026-01-10",
            split_ratio=2.0,
            cash_per_share=None,
            cumulative_factor=2.0,
            price_basis_changed=True,
            source="joinquant.finance.FUND_DIVIDEND",
            source_record_sha256="b" * 64,
        ),
    )

    facts = to_joinquant_facts(inputs, simulation, scenario_id="corporate-action")

    rows = [
        row
        for row in facts.attribution.to_pylist()
        if row["event_type"] == "corporate_action"
    ]
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "corporate_action_applied"
    assert rows[0]["requested_amount"] is None
    assert rows[0]["executed_amount"] is None
    details = json.loads(rows[0]["details_json"])
    assert details == {
        "announcement_date": "2026-01-05",
        "cash_per_share": None,
        "corporate_action_mode": "point_in_time_total_return_approximation",
        "cumulative_factor": 2.0,
        "effective_date": "2026-01-05",
        "application_date": "2026-01-06",
        "event_type": "split",
        "evidence_timing": "point_in_time",
        "knowledge_cutoff_date": "2026-01-10",
        "source": "joinquant.finance.FUND_DIVIDEND",
        "source_event_id": "FUND_DIVIDEND:101",
        "source_record_sha256": "b" * 64,
        "split_ratio": 2.0,
        "price_basis_changed": True,
    }
    assert not any(
        order["time"].startswith("2026-01-06")
        and order["comment"] == "corporate_action"
        for order in facts.orders.to_pylist()
    )
    assert facts.attribution.num_rows == 9
    validate_turtle_attribution(facts)


def test_attribution_uses_exact_openspec_fields_and_parseable_details() -> None:
    inputs, simulation = _simulation()

    facts = to_joinquant_facts(inputs, simulation, scenario_id="baseline")

    assert ATTRIBUTION_FIELDS == (
        "time",
        "event_id",
        "scope",
        "security",
        "event_type",
        "reason_code",
        "requested_amount",
        "executed_amount",
        "reference_price",
        "risk_before",
        "risk_after",
        "details_json",
    )
    assert facts.attribution.schema.names == list(ATTRIBUTION_FIELDS)
    rows = facts.attribution.to_pylist()
    assert len({row["event_id"] for row in rows}) == len(rows)
    for row in rows:
        assert row["scope"] == "security"
        assert isinstance(json.loads(row["details_json"]), dict)
    decision = next(
        row
        for row in rows
        if row["time"].startswith("2026-01-06")
        and row["security"] == "ETF-A"
        and row["event_type"] == "decision"
    )
    assert decision["reason_code"] == "protective_stop"
    assert decision["risk_before"] == pytest.approx(200.0)
    assert decision["risk_after"] == pytest.approx(0.0)


def test_security_daily_pnl_reconciles_entry_partial_exit_and_full_exit() -> None:
    inputs, simulation = _simulation()

    facts = to_joinquant_facts(inputs, simulation, scenario_id="baseline")

    held_pnl = {
        (row["time"][:10], row["security"]): row["daily_gains"]
        for row in facts.positions.to_pylist()
    }
    assert held_pnl == pytest.approx(
        {
            ("2026-01-05", "ETF-A"): -5.0,
            ("2026-01-05", "ETF-B"): -5.0,
            ("2026-01-06", "ETF-B"): 95.0,
        }
    )
    assert ("2026-01-06", "ETF-A") not in held_pnl

    valuations = [
        row for row in facts.attribution.to_pylist() if row["event_type"] == "valuation"
    ]
    daily_security_pnl = {
        (row["time"][:10], row["security"]): json.loads(row["details_json"])[
            "security_daily_pnl"
        ]
        for row in valuations
    }
    assert daily_security_pnl == pytest.approx(
        {
            ("2026-01-05", "ETF-A"): -5.0,
            ("2026-01-05", "ETF-B"): -5.0,
            ("2026-01-06", "ETF-A"): 95.0,
            ("2026-01-06", "ETF-B"): 95.0,
        }
    )
    assert sum(
        value
        for (date, _), value in daily_security_pnl.items()
        if date == "2026-01-05"
    ) == pytest.approx(-10.0)
    assert sum(
        value
        for (date, _), value in daily_security_pnl.items()
        if date == "2026-01-06"
    ) == pytest.approx(190.0)


def test_security_daily_pnl_prices_additions_and_trend_exit_at_execution() -> None:
    inputs = SimpleNamespace(
        dates=np.asarray(
            ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"],
            dtype="datetime64[D]",
        ),
        securities=("ETF-A",),
        close=np.asarray([[10.0], [11.0], [12.0], [11.0]], dtype=np.float64),
        signal_n=np.asarray([[1.0], [1.0], [1.0], [1.0]], dtype=np.float64),
    )
    simulation = SimpleNamespace(
        initial_cash=10_000.0,
        portfolio=SimpleNamespace(
            value=lambda: pd.Series([10_049.0, 10_173.0, 10_362.0, 10_256.0]),
            cash=lambda: pd.Series([9_049.0, 8_523.0, 9_522.0, 10_256.0]),
        ),
        action_codes=np.asarray(
            [
                [ACTION_ENTRY],
                [ACTION_ADDITION],
                [ACTION_REDISTRIBUTION_SELL],
                [ACTION_FULL_EXIT],
            ],
            dtype=np.int16,
        ),
        reason_codes=np.asarray(
            [
                [REASON_ENTRY_BREAKOUT],
                [REASON_ENTRY_BREAKOUT],
                [REASON_FULL_POSITION_REDISTRIBUTION],
                [REASON_TREND_EXIT],
            ],
            dtype=np.int16,
        ),
        requested_quantities=np.asarray([[100], [50], [80], [70]], dtype=np.int64),
        planned_quantities=np.asarray([[100], [50], [80], [70]], dtype=np.int64),
        filled_quantities=np.asarray([[100], [50], [80], [70]], dtype=np.int64),
        fill_prices=np.asarray([[9.5], [10.5], [12.5], [10.5]], dtype=np.float64),
        fees=np.asarray([[1.0], [1.0], [1.0], [1.0]], dtype=np.float64),
        state_quantities=np.asarray([[100], [150], [70], [0]], dtype=np.int64),
        state_common_stop=np.asarray([[8.0], [9.0], [9.0], [np.nan]], dtype=np.float64),
        state_next_add_index=np.asarray([[1], [2], [2], [0]], dtype=np.int64),
    )

    facts = to_joinquant_facts(inputs, simulation, scenario_id="path-dependent")

    assert facts.positions["daily_gains"].to_pylist() == pytest.approx(
        [49.0, 124.0, 189.0]
    )
    valuation_rows = [
        row for row in facts.attribution.to_pylist() if row["event_type"] == "valuation"
    ]
    valuation_pnl = [
        json.loads(row["details_json"])["security_daily_pnl"]
        for row in valuation_rows
    ]
    assert valuation_pnl == pytest.approx([49.0, 124.0, 189.0, -106.0])
    final_details = json.loads(valuation_rows[-1]["details_json"])
    assert valuation_rows[-1]["reason_code"] == "signal_exit"
    assert final_details["source_reason"] == "trend_exit"
    assert final_details["position_after"] == 0


def test_adapter_rejects_security_pnl_that_does_not_match_portfolio_change() -> None:
    inputs, simulation = _simulation()
    simulation.portfolio = SimpleNamespace(
        value=lambda: pd.Series([9_990.0, 10_181.0]),
        cash=lambda: pd.Series([6_990.0, 9_130.0]),
    )

    with pytest.raises(ResultContractError, match="daily PnL"):
        to_joinquant_facts(inputs, simulation, scenario_id="broken-pnl")


def test_attribution_rejects_unknown_reason_and_uncovered_order() -> None:
    inputs, simulation = _simulation()
    facts = to_joinquant_facts(inputs, simulation, scenario_id="baseline")
    document = facts.attribution.to_pydict()
    document["reason_code"][0] = "unknown_reason"
    invalid = facts.with_attribution(document)
    with pytest.raises(ResultContractError, match="reason"):
        validate_turtle_attribution(invalid)

    document = facts.attribution.slice(1).to_pydict()
    uncovered = facts.with_attribution(document)
    with pytest.raises(ResultContractError, match="cover"):
        validate_turtle_attribution(uncovered)


def test_rejected_vectorbt_order_is_preserved_as_canceled_order() -> None:
    inputs, simulation = _simulation()
    simulation.action_codes[1, 0] = ACTION_ADDITION
    simulation.reason_codes[1, 0] = REASON_ORDER_REJECTED
    simulation.filled_quantities[1, 0] = 0
    simulation.fill_prices[1, 0] = np.nan
    simulation.fees[1, 0] = 0.0
    simulation.state_quantities[1, 0] = 100
    simulation.state_common_stop[1, 0] = 8.0
    simulation.state_next_add_index[1, 0] = 1
    simulation.portfolio = SimpleNamespace(
        value=lambda: pd.Series([9_990.0, 10_185.0]),
        cash=lambda: pd.Series([6_990.0, 8_035.0]),
    )

    facts = to_joinquant_facts(inputs, simulation, scenario_id="rejected-order")

    canceled = [
        item for item in facts.orders.to_pylist() if item["status"] == "canceled"
    ]
    assert canceled == [
        {
            "match_time": None,
            "pindex": 0,
            "cancel_time": "2026-01-06 09:30:00",
            "action": "open",
            "limit_price": 0.0,
            "comment": "order_rejected",
            "entrust_time": "2026-01-06 09:30:00",
            "finish_time": None,
            "side": "long",
            "price": 0.0,
            "commission": 0.0,
            "gains": 0.0,
            "type": "market",
            "time": "2026-01-06 09:30:00",
            "security_name": "ETF-A",
            "security": "ETF-A",
            "filled": 0,
            "amount": 100,
            "status": "canceled",
        }
    ]
    validate_turtle_attribution(facts)


def test_params_current_and_version_are_byte_identical(tmp_path: Path) -> None:
    inputs, simulation = _simulation()
    facts = to_joinquant_facts(inputs, simulation, scenario_id="baseline")
    code = tmp_path / "entry.py"
    code.write_text("pass\n", encoding="utf-8")
    root = tmp_path / "backtest"
    package = write_local_result(
        root,
        facts=facts,
        run_id="run-1",
        local_backtest_id="local-1",
        scenario_id="baseline",
        snapshot_id="b" * 64,
        corporate_actions_sha256="e" * 64,
        code_path=code,
        params={"z": 2, "a": 1},
        performance={"status": "pass"},
    )

    current = (root / "params.json").read_bytes()
    version = (root / "params_versions" / f"{package.params_sha256}.json").read_bytes()
    assert current == version
    assert hashlib.sha256(current).hexdigest() == package.params_sha256
    assert json.loads(current) == {"a": 1, "z": 2}


def test_existing_output_directory_is_never_overwritten(tmp_path: Path) -> None:
    inputs, simulation = _simulation()
    facts = to_joinquant_facts(inputs, simulation, scenario_id="baseline")
    code = tmp_path / "entry.py"
    code.write_text("pass\n", encoding="utf-8")
    root = tmp_path / "backtest"
    root.mkdir()

    with pytest.raises(ResultContractError, match="already exists"):
        write_local_result(
            root,
            facts=facts,
            run_id="run-1",
            local_backtest_id="local-1",
            scenario_id="baseline",
            snapshot_id="c" * 64,
            corporate_actions_sha256="e" * 64,
            code_path=code,
            params={"scenario_id": "baseline"},
            performance={"status": "pass"},
        )


def test_project_validator_rejects_missing_attribution_declaration(
    tmp_path: Path,
) -> None:
    inputs, simulation = _simulation()
    facts = to_joinquant_facts(inputs, simulation, scenario_id="baseline")
    code = tmp_path / "entry.py"
    code.write_text("pass\n", encoding="utf-8")
    root = tmp_path / "backtest"
    write_local_result(
        root,
        facts=facts,
        run_id="run-1",
        local_backtest_id="local-1",
        scenario_id="baseline",
        snapshot_id="d" * 64,
        corporate_actions_sha256="e" * 64,
        code_path=code,
        params={"scenario_id": "baseline"},
        performance={"status": "pass"},
    )
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["extensions"] = {}
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ResultContractError, match="attribution declaration"):
        validate_turtle_result(root)
