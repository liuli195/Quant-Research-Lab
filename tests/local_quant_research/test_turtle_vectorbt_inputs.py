from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

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

from turtle_etf.vectorbt_engine import run_vectorbt_simulation  # noqa: E402
from turtle_etf.vectorbt_inputs import prepare_simulation_inputs  # noqa: E402


def _frame(
    security: str,
    close: list[float],
    *,
    money: float | None = 250_000_000.0,
) -> pd.DataFrame:
    dates = pd.date_range("2026-01-05", periods=len(close), freq="D")
    high = np.asarray(close, dtype=np.float64) + 1.0
    low = np.asarray(close, dtype=np.float64) - 1.0
    data: dict[str, object] = {
        "date": dates.strftime("%Y-%m-%d"),
        "security": security,
        "open": np.asarray(close, dtype=np.float64) + 0.25,
        "high": high,
        "low": low,
        "close": close,
        "pre_close": np.r_[close[0], close[:-1]],
        "paused": False,
        "high_limit": high + 1.0,
        "low_limit": low - 1.0,
    }
    if money is not None:
        data["money"] = np.full(len(close), money, dtype=np.float64)
    return pd.DataFrame(data)


def _config() -> dict[str, object]:
    return {
        "universe": [
            {"security": "ETF-B", "asset_group": "group-b"},
            {"security": "ETF-A", "asset_group": "group-a"},
        ],
        "signal": {"entry_days": 2, "exit_days": 2, "n_days": 2},
        "risk": {},
        "execution": {"additional_delay_days": 0},
    }


def _simulation_config() -> dict[str, object]:
    config = _config()
    config["research"] = {"initial_cash": 1_000_000.0}
    config["signal"] = {
        **config["signal"],
        "add_step_n": 0.5,
        "stop_n": 2.0,
        "max_units": 4,
    }
    config["risk"] = {
        "lot_size": 100,
        "unit_risk_per_n": 0.005,
        "asset_group_unit_cap": 6.0,
        "portfolio_unit_cap": 12.0,
    }
    config["costs"] = {
        "commission_multiplier": 1.0,
        "one_way_slippage": 0.0,
    }
    return config


def _single_config() -> dict[str, object]:
    config = _config()
    config["universe"] = [{"security": "ETF-A", "asset_group": "group-a"}]
    return config


def _corporate_action(
    *,
    event_type: str = "split",
    announcement_date: str = "2026-01-06",
    effective_date: str = "2026-01-07",
    status: str = "active",
    split_ratio: float | None = 2.0,
    cash_per_share: float | None = None,
) -> dict[str, object]:
    return {
        "source_event_id": "FUND_DIVIDEND:101",
        "security": "ETF-A",
        "event_type": event_type,
        "announcement_date": announcement_date,
        "record_date": "2026-01-06",
        "ex_date": effective_date,
        "effective_date": effective_date,
        "pay_date": "2026-01-09" if event_type == "cash_dividend" else None,
        "status": status,
        "knowledge_cutoff_date": "2026-01-10",
        "split_ratio": split_ratio,
        "cash_per_share": cash_per_share,
        "source": "joinquant.finance.FUND_DIVIDEND",
        "source_record_sha256": "b" * 64,
    }


def _corporate_action_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-05", periods=5, freq="D").strftime(
                "%Y-%m-%d"
            ),
            "security": "ETF-A",
            "open": [100.0, 101.0, 50.75, 51.5, 52.0],
            "high": [101.0, 103.0, 52.0, 53.0, 54.0],
            "low": [99.0, 100.0, 50.0, 51.0, 52.0],
            "close": [100.0, 102.0, 51.0, 52.0, 53.0],
            "pre_close": [100.0, 100.0, 51.0, 51.0, 52.0],
            "paused": False,
            "high_limit": [110.0, 112.0, 56.0, 57.0, 58.0],
            "low_limit": [90.0, 92.0, 46.0, 47.0, 48.0],
        }
    )


