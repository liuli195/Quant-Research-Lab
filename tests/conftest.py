from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = REPO_ROOT / ".agents" / "skills" / "joinquant-archive-sync" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def analysis_rows() -> dict[str, list[dict[str, object]]]:
    dates = ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08")
    equities = (100.0, 110.0, 99.0, 121.0)
    returns = (0.0, 0.1, -0.1, 22.0 / 99.0)
    invested = (0.5, 0.6, 0.4, 0.7)
    rows: dict[str, list[dict[str, object]]] = {
        "equity": [],
        "returns": [],
        "trades": [
            {
                "trade_id": "trade-1",
                "entry_date": dates[0],
                "exit_date": dates[1],
                "security": "ETF-A",
                "asset_group": "equity",
                "quantity": 5.0,
                "entry_price": 10.0,
                "exit_price": 12.0,
                "fees": 0.0,
                "pnl": 10.0,
                "return": 0.2,
                "entry_reason": "breakout",
                "exit_reason": "trend_exit",
            },
            {
                "trade_id": "trade-2",
                "entry_date": dates[1],
                "exit_date": dates[2],
                "security": "ETF-B",
                "asset_group": "bond",
                "quantity": 5.0,
                "entry_price": 10.0,
                "exit_price": 9.0,
                "fees": 0.0,
                "pnl": -5.0,
                "return": -0.1,
                "entry_reason": "breakout",
                "exit_reason": "protective_stop",
            },
        ],
        "orders": [
            {
                "order_id": "order-1",
                "date": dates[0],
                "security": "ETF-A",
                "side": "buy",
                "requested_quantity": 5.0,
                "filled_quantity": 5.0,
                "fill_price": 10.0,
                "fee": 1.0,
                "status": "filled",
                "reason": "breakout",
            },
            {
                "order_id": "order-2",
                "date": dates[1],
                "security": "ETF-A",
                "side": "sell",
                "requested_quantity": 5.0,
                "filled_quantity": 5.0,
                "fill_price": 12.0,
                "fee": 2.0,
                "status": "filled",
                "reason": "trend_exit",
            },
        ],
        "positions": [],
        "risk": [],
        "events": [
            {
                "event_id": "event-1",
                "date": dates[0],
                "sequence": 1,
                "security": "ETF-A",
                "event_type": "order",
                "status": "filled",
                "reason": "breakout",
            }
        ],
        "benchmarks": [],
    }
    benchmark_returns = {
        "csi300_total_return_cny": (0.0, 0.05, -0.02, 0.03),
        "nasdaq100_total_return_cny": (0.0, 0.02, -0.01, 0.04),
    }
    benchmark_indices = {benchmark: 100.0 for benchmark in benchmark_returns}
    previous_equity = equities[0]
    for index, current_date in enumerate(dates):
        equity = equities[index]
        cash = equity * (1.0 - invested[index])
        positions_value = equity - cash
        daily_pnl = 0.0 if index == 0 else equity - previous_equity
        rows["equity"].append(
            {
                "date": current_date,
                "portfolio_id": "strategy",
                "currency": "CNY",
                "equity": equity,
                "cash": cash,
                "positions_value": positions_value,
                "daily_pnl": daily_pnl,
                "fees": 0.0,
            }
        )
        rows["returns"].append(
            {
                "date": current_date,
                "portfolio_id": "strategy",
                "return": returns[index],
                "equity": equity,
                "cash_return_contribution": 0.0,
            }
        )
        rows["positions"].append(
            {
                "date": current_date,
                "security": "ETF-A",
                "asset_group": "equity",
                "quantity": 5.0,
                "close": positions_value / 5.0,
                "market_value": positions_value,
                "weight": invested[index],
                "planned_loss": 1.0,
                "common_stop": positions_value / 5.0 - 2.0,
                "signal_n": 1.0,
                "stop_failure_loss": 20.0,
                "attribution_reason": "holding",
                "pnl_contribution": daily_pnl,
                "return_contribution": returns[index],
            }
        )
        rows["risk"].append(
            {
                "date": current_date,
                "portfolio_id": "strategy",
                "equity": equity,
                "cash": cash,
                "invested_ratio": invested[index],
                "cash_ratio": 1.0 - invested[index],
                "planned_risk": 1.0,
                "portfolio_risk_usage": 0.2,
                "portfolio_volatility": 0.1,
                "target_volatility_usage": 0.5,
            }
        )
        for benchmark_id, values in benchmark_returns.items():
            benchmark_return = values[index]
            if index:
                benchmark_indices[benchmark_id] *= 1.0 + benchmark_return
            rows["benchmarks"].append(
                {
                    "date": current_date,
                    "benchmark_id": benchmark_id,
                    "currency": "CNY",
                    "total_return_index": benchmark_indices[benchmark_id],
                    "return": benchmark_return,
                    "source_id": f"fixture:{benchmark_id}",
                }
            )
        previous_equity = equity
    return rows
