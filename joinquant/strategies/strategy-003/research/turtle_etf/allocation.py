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


def _is_feasible(
    candidates: Sequence[BuyRequest],
    quantities: Mapping[str, int],
    constraints: PortfolioConstraints,
) -> bool:
    intents = _allocated_intents(candidates, quantities)
    if not intents:
        return True
    decision = evaluate_risk(
        intents,
        constraints.state,
        constraints.risk_inputs,
    )
    return not decision.reason_codes and decision.approved == intents


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
        security = min(
            active,
            key=lambda item: (
                Decimal(quantities[item] + lot)
                / Decimal(by_security[item].intent.quantity),
                item,
            ),
        )
        request = by_security[security]
        next_quantity = quantities[security] + lot
        if next_quantity > request.intent.quantity:
            active.remove(security)
            continue
        proposed = dict(quantities)
        proposed[security] = next_quantity
        if not _is_feasible(ordered, proposed, constraints):
            active.remove(security)
            continue
        quantities = proposed
        if next_quantity + lot > request.intent.quantity:
            active.remove(security)

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
