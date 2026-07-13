from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Sequence

from .risk import PortfolioState, RiskInputs, evaluate_risk
from .state import OrderIntent, commission_fee


@dataclass(frozen=True)
class BuyRequest:
    intent: OrderIntent

    def __post_init__(self) -> None:
        if self.intent.action not in {"entry", "addition"}:
            raise ValueError("A1 candidates must be entry or addition requests")


@dataclass(frozen=True)
class PortfolioConstraints:
    state: PortfolioState
    risk_inputs: RiskInputs


@dataclass(frozen=True)
class AllocationResult:
    allocations: tuple[OrderIntent, ...]
    rejected: tuple[BuyRequest, ...]
    quantities: Mapping[str, int]
    completion_ratios: Mapping[str, Decimal]
    remaining_cash: Decimal
    audit_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "allocations", tuple(self.allocations))
        object.__setattr__(self, "rejected", tuple(self.rejected))
        object.__setattr__(
            self,
            "quantities",
            MappingProxyType(dict(self.quantities)),
        )
        object.__setattr__(
            self,
            "completion_ratios",
            MappingProxyType(dict(self.completion_ratios)),
        )


def _allocated_intents(
    candidates: Sequence[BuyRequest],
    quantities: Mapping[str, int],
) -> tuple[OrderIntent, ...]:
    return tuple(
        replace(
            candidate.intent,
            quantity=quantities[candidate.intent.security],
            estimated_fee=commission_fee(
                candidate.intent.expected_price,
                quantities[candidate.intent.security],
            ),
        )
        for candidate in candidates
        if quantities[candidate.intent.security] > 0
    )


def _reason_codes(
    candidates: Sequence[BuyRequest],
    quantities: Mapping[str, int],
    constraints: PortfolioConstraints,
) -> tuple[str, ...]:
    intents = _allocated_intents(candidates, quantities)
    if not intents:
        return ()
    decision = evaluate_risk(
        intents,
        constraints.state,
        constraints.risk_inputs,
    )
    if decision.approved != intents and not decision.reason_codes:
        return ("allocation_not_approved",)
    return decision.reason_codes


def _is_feasible(
    candidates: Sequence[BuyRequest],
    quantities: Mapping[str, int],
    constraints: PortfolioConstraints,
) -> bool:
    return not _reason_codes(candidates, quantities, constraints)


def _hamilton_quantities(
    *,
    base: Mapping[str, int],
    active: set[str],
    extra_lots: int,
    lot: int,
    requests: Mapping[str, BuyRequest],
) -> dict[str, int]:
    quantities = dict(base)
    remaining_lots = {
        security: (requests[security].intent.quantity - quantities[security]) // lot
        for security in active
    }
    total_remaining = sum(remaining_lots.values())
    if extra_lots < 0 or extra_lots > total_remaining:
        raise ValueError("A1 extra lots exceed remaining requests")
    if not extra_lots or not total_remaining:
        return quantities

    floor_lots: dict[str, int] = {}
    remainders: dict[str, int] = {}
    for security in active:
        numerator = extra_lots * remaining_lots[security]
        floor_lots[security], remainders[security] = divmod(
            numerator,
            total_remaining,
        )
    remainder_lots = extra_lots - sum(floor_lots.values())
    for security in sorted(active, key=lambda item: (-remainders[item], item)):
        if not remainder_lots:
            break
        if floor_lots[security] < remaining_lots[security]:
            floor_lots[security] += 1
            remainder_lots -= 1
    if remainder_lots:
        raise ValueError("A1 remainder allocation did not converge")
    for security, added_lots in floor_lots.items():
        quantities[security] += added_lots * lot
    return quantities


