from __future__ import annotations

import itertools
import sys
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

from turtle_etf.allocation import (  # noqa: E402
    BuyRequest,
    PortfolioConstraints,
    allocate_a1,
)
from turtle_etf.risk import (  # noqa: E402
    CovarianceEstimate,
    PortfolioState,
    RiskInputs,
    evaluate_risk,
)
from turtle_etf.state import OrderIntent  # noqa: E402


def _intent(
    security: str,
    *,
    quantity: int,
    group: str = "group-a",
    price: str = "10",
) -> OrderIntent:
    return OrderIntent(
        security=security,
        asset_group=group,
        action="entry",
        quantity=quantity,
        expected_price=Decimal(price),
        signal_date="2026-01-05",
        execution_date="2026-01-06",
        signal_n=Decimal("0.05"),
        standard_unit=quantity,
        common_stop_after=Decimal(price) - Decimal("0.10"),
        reason="entry_breakout",
    )


def _inputs(
    securities: tuple[str, ...],
    *,
    group_value_cap: str = "0.50",
) -> RiskInputs:
    covariance = CovarianceEstimate(
        securities=tuple(sorted(securities)),
        matrix=tuple(
            tuple(
                Decimal("0.000001") if left == right else Decimal("0")
                for right in range(len(securities))
            )
            for left in range(len(securities))
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
        asset_group_value_cap=Decimal(group_value_cap),
        portfolio_risk_cap=Decimal("1"),
        portfolio_value_cap=Decimal("1"),
        target_volatility=Decimal("1"),
    )


def test_a1_uses_common_completion_then_fractional_lot_remainder() -> None:
    requests = (
        BuyRequest(_intent("B", quantity=600)),
        BuyRequest(_intent("A", quantity=1000)),
    )
    constraints = PortfolioConstraints(
        state=PortfolioState(Decimal("100000"), Decimal("10000")),
        risk_inputs=_inputs(("A", "B")),
    )

    result = allocate_a1(requests, constraints)

    assert dict(result.quantities) == {"A": 600, "B": 400}
    assert result.completion_ratios["A"] == Decimal("0.6")
    assert result.completion_ratios["B"] == Decimal("0.6666666666666666666666666667")
    decision = evaluate_risk(result.allocations, constraints.state, constraints.risk_inputs)
    assert decision.reason_codes == ()
    assert decision.approved == result.allocations


def test_a1_releases_group_limited_budget_to_other_candidates() -> None:
    requests = tuple(
        BuyRequest(_intent(security, quantity=1000, group=group))
        for security, group in (("A", "shared"), ("B", "shared"), ("C", "other"))
    )
    constraints = PortfolioConstraints(
        state=PortfolioState(Decimal("100000"), Decimal("30000")),
        risk_inputs=_inputs(("A", "B", "C"), group_value_cap="0.10"),
    )

    expected = {"A": 500, "B": 500, "C": 1000}
    digests: set[str] = set()
    for permutation in itertools.permutations(requests):
        result = allocate_a1(permutation, constraints)
        assert dict(result.quantities) == expected
        assert result.remaining_cash == Decimal("10000")
        digests.add(result.audit_sha256)

    assert len(digests) == 1


def test_a1_exact_remainder_tie_uses_security_code() -> None:
    requests = (
        BuyRequest(_intent("B", quantity=1000)),
        BuyRequest(_intent("A", quantity=1000)),
    )
    constraints = PortfolioConstraints(
        state=PortfolioState(Decimal("100000"), Decimal("1000")),
        risk_inputs=_inputs(("A", "B")),
    )

    result = allocate_a1(requests, constraints)

    assert dict(result.quantities) == {"A": 100, "B": 0}
    assert result.allocations[0].security == "A"


def test_a1_infeasible_candidate_does_not_block_other_budget() -> None:
    requests = (
        BuyRequest(_intent("A", quantity=1000)),
        BuyRequest(_intent("B", quantity=1000)),
    )
    inputs = _inputs(("A", "B"))
    inputs = RiskInputs(
        **{
            **inputs.__dict__,
            "median_turnover_20d": {
                "A": Decimal("1"),
                "B": Decimal("1000000000"),
            },
        }
    )
    constraints = PortfolioConstraints(
        state=PortfolioState(Decimal("100000"), Decimal("10000")),
        risk_inputs=inputs,
    )

    result = allocate_a1(requests, constraints)

    assert dict(result.quantities) == {"A": 0, "B": 1000}
    assert result.rejected == (requests[0],)

