from __future__ import annotations

import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path


RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))

from turtle_etf.execution import (  # noqa: E402
    DailyMarket,
    MarketQuote,
    TradingDay,
    process_day,
)
from turtle_etf.risk import (  # noqa: E402
    CovarianceEstimate,
    PortfolioState,
    RiskInputs,
)
from turtle_etf.state import (  # noqa: E402
    OrderIntent,
    apply_entry_fill,
)


def _sell(
    security: str,
    action: str,
    quantity: int,
    *,
    price: str = "10",
) -> OrderIntent:
    return OrderIntent(
        security=security,
        asset_group="group-a",
        action=action,
        quantity=quantity,
        expected_price=Decimal(price),
        signal_date="2026-01-05",
        execution_date="2026-01-06",
        reason="test_sell",
    )


def _buy(
    security: str,
    action: str = "entry",
    *,
    price: str = "10",
    signal_n: str = "1",
) -> OrderIntent:
    return OrderIntent(
        security=security,
        asset_group="group-a",
        action=action,
        quantity=100,
        expected_price=Decimal(price),
        signal_date="2026-01-05",
        execution_date="2026-01-06",
        signal_n=Decimal(signal_n),
        standard_unit=100,
        common_stop_after=Decimal(price) - Decimal("2") * Decimal(signal_n),
        reason="test_buy",
    )


def _position(security: str, quantity: int, *, signal_n: str = "1"):
    return apply_entry_fill(
        security=security,
        asset_group="group-a",
        execution_date="2026-01-02",
        fill_price=Decimal("10"),
        quantity=quantity,
        signal_n=Decimal(signal_n),
        standard_unit=100,
    )


def _risk_inputs(securities: tuple[str, ...]) -> RiskInputs:
    ordered = tuple(sorted(securities))
    covariance = CovarianceEstimate(
        securities=ordered,
        matrix=tuple(
            tuple(
                Decimal("0.000001") if left == right else Decimal("0")
                for right in range(len(ordered))
            )
            for left in range(len(ordered))
        ),
        aligned_samples=60,
        window_days=60,
    )
    return RiskInputs(
        prices={security: Decimal("10") for security in securities},
        median_turnover_20d={
            security: Decimal("1000000000") for security in securities
        },
        covariance=covariance,
        security_risk_cap=Decimal("1"),
        security_value_cap=Decimal("1"),
        asset_group_risk_cap=Decimal("1"),
        asset_group_value_cap=Decimal("1"),
        portfolio_risk_cap=Decimal("1"),
        portfolio_value_cap=Decimal("1"),
        target_volatility=Decimal("1"),
    )


def test_day_flow_executes_exit_reduction_then_same_level_buys_at_actual_open() -> None:
    exit_position = _position("EXIT", 200)
    reduce_position = _position("RED", 400)
    add_position = replace(
        _position("ADD", 100, signal_n="0.5"),
        last_add_request_date="2026-01-05",
    )
    state = PortfolioState(
        equity=Decimal("100000"),
        cash=Decimal("93000"),
        positions=(add_position, exit_position, reduce_position),
    )
    day = TradingDay(
        date="2026-01-06",
        intents=(
            _buy("NEW"),
            _buy("EXIT", action="addition"),
            _sell("RED", "mandatory_risk_reduction", 100),
            _buy("ADD", action="addition", signal_n="0.5"),
            _sell("EXIT", "full_exit", 200),
        ),
    )
    market = DailyMarket(
        quotes={
            "EXIT": MarketQuote(open=Decimal("11")),
            "RED": MarketQuote(open=Decimal("9")),
            "ADD": MarketQuote(open=Decimal("12")),
            "NEW": MarketQuote(open=Decimal("8")),
        },
        risk_inputs=_risk_inputs(("EXIT", "RED", "ADD", "NEW")),
    )

    first = process_day(day, state, market)
    second = process_day(day, state, market)

    filled_actions = [record.action for record in first.audit if record.status == "filled"]
    assert filled_actions == [
        "full_exit",
        "mandatory_risk_reduction",
        "addition",
        "entry",
    ]
    assert any(
        record.security == "EXIT"
        and record.action == "addition"
        and record.status == "cancelled"
        for record in first.audit
    )
    positions = {position.security: position for position in first.portfolio.positions}
    assert set(positions) == {"ADD", "NEW", "RED"}
    assert positions["RED"].quantity == 300
    assert positions["ADD"].quantity == 200
    assert positions["ADD"].common_stop == Decimal("11.0")
    assert positions["NEW"].common_stop == Decimal("6")
    assert first.portfolio.cash == Decimal("94100")
    assert first.audit_sha256 == second.audit_sha256


def test_paused_limits_and_missing_open_never_create_fills() -> None:
    held = _position("LOW", 100)
    state = PortfolioState(
        equity=Decimal("100000"),
        cash=Decimal("99000"),
        positions=(held,),
    )
    day = TradingDay(
        date="2026-01-06",
        intents=(
            _buy("PAUSED"),
            _buy("HIGH"),
            _buy("MISSING"),
            _sell("LOW", "full_exit", 100),
        ),
    )
    market = DailyMarket(
        quotes={
            "PAUSED": MarketQuote(open=Decimal("10"), paused=True),
            "HIGH": MarketQuote(open=Decimal("10"), high_limit=Decimal("10")),
            "MISSING": MarketQuote(open=None),
            "LOW": MarketQuote(open=Decimal("8"), low_limit=Decimal("8")),
        },
        risk_inputs=_risk_inputs(("PAUSED", "HIGH", "MISSING", "LOW")),
    )

    result = process_day(day, state, market)

    assert all(record.status == "unfilled" for record in result.audit)
    assert result.portfolio == state
    assert result.allocation.allocations == ()