def _maximum_hamilton_allocation(
    *,
    candidates: Sequence[BuyRequest],
    base: Mapping[str, int],
    active: set[str],
    lot: int,
    requests: Mapping[str, BuyRequest],
    constraints: PortfolioConstraints,
) -> tuple[int, dict[str, int]]:
    maximum_lots = sum(
        (requests[security].intent.quantity - base[security]) // lot
        for security in active
    )
    base_intents = _allocated_intents(candidates, base)
    base_spend = sum(
        (
            intent.expected_price * intent.quantity + intent.estimated_fee
            for intent in base_intents
        ),
        Decimal("0"),
    )
    available_cash = max(Decimal("0"), constraints.state.cash - base_spend)
    cheapest_lot = min(
        requests[security].intent.expected_price * lot for security in active
    )
    maximum_lots = min(maximum_lots, int(available_cash // cheapest_lot))
    # Portfolio volatility is not monotonic when candidates diversify each
    # other.  Search actual Hamilton portfolios from largest to smallest so a
    # feasible bundle cannot be discarded merely because its single-lot
    # members are infeasible in isolation.
    for candidate_lots in range(maximum_lots, 0, -1):
        proposed = _hamilton_quantities(
            base=base,
            active=active,
            extra_lots=candidate_lots,
            lot=lot,
            requests=requests,
        )
        if _is_feasible(candidates, proposed, constraints):
            return candidate_lots, proposed
    return 0, dict(base)


def _audit_digest(
    candidates: Sequence[BuyRequest],
    quantities: Mapping[str, int],
    ratios: Mapping[str, Decimal],
    remaining_cash: Decimal,
) -> str:
    document = {
        "allocations": [
            {
                "security": candidate.intent.security,
                "action": candidate.intent.action,
                "requested_quantity": candidate.intent.quantity,
                "allocated_quantity": quantities[candidate.intent.security],
                "completion_ratio": str(ratios[candidate.intent.security]),
            }
            for candidate in candidates
        ],
        "remaining_cash": str(remaining_cash),
    }
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def allocate_a1(
    candidates: Sequence[BuyRequest],
    constraints: PortfolioConstraints,
) -> AllocationResult:
    ordered = tuple(sorted(candidates, key=lambda item: item.intent.security))
    securities = tuple(candidate.intent.security for candidate in ordered)
    if len(securities) != len(set(securities)):
        raise ValueError("A1 candidates must be unique by security")
    lot = constraints.risk_inputs.lot_size
    quantities = {security: 0 for security in securities}
    active = set(securities)
    by_security = {candidate.intent.security: candidate for candidate in ordered}

    while active:
        active = {
            security
            for security in active
            if quantities[security] < by_security[security].intent.quantity
        }
        if not active:
            break
        _, proposed = _maximum_hamilton_allocation(
            candidates=ordered,
            base=quantities,
            active=active,
            lot=lot,
            requests=by_security,
            constraints=constraints,
        )
        quantities = proposed
        remaining_lots = sum(
            (by_security[security].intent.quantity - quantities[security]) // lot
            for security in active
        )
        if not remaining_lots:
            break
        blocked: set[str] = set()
        for security in active:
            next_quantities = dict(quantities)
            next_quantities[security] += lot
            reasons = set(_reason_codes(ordered, next_quantities, constraints))
            if reasons - {"target_volatility"}:
                blocked.add(security)
        if blocked:
            active.difference_update(blocked)
            continue
        break

    allocations = _allocated_intents(ordered, quantities)
    ratios = {
        security: Decimal(quantities[security])
        / Decimal(by_security[security].intent.quantity)
        for security in securities
    }
    spent = sum(
        (
            intent.expected_price * intent.quantity + intent.estimated_fee
            for intent in allocations
        ),
        Decimal("0"),
    )
    remaining_cash = constraints.state.cash - spent
    rejected = tuple(
        candidate
        for candidate in ordered
        if quantities[candidate.intent.security] == 0
    )
    return AllocationResult(
        allocations=allocations,
        rejected=rejected,
        quantities=quantities,
        completion_ratios=ratios,
        remaining_cash=remaining_cash,
        audit_sha256=_audit_digest(ordered, quantities, ratios, remaining_cash),
    )
