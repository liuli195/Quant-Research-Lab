from __future__ import annotations

import math

import pandas as pd


def _numeric_columns(frame: pd.DataFrame, fields: tuple[str, ...]) -> dict[str, pd.Series]:
    missing = [field for field in fields if field not in frame.columns]
    if missing:
        raise ValueError(f"missing price fields: {', '.join(missing)}")
    return {
        field: pd.to_numeric(frame[field], errors="coerce").astype(float)
        for field in fields
    }


def true_range(frame: pd.DataFrame) -> pd.Series:
    columns = _numeric_columns(frame, ("high", "low", "pre_close"))
    components = pd.concat(
        (
            columns["high"] - columns["low"],
            (columns["high"] - columns["pre_close"]).abs(),
            (columns["low"] - columns["pre_close"]).abs(),
        ),
        axis=1,
    )
    result = components.max(axis=1, skipna=False)
    result.name = "tr"
    return result


def turtle_n(frame: pd.DataFrame, days: int = 20) -> pd.Series:
    if not isinstance(days, int) or days < 1:
        raise ValueError("days must be a positive integer")
    tr = true_range(frame)
    values: list[float] = []
    warmup: list[float] = []
    current: float | None = None
    for raw_value in tr:
        value = float(raw_value) if pd.notna(raw_value) else math.nan
        if not math.isfinite(value):
            warmup.clear()
            current = None
            values.append(math.nan)
            continue
        if current is None:
            warmup.append(value)
            if len(warmup) < days:
                values.append(math.nan)
                continue
            current = sum(warmup[-days:]) / days
        else:
            current = ((current * (days - 1)) + value) / days
        values.append(current)
    return pd.Series(values, index=frame.index, name="n", dtype=float)


def breakout_levels(
    frame: pd.DataFrame,
    entry_days: int,
    exit_days: int,
) -> pd.DataFrame:
    if not isinstance(entry_days, int) or entry_days < 1:
        raise ValueError("entry_days must be a positive integer")
    if not isinstance(exit_days, int) or exit_days < 1:
        raise ValueError("exit_days must be a positive integer")
    columns = _numeric_columns(frame, ("high", "low"))
    return pd.DataFrame(
        {
            "entry_high": columns["high"]
            .shift(1)
            .rolling(entry_days, min_periods=entry_days)
            .max(),
            "exit_low": columns["low"]
            .shift(1)
            .rolling(exit_days, min_periods=exit_days)
            .min(),
        },
        index=frame.index,
    )
