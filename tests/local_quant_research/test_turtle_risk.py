from __future__ import annotations

import sys
from dataclasses import replace
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

from turtle_etf.risk import (  # noqa: E402
    PortfolioState,
    RiskInputs,
    estimate_covariance,
    evaluate_risk,
    initial_unit,
    portfolio_volatility,
    target_volatility_reductions,
)
from turtle_etf.state import OrderIntent, apply_entry_fill  # noqa: E402


def _intent(
    security: str,
    *,
    group: str = "group-a",
    quantity: int = 100,
    price: str = "10",
    stop: str = "8",
    action: str = "entry",
) -> OrderIntent:
    return OrderIntent(
        security=security,
        asset_group=group,
        action=action,
        quantity=quantity,
        expected_price=Decimal(price),
        signal_date="2026-01-05",
        execution_date="2026-01-06",
        signal_n=Decimal("1") if action in {"entry", "addition"} else None,
        standard_unit=max(quantity, 100) if action in {"entry", "addition"} else None,
        common_stop_after=Decimal(stop) if action in {"entry", "addition"} else None,
        reason="test",
    )


def _covariance(
    securities: tuple[str, ...],
    *,
    scale: float = 0.001,
    rows: int = 60,
):
    frame = pd.DataFrame(
        {
            security: [
                (1 if index % 2 else -1) * scale * (1 + offset / 10)
                for index in range(rows)
            ]
            for offset, security in enumerate(securities)
        }
    )
    return estimate_covariance(frame, securities=securities, days=60)


def _inputs(
    securities: tuple[str, ...],
    *,
    covariance=None,
    turnover: str = "1000000000",
) -> RiskInputs:
    return RiskInputs(
        prices={security: Decimal("10") for security in securities},
        median_turnover_20d={
            security: Decimal(turnover) for security in securities
        },
        covariance=covariance or _covariance(securities),
    )


def test_initial_unit_uses_half_percent_two_n_risk_without_lot_rounding() -> None:
    assert initial_unit(Decimal("1500000"), Decimal("3")) == 1250
    with pytest.raises(ValueError):
        initial_unit(Decimal("1500000"), Decimal("0"))


def test_covariance_requires_sixty_complete_aligned_return_rows() -> None:
    returns = pd.DataFrame(
        {
            "A": [0.01, -0.01] * 30 + [0.02],
            "B": [0.02, -0.02] * 30 + [None],
        }
    )

    estimate = estimate_covariance(returns, securities=("A", "B"), days=60)
    insufficient = estimate_covariance(
        returns.iloc[:-1], securities=("A", "B"), days=61
    )

    assert estimate.aligned_samples == 60
    assert estimate.window_days == 60
    assert estimate.securities == ("A", "B")
    assert insufficient is None


def test_valid_lot_passes_all_risk_gates() -> None:
    request = _intent("A")
    state = PortfolioState(equity=Decimal("1000000"), cash=Decimal("1000000"))

    decision = evaluate_risk((request,), state, _inputs(("A",)))

    assert decision.allow_new_risk is True
    assert decision.approved == (request,)
    assert decision.rejected == ()
    assert decision.reason_codes == ()
    assert decision.projected_volatility < Decimal("0.10")


@pytest.mark.parametrize(
    ("intent", "state", "inputs", "reason"),
    [
        (
            _intent("A", quantity=50),
            PortfolioState(Decimal("1000000"), Decimal("1000000")),
            _inputs(("A",)),
            "invalid_lot",
        ),
        (
            _intent("A", quantity=100),
            PortfolioState(Decimal("1000000"), Decimal("999")),
            _inputs(("A",)),
            "insufficient_cash",
        ),
        (
            _intent("A", quantity=100),
            PortfolioState(Decimal("1000000"), Decimal("1000000")),
            _inputs(("A",), turnover="99999999"),
            "liquidity_floor",
        ),
        (
            _intent("A", quantity=100100, stop="9.99"),
            PortfolioState(Decimal("10000000"), Decimal("10000000")),
            _inputs(("A",), turnover="100000000"),
            "order_liquidity_cap",
        ),
        (
            _intent("A", quantity=30100, stop="9.99"),
            PortfolioState(Decimal("1000000"), Decimal("1000000")),
            _inputs(("A",)),
            "security_value_cap",
        ),
        (
            _intent("A", quantity=13000, stop="9"),
            PortfolioState(Decimal("1000000"), Decimal("1000000")),
            _inputs(("A",)),
            "security_risk_cap",
        ),
    ],
)
def test_individual_order_cash_lot_liquidity_and_security_gates(
    intent: OrderIntent,
    state: PortfolioState,
    inputs: RiskInputs,
    reason: str,
) -> None:
    decision = evaluate_risk((intent,), state, inputs)

    assert decision.approved == ()
    assert reason in decision.reason_codes


