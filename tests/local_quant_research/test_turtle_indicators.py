from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

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

from turtle_etf.indicators import breakout_levels, true_range, turtle_n  # noqa: E402
from turtle_etf.signals import entry_signal, make_entry_intent, trend_exit_signal  # noqa: E402
from turtle_etf.state import (  # noqa: E402
    apply_addition_fill,
    apply_entry_fill,
    request_addition,
    request_full_exit,
)


def test_true_range_uses_all_three_unadjusted_price_components() -> None:
    frame = pd.DataFrame(
        {
            "high": [11.0, 12.0, 9.0],
            "low": [9.0, 11.0, 8.0],
            "pre_close": [10.0, 10.0, 10.0],
        }
    )

    result = true_range(frame)

    assert result.tolist() == [2.0, 2.0, 2.0]
    assert result.name == "tr"


def test_turtle_n_initializes_with_twenty_trs_then_uses_wilder_smoothing() -> None:
    frame = pd.DataFrame(
        {
            "high": [11.0] * 20 + [13.0],
            "low": [9.0] * 21,
            "pre_close": [10.0] * 21,
        }
    )

    result = turtle_n(frame, days=20)

    assert result.iloc[:19].isna().all()
    assert result.iloc[19] == pytest.approx(2.0)
    assert result.iloc[20] == pytest.approx(2.1)


def test_turtle_n_does_not_carry_stale_value_across_missing_tr() -> None:
    frame = pd.DataFrame(
        {
            "high": [11.0] * 20 + [None] + [11.0] * 20,
            "low": [9.0] * 41,
            "pre_close": [10.0] * 41,
        }
    )

    result = turtle_n(frame, days=20)

    assert result.iloc[19] == pytest.approx(2.0)
    assert result.iloc[20:40].isna().all()
    assert result.iloc[40] == pytest.approx(2.0)


def test_breakout_channels_exclude_the_signal_day_high_and_low() -> None:
    frame = pd.DataFrame(
        {
            "high": [float(value) for value in range(1, 61)],
            "low": [float(100 - value) for value in range(1, 61)],
        }
    )
    frame.loc[55, "high"] = 1000.0
    frame.loc[20, "low"] = -1000.0

    levels = breakout_levels(frame, entry_days=55, exit_days=20)

    assert levels.loc[55, "entry_high"] == 55.0
    assert levels.loc[20, "exit_low"] == 80.0
    assert pd.isna(levels.loc[54, "entry_high"])
    assert pd.isna(levels.loc[19, "exit_low"])


def test_close_breakout_is_strict_and_becomes_only_a_next_open_intent() -> None:
    assert entry_signal(Decimal("55"), Decimal("55")) is False
    assert entry_signal(Decimal("55.01"), Decimal("55")) is True
    assert trend_exit_signal(Decimal("20"), Decimal("20")) is False
    assert trend_exit_signal(Decimal("19.99"), Decimal("20")) is True

    intent = make_entry_intent(
        security="510300.XSHG",
        asset_group="china_sync_equity",
        signal_date="2026-01-05",
        execution_date="2026-01-06",
        expected_price=Decimal("10"),
        quantity=1200,
        signal_n=Decimal("0.5"),
        standard_unit=1250,
    )

    assert intent.action == "entry"
    assert intent.signal_date == "2026-01-05"
    assert intent.execution_date == "2026-01-06"
    assert intent.common_stop_after == Decimal("9.0")


def test_addition_uses_fixed_levels_one_request_per_day_and_only_fill_moves_state() -> None:
    state = apply_entry_fill(
        security="510300.XSHG",
        asset_group="china_sync_equity",
        execution_date="2026-01-06",
        fill_price=Decimal("10"),
        quantity=500,
        signal_n=Decimal("1"),
        standard_unit=500,
    )
    assert state.common_stop == Decimal("8")
    assert state.next_add_level == Decimal("10.5")

    requested, intent = request_addition(
        state,
        signal_date="2026-01-07",
        execution_date="2026-01-08",
        close=Decimal("12"),
        expected_price=Decimal("11.2"),
    )

    assert intent is not None
    assert intent.quantity == 500
    assert requested.batches == state.batches
    assert requested.common_stop == state.common_stop
    assert requested.next_add_level == Decimal("10.5")
    same_day_state, same_day_intent = request_addition(
        requested,
        signal_date="2026-01-07",
        execution_date="2026-01-08",
        close=Decimal("12"),
        expected_price=Decimal("11.2"),
    )
    assert same_day_state == requested
    assert same_day_intent is None

    filled = apply_addition_fill(
        requested,
        intent,
        execution_date="2026-01-08",
        fill_price=Decimal("11.2"),
        quantity=400,
    )
    assert len(filled.batches) == 2
    assert filled.common_stop == Decimal("9.2")
    assert filled.next_add_level == Decimal("11.0")

    next_requested, next_intent = request_addition(
        filled,
        signal_date="2026-01-08",
        execution_date="2026-01-09",
        close=Decimal("11.3"),
        expected_price=Decimal("10.8"),
    )
    lowered_fill = apply_addition_fill(
        next_requested,
        next_intent,
        execution_date="2026-01-09",
        fill_price=Decimal("10.8"),
        quantity=300,
    )
    assert lowered_fill.common_stop == Decimal("9.2")

    with pytest.raises(ValueError, match="execution date"):
        apply_addition_fill(
            requested,
            intent,
            execution_date="2026-01-09",
            fill_price=Decimal("11.2"),
            quantity=400,
        )


def test_unfilled_addition_remains_eligible_next_day() -> None:
    state = apply_entry_fill(
        security="510300.XSHG",
        asset_group="china_sync_equity",
        execution_date="2026-01-06",
        fill_price=Decimal("10"),
        quantity=500,
        signal_n=Decimal("1"),
        standard_unit=500,
    )
    requested, _ = request_addition(
        state,
        signal_date="2026-01-07",
        execution_date="2026-01-08",
        close=Decimal("10.6"),
        expected_price=Decimal("10.7"),
    )

    next_day_state, next_day_intent = request_addition(
        requested,
        signal_date="2026-01-08",
        execution_date="2026-01-09",
        close=Decimal("10.6"),
        expected_price=Decimal("10.7"),
    )

    assert next_day_intent is not None
    assert next_day_state.next_add_level == Decimal("10.5")


def test_protective_stop_and_twenty_day_exit_both_request_full_exit() -> None:
    state = apply_entry_fill(
        security="510300.XSHG",
        asset_group="china_sync_equity",
        execution_date="2026-01-06",
        fill_price=Decimal("10"),
        quantity=500,
        signal_n=Decimal("1"),
        standard_unit=500,
    )
    protective = request_full_exit(
        state,
        signal_date="2026-01-07",
        execution_date="2026-01-08",
        close=Decimal("8"),
        exit_level=Decimal("7"),
        expected_price=Decimal("7.9"),
    )
    trend = request_full_exit(
        state,
        signal_date="2026-01-07",
        execution_date="2026-01-08",
        close=Decimal("8.5"),
        exit_level=Decimal("9"),
        expected_price=Decimal("8.4"),
    )

    assert protective.reason == "protective_stop"
    assert trend.reason == "trend_exit"
    assert protective.action == trend.action == "full_exit"
    assert protective.quantity == trend.quantity == 500
