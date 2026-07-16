from __future__ import annotations

import sys
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
