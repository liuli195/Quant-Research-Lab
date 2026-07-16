from __future__ import annotations

from types import MappingProxyType

import pandas as pd
import pytest

from scripts.research.market_data.contracts import (
    CORPORATE_ACTION_FIELDS,
    MARKET_DATA_FIELDS,
    corporate_actions_digest,
    normalized_digest,
)
from scripts.research.market_data.economic_returns import (
    EconomicReturnError,
    derive_continuous_prices,
    snapshot_return_panel,
)
from scripts.research.market_data.query import SnapshotView


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-05", periods=5, freq="D"),
            "security": "ETF-A",
            "open": [100.0, 101.0, 50.75, 51.5, 52.0],
            "high": [101.0, 103.0, 52.0, 53.0, 54.0],
            "low": [99.0, 100.0, 50.0, 51.0, 52.0],
            "close": [100.0, 102.0, 51.0, 52.0, 53.0],
            "pre_close": [100.0, 100.0, 51.0, 51.0, 52.0],
            "volume": [1_000.0] * 5,
            "money": [100_000.0] * 5,
            "factor": [1.0] * 5,
            "paused": [False] * 5,
            "high_limit": [110.0, 112.0, 56.0, 57.0, 58.0],
            "low_limit": [90.0, 92.0, 46.0, 47.0, 48.0],
        }
    )


def _split_action() -> dict[str, object]:
    return {
        "source_event_id": "FUND_DIVIDEND:101",
        "security": "ETF-A",
        "event_type": "split",
        "announcement_date": "2026-01-06",
        "record_date": "2026-01-06",
        "ex_date": "2026-01-07",
        "effective_date": "2026-01-07",
        "pay_date": None,
        "status": "active",
        "knowledge_cutoff_date": "2026-01-10",
        "split_ratio": 2.0,
        "cash_per_share": None,
        "source": "joinquant.finance.FUND_DIVIDEND",
        "source_record_sha256": "b" * 64,
    }


def test_shared_continuous_returns_reconcile_split_without_false_loss() -> None:
    result = derive_continuous_prices(
        _frame(),
        security="ETF-A",
        corporate_actions=[_split_action()],
    )

    assert result.returns.to_numpy() == pytest.approx(
        result.frame["close"] / result.frame["pre_close"] - 1.0
    )
    assert result.returns.iloc[2] == pytest.approx(0.0)
    assert result.applications[0].security == "ETF-A"


def test_shared_continuous_returns_reject_unreconciled_basis_change() -> None:
    with pytest.raises(EconomicReturnError, match="unexplained price-basis change"):
        derive_continuous_prices(
            _frame(),
            security="ETF-A",
            corporate_actions=(),
        )


def test_snapshot_return_panel_uses_snapshot_actions_and_sorted_columns() -> None:
    frame = _frame()
    rows = tuple(
        MappingProxyType(
            {
                field: (
                    value.strftime("%Y-%m-%d")
                    if field == "date"
                    else value
                )
                for field, value in row.items()
                if field in MARKET_DATA_FIELDS
            }
        )
        for row in frame.to_dict(orient="records")
    )
    actions = (MappingProxyType(_split_action()),)
    snapshot = SnapshotView(
        snapshot_id="a" * 64,
        fields=MARKET_DATA_FIELDS,
        rows=rows,
        digest=normalized_digest(rows),
        corporate_action_fields=CORPORATE_ACTION_FIELDS,
        corporate_actions=actions,
        corporate_actions_digest=corporate_actions_digest(actions),
    )

    panel = snapshot_return_panel(snapshot)

    assert panel.columns.tolist() == ["ETF-A"]
    assert panel.index.tolist() == list(pd.date_range("2026-01-05", periods=5))
    assert panel.loc[pd.Timestamp("2026-01-07"), "ETF-A"] == pytest.approx(0.0)