@pytest.mark.parametrize(
    ("requests", "state", "inputs", "reason"),
    [
        (
            (
                _intent("A", quantity=26000, stop="9.99"),
                _intent("B", quantity=26000, stop="9.99"),
            ),
            PortfolioState(Decimal("1000000"), Decimal("1000000")),
            _inputs(("A", "B")),
            "group_value_cap",
        ),
        (
            (
                _intent("A", quantity=10000, stop="8.8"),
                _intent("B", quantity=10000, stop="8.8"),
                _intent("C", quantity=1000, stop="8"),
            ),
            PortfolioState(Decimal("1000000"), Decimal("1000000")),
            _inputs(("A", "B", "C")),
            "group_risk_cap",
        ),
        (
            tuple(
                _intent(chr(65 + index), group=f"group-{index}", quantity=25100, stop="9.99")
                for index in range(4)
            ),
            PortfolioState(Decimal("1000000"), Decimal("2000000")),
            _inputs(("A", "B", "C", "D")),
            "portfolio_value_cap",
        ),
        (
            tuple(
                _intent(chr(65 + index), group=f"group-{index}", quantity=10000, stop="8.9")
                for index in range(5)
            ),
            PortfolioState(Decimal("1000000"), Decimal("1000000")),
            _inputs(("A", "B", "C", "D", "E")),
            "portfolio_risk_cap",
        ),
    ],
)
def test_asset_group_and_portfolio_hard_caps(
    requests: tuple[OrderIntent, ...],
    state: PortfolioState,
    inputs: RiskInputs,
    reason: str,
) -> None:
    decision = evaluate_risk(requests, state, inputs)

    assert reason in decision.reason_codes
    assert not decision.approved


def test_target_volatility_is_a_ten_percent_one_way_cap() -> None:
    request = _intent("A", quantity=30000, stop="9.99")
    state = PortfolioState(Decimal("1000000"), Decimal("1000000"))
    high_volatility = _covariance(("A",), scale=0.03)

    decision = evaluate_risk(
        (request,),
        state,
        _inputs(("A",), covariance=high_volatility, turnover="100000000000"),
    )

    assert decision.projected_volatility > Decimal("0.10")
    assert "target_volatility" in decision.reason_codes
    assert decision.approved == ()


def test_existing_high_volatility_positions_scale_toward_nine_point_five_percent() -> None:
    position = apply_entry_fill(
        security="A",
        asset_group="group-a",
        execution_date="2026-01-01",
        fill_price=Decimal("10"),
        quantity=30000,
        signal_n=Decimal("0.005"),
        standard_unit=30000,
    )
    state = PortfolioState(Decimal("1000000"), Decimal("700000"), (position,))
    inputs = _inputs(
        ("A",),
        covariance=_covariance(("A",), scale=0.03),
        turnover="100000000000",
    )

    before = portfolio_volatility(state, inputs)
    reductions = target_volatility_reductions(
        state,
        inputs,
        signal_date="2026-01-05",
        execution_date="2026-01-06",
    )

    assert before > Decimal("0.10")
    assert len(reductions) == 1
    assert reductions[0].action == "mandatory_risk_reduction"
    assert reductions[0].quantity % 100 == 0
    remaining_value = Decimal(position.quantity - reductions[0].quantity) * Decimal("10")
    remaining_volatility = before * remaining_value / Decimal("300000")
    assert remaining_volatility <= Decimal("0.095")


def test_cold_security_without_sixty_samples_cannot_add_risk() -> None:
    request = _intent("A")
    inputs = replace(_inputs(("A",)), covariance=None)

    decision = evaluate_risk(
        (request,),
        PortfolioState(Decimal("1000000"), Decimal("1000000")),
        inputs,
    )

    assert decision.allow_new_risk is True
    assert decision.approved == ()
    assert "covariance_unavailable" in decision.reason_codes


def test_missing_held_price_stops_new_risk_but_keeps_exit_and_reduction() -> None:
    position = apply_entry_fill(
        security="A",
        asset_group="group-a",
        execution_date="2026-01-01",
        fill_price=Decimal("10"),
        quantity=1000,
        signal_n=Decimal("1"),
        standard_unit=1000,
    )
    state = PortfolioState(
        equity=Decimal("1000000"),
        cash=Decimal("990000"),
        positions=(position,),
    )
    entry = _intent("B", group="group-b")
    full_exit = _intent("A", quantity=1000, action="full_exit")
    reduction = _intent("A", quantity=100, action="mandatory_risk_reduction")
    inputs = _inputs(("A", "B"))
    inputs = replace(inputs, prices={"A": None, "B": Decimal("10")})

    decision = evaluate_risk((entry, full_exit, reduction), state, inputs)

    assert decision.allow_new_risk is False
    assert decision.approved == (full_exit, reduction)
    assert entry in decision.rejected
    assert "held_risk_input_missing" in decision.reason_codes


def test_missing_held_covariance_stops_new_risk_without_zero_fill() -> None:
    position = apply_entry_fill(
        security="A",
        asset_group="group-a",
        execution_date="2026-01-01",
        fill_price=Decimal("10"),
        quantity=1000,
        signal_n=Decimal("1"),
        standard_unit=1000,
    )
    state = PortfolioState(Decimal("1000000"), Decimal("990000"), (position,))
    inputs = _inputs(("B",))
    inputs = replace(
        inputs,
        prices={"A": Decimal("10"), "B": Decimal("10")},
        median_turnover_20d={"A": Decimal("1000000000"), "B": Decimal("1000000000")},
    )

    decision = evaluate_risk((_intent("B", group="group-b"),), state, inputs)

    assert decision.allow_new_risk is False
    assert "held_risk_input_missing" in decision.reason_codes