def _actions_digest(actions: list[dict[str, object]]) -> str:
    payload = json.dumps(
        actions,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_simulation_inputs_are_aligned_stable_and_read_only() -> None:
    frames = {
        "ETF-B": _frame("ETF-B", [20.0, 21.0, 22.0, 23.0, 24.0, 25.0]),
        "ETF-A": _frame("ETF-A", [9.0, 10.0, 13.0, 12.0, 14.0, 15.0]),
    }

    inputs = prepare_simulation_inputs(frames, _config())

    assert inputs.securities == ("ETF-A", "ETF-B")
    assert inputs.asset_groups == ("group-a", "group-b")
    assert inputs.dates.dtype == np.dtype("datetime64[D]")
    assert inputs.asset_group_ids.dtype == np.dtype("int64")
    assert inputs.paused.dtype == np.dtype("bool")
    assert inputs.close.dtype == np.dtype("float64")
    assert inputs.close.flags.c_contiguous
    for value in vars(inputs).values():
        if isinstance(value, np.ndarray):
            assert not value.flags.writeable
    with pytest.raises(ValueError):
        inputs.close[0, 0] = 999.0


def test_additional_execution_delay_does_not_move_signal_inputs() -> None:
    frames = {
        "ETF-B": _frame("ETF-B", [20.0, 21.0, 22.0, 23.0, 24.0, 25.0]),
        "ETF-A": _frame("ETF-A", [9.0, 10.0, 13.0, 12.0, 14.0, 15.0]),
    }
    immediate_config = _config()
    delayed_config = _config()
    delayed_config["execution"] = {"additional_delay_days": 1}

    immediate = prepare_simulation_inputs(frames, immediate_config)
    delayed = prepare_simulation_inputs(frames, delayed_config)

    assert np.array_equal(delayed.signal_source_index, immediate.signal_source_index)
    assert np.array_equal(delayed.signal_close, immediate.signal_close, equal_nan=True)
    assert np.array_equal(
        delayed.signal_entry_high, immediate.signal_entry_high, equal_nan=True
    )
    assert np.array_equal(
        delayed.signal_exit_low, immediate.signal_exit_low, equal_nan=True
    )
    assert np.array_equal(delayed.signal_n, immediate.signal_n, equal_nan=True)


def test_signal_inputs_are_shifted_to_execution_row_without_future_data() -> None:
    frames = {
        "ETF-B": _frame("ETF-B", [20.0, 21.0, 22.0, 23.0, 24.0, 25.0]),
        "ETF-A": _frame("ETF-A", [9.0, 10.0, 13.0, 12.0, 14.0, 15.0]),
    }
    original = prepare_simulation_inputs(frames, _config())
    changed_frames = {key: value.copy() for key, value in frames.items()}
    changed_frames["ETF-A"].loc[3, "close"] = 999.0
    changed_frames["ETF-A"].loc[4, "pre_close"] = 999.0
    changed = prepare_simulation_inputs(changed_frames, _config())

    execution_row = 3
    asset = original.securities.index("ETF-A")
    assert original.signal_source_index[execution_row] == 2
    assert original.signal_close[execution_row, asset] == 13.0
    assert original.signal_entry_high[execution_row, asset] == 11.0
    assert original.signal_close[execution_row, asset] == changed.signal_close[execution_row, asset]
    assert original.signal_entry_high[execution_row, asset] == changed.signal_entry_high[execution_row, asset]
    assert original.close[execution_row, asset] != changed.close[execution_row, asset]


def test_market_money_is_outside_the_turtle_order_contract() -> None:
    close_a = [10.0] * 20 + [13.0, 13.0, 13.0]
    close_b = [20.0] * len(close_a)

    def simulate(money: float | None):
        frames = {
            "ETF-A": _frame("ETF-A", close_a, money=money),
            "ETF-B": _frame("ETF-B", close_b, money=money),
        }
        return run_vectorbt_simulation(
            prepare_simulation_inputs(frames, _simulation_config()),
            _simulation_config(),
        )

    low = simulate(1.0)
    high = simulate(1_000_000_000_000.0)
    assert np.array_equal(low.action_codes, high.action_codes)
    assert np.array_equal(low.filled_quantities, high.filled_quantities)
    assert low.portfolio.orders.count() == high.portfolio.orders.count()

    missing = simulate(None)
    assert np.array_equal(low.action_codes, missing.action_codes)
    assert np.array_equal(low.filled_quantities, missing.filled_quantities)
    assert low.portfolio.orders.count() == missing.portfolio.orders.count()
    assert int(low.filled_quantities.sum()) > 0


def test_simulation_inputs_expose_only_strategy_and_risk_arrays() -> None:
    frames = {
        "ETF-B": _frame("ETF-B", [20.0, 21.0, 22.0, 23.0, 24.0, 25.0]),
        "ETF-A": _frame("ETF-A", [9.0, 10.0, 13.0, 12.0, 14.0, 15.0]),
    }

    inputs = prepare_simulation_inputs(frames, _config())

    assert tuple(vars(inputs)) == (
        "dates",
        "securities",
        "asset_groups",
        "asset_group_ids",
        "raw_open",
        "raw_high",
        "raw_low",
        "raw_close",
        "raw_pre_close",
        "continuous_open",
        "continuous_high",
        "continuous_low",
        "continuous_close",
        "continuous_pre_close",
        "continuity_factor",
        "corporate_action_applied",
        "corporate_actions_digest",
        "corporate_action_applications",
        "paused",
        "high_limit",
        "low_limit",
        "signal_source_index",
        "signal_close",
        "signal_entry_high",
        "signal_exit_low",
        "signal_n",
    )


def test_split_uses_a_forward_only_continuity_factor_and_economic_units() -> None:
    actions = [_corporate_action()]
    digest = _actions_digest(actions)
    inputs = prepare_simulation_inputs(
        {"ETF-A": _corporate_action_frame()},
        _single_config(),
        corporate_actions=actions,
        corporate_actions_digest=digest,
    )

    assert np.array_equal(inputs.raw_close[:, 0], [100.0, 102.0, 51.0, 52.0, 53.0])
    assert np.array_equal(inputs.continuity_factor[:, 0], [1.0, 1.0, 2.0, 2.0, 2.0])
    assert np.array_equal(inputs.continuous_close[:, 0], [100.0, 102.0, 102.0, 104.0, 106.0])
    assert np.array_equal(inputs.continuous_pre_close[:, 0], [100.0, 100.0, 102.0, 102.0, 104.0])
    assert np.array_equal(inputs.corporate_action_applied[:, 0], [False, False, True, False, False])
    assert inputs.corporate_actions_digest == digest
    assert len(inputs.corporate_action_applications) == 1
    application = inputs.corporate_action_applications[0]
    assert application.source_event_id == "FUND_DIVIDEND:101"
    assert application.event_type == "split"
    assert application.security == "ETF-A"
    assert application.effective_date == "2026-01-07"
    assert application.application_date == "2026-01-07"
    assert application.split_ratio == 2.0
    assert application.cash_per_share is None
    assert application.cumulative_factor == 2.0
    assert application.price_basis_changed is True
    assert application.evidence_timing == "point_in_time"
    assert inputs.execution_open is inputs.continuous_open
    assert inputs.close is inputs.continuous_close


def test_cash_dividend_is_implicit_total_return_without_pay_date_cash() -> None:
    frame = _corporate_action_frame()
    frame.loc[2:, ["open", "high", "low", "close", "pre_close", "high_limit", "low_limit"]] = [
        [9.55, 9.7, 9.4, 9.6, 9.5, 10.45, 8.55],
        [9.7, 9.9, 9.6, 9.8, 9.6, 10.56, 8.64],
        [9.9, 10.1, 9.8, 10.0, 9.8, 10.78, 8.82],
    ]
    frame.loc[0:1, ["open", "high", "low", "close", "pre_close", "high_limit", "low_limit"]] = [
        [9.8, 10.1, 9.7, 9.9, 9.9, 10.89, 8.91],
        [9.9, 10.2, 9.8, 10.0, 9.9, 10.89, 8.91],
    ]
    action = _corporate_action(
        event_type="cash_dividend",
        split_ratio=None,
        cash_per_share=0.5,
    )

    inputs = prepare_simulation_inputs(
        {"ETF-A": frame},
        _single_config(),
        corporate_actions=[action],
    )

    factor = 10.0 / 9.5
    assert inputs.continuity_factor[2, 0] == pytest.approx(factor)
    assert inputs.continuous_pre_close[2, 0] == pytest.approx(10.0)
    assert inputs.continuous_close[2, 0] == pytest.approx(9.6 * factor)
    assert inputs.corporate_action_applied[2, 0]
    assert not inputs.corporate_action_applied[4, 0]


def test_cash_dividend_factor_uses_raw_market_prices_not_cash_metadata() -> None:
    frame = _corporate_action_frame()
    frame.loc[1, "close"] = 140.847
    frame.loc[2, "pre_close"] = 140.193
    frame.loc[2:, ["open", "high", "low", "close", "high_limit", "low_limit"]] = [
        [140.2, 140.3, 140.1, 140.285, 154.212, 126.174],
        [140.3, 140.4, 140.2, 140.35, 154.385, 126.225],
        [140.4, 140.5, 140.3, 140.45, 154.495, 126.315],
    ]
    frame.loc[3, "pre_close"] = 140.285
    frame.loc[4, "pre_close"] = 140.35

    inputs = prepare_simulation_inputs(
        {"ETF-A": frame},
        _single_config(),
        corporate_actions=[
            _corporate_action(
                event_type="cash_dividend",
                split_ratio=None,
                cash_per_share=0.6542,
            )
        ],
    )

    assert inputs.continuity_factor[2, 0] == pytest.approx(140.847 / 140.193)


def test_split_on_paused_effective_date_applies_on_first_resumed_market_row() -> None:
    frame = _corporate_action_frame()
    frame.loc[2, ["open", "high", "low", "close", "pre_close", "high_limit", "low_limit"]] = [
        102.0,
        102.0,
        102.0,
        102.0,
        102.0,
        102.0,
        102.0,
    ]
    frame.loc[2, "paused"] = True
    frame.loc[3, ["open", "high", "low", "close", "pre_close", "high_limit", "low_limit"]] = [
        51.25,
        52.0,
        50.5,
        51.5,
        51.0,
        56.0,
        46.0,
    ]
    frame.loc[4, "pre_close"] = 51.5

    inputs = prepare_simulation_inputs(
        {"ETF-A": frame},
        _single_config(),
        corporate_actions=[_corporate_action()],
    )

    application = inputs.corporate_action_applications[0]
    assert application.effective_date == "2026-01-07"
    assert application.application_date == "2026-01-08"
    assert application.price_basis_changed is True
    assert inputs.continuity_factor[2, 0] == pytest.approx(1.0)
    assert inputs.continuity_factor[3, 0] == pytest.approx(2.0)


def test_active_event_without_price_basis_change_is_audited_without_factor() -> None:
    frame = _frame("ETF-A", [100.0, 101.0, 102.0, 103.0, 104.0])
    inputs = prepare_simulation_inputs(
        {"ETF-A": frame},
        _single_config(),
        corporate_actions=[_corporate_action(split_ratio=1.46301)],
    )

    application = inputs.corporate_action_applications[0]
    assert application.application_date == "2026-01-07"
    assert application.price_basis_changed is False
    assert application.cumulative_factor == pytest.approx(1.0)
    assert np.array_equal(inputs.continuity_factor[:, 0], np.ones(5))


@pytest.mark.parametrize(
    ("action", "message"),
    [
        (_corporate_action(status="cancelled"), "evidence_insufficient"),
    ],
)
def test_invalid_or_unreconciled_corporate_action_closes_the_run(
    action: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        prepare_simulation_inputs(
            {"ETF-A": _corporate_action_frame()},
            _single_config(),
            corporate_actions=[action],
        )


def test_official_split_ratio_is_audit_metadata_not_factor_input() -> None:
    inputs = prepare_simulation_inputs(
        {"ETF-A": _corporate_action_frame()},
        _single_config(),
        corporate_actions=[_corporate_action(split_ratio=3.0)],
    )

    application = inputs.corporate_action_applications[0]
    assert application.split_ratio == 3.0
    assert application.cumulative_factor == pytest.approx(2.0)


def test_late_action_metadata_is_only_retrospective_reconciliation() -> None:
    inputs = prepare_simulation_inputs(
        {"ETF-A": _corporate_action_frame()},
        _single_config(),
        corporate_actions=[_corporate_action(announcement_date="2026-01-08")],
    )

    application = inputs.corporate_action_applications[0]
    assert application.evidence_timing == "retrospective_reconciliation"
    assert inputs.continuity_factor[2, 0] == pytest.approx(2.0)


def test_unexplained_price_basis_change_closes_the_run() -> None:
    with pytest.raises(ValueError, match="evidence_insufficient"):
        prepare_simulation_inputs(
            {"ETF-A": _corporate_action_frame()},
            _single_config(),
            corporate_actions=[],
        )


def test_corporate_action_digest_mismatch_closes_the_run() -> None:
    with pytest.raises(ValueError, match="evidence_insufficient"):
        prepare_simulation_inputs(
            {"ETF-A": _corporate_action_frame()},
            _single_config(),
            corporate_actions=[_corporate_action()],
            corporate_actions_digest="0" * 64,
        )

