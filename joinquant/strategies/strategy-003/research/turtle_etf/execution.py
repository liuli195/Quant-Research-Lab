from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Literal, Mapping

from .allocation import (
    AllocationResult,
    BuyRequest,
    PortfolioConstraints,
    allocate_a1,
)
from .risk import PortfolioState, RiskInputs
from .state import (
    Batch,
    OrderIntent,
    TrendState,
    _date,
    _decimal,
    apply_addition_fill,
    apply_entry_fill,
    commission_fee,
)


@dataclass(frozen=True)
class MarketQuote:
    open: Decimal | None
    paused: bool = False
    high_limit: Decimal | None = None
    low_limit: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "open",
            None if self.open is None else _decimal(self.open, "open", positive=True),
        )
        object.__setattr__(
            self,
            "high_limit",
            None
            if self.high_limit is None
            else _decimal(self.high_limit, "high_limit", positive=True),
        )
        object.__setattr__(
            self,
            "low_limit",
            None
            if self.low_limit is None
            else _decimal(self.low_limit, "low_limit", positive=True),
        )
        if not isinstance(self.paused, bool):
            raise ValueError("paused must be boolean")


@dataclass(frozen=True)
class TradingDay:
    date: str
    intents: tuple[OrderIntent, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", _date(self.date, "date"))
        object.__setattr__(self, "intents", tuple(self.intents))
        if any(intent.execution_date != self.date for intent in self.intents):
            raise ValueError("all intents must execute on the trading day")


@dataclass(frozen=True)
class DailyMarket:
    quotes: Mapping[str, MarketQuote]
    risk_inputs: RiskInputs

    def __post_init__(self) -> None:
        normalized = {str(security): quote for security, quote in self.quotes.items()}
        if any(not security or not isinstance(quote, MarketQuote) for security, quote in normalized.items()):
            raise ValueError("daily market quotes are invalid")
        object.__setattr__(self, "quotes", MappingProxyType(normalized))


@dataclass(frozen=True)
class ExecutionCosts:
    commission_multiplier: Decimal = Decimal("1")
    one_way_slippage: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "commission_multiplier",
            _decimal(self.commission_multiplier, "commission_multiplier", positive=True),
        )
        object.__setattr__(
            self,
            "one_way_slippage",
            _decimal(self.one_way_slippage, "one_way_slippage"),
        )
        if self.one_way_slippage < 0 or self.one_way_slippage >= 1:
            raise ValueError("one_way_slippage must be between zero and one")


ExecutionStatus = Literal["filled", "unfilled", "cancelled"]


@dataclass(frozen=True)
class ExecutionRecord:
    sequence: int
    security: str
    action: str
    status: ExecutionStatus
    requested_quantity: int
    filled_quantity: int
    fill_price: Decimal | None
    fee: Decimal
    reason: str

    def to_document(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "security": self.security,
            "action": self.action,
            "status": self.status,
            "requested_quantity": self.requested_quantity,
            "filled_quantity": self.filled_quantity,
            "fill_price": None if self.fill_price is None else str(self.fill_price),
            "fee": str(self.fee),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DayResult:
    portfolio: PortfolioState
    audit: tuple[ExecutionRecord, ...]
    allocation: AllocationResult
    audit_sha256: str


def _sell_fill_reason(quote: MarketQuote | None) -> str | None:
    if quote is None or quote.open is None:
        return "missing_open"
    if quote.paused:
        return "paused"
    if quote.low_limit is not None and quote.open <= quote.low_limit:
        return "low_limit"
    return None


def _buy_fill_reason(quote: MarketQuote | None) -> str | None:
    if quote is None or quote.open is None:
        return "missing_open"
    if quote.paused:
        return "paused"
    if quote.high_limit is not None and quote.open >= quote.high_limit:
        return "high_limit"
    return None


def _reduce_position(position: TrendState, quantity: int) -> TrendState | None:
    remaining = min(quantity, position.quantity)
    kept_reversed: list[Batch] = []
    for batch in reversed(position.batches):
        removed = min(remaining, batch.quantity)
        kept = batch.quantity - removed
        remaining -= removed
        if kept:
            kept_reversed.append(replace(batch, quantity=kept))
    batches = tuple(reversed(kept_reversed))
    return None if not batches else replace(position, batches=batches)


def _adjust_buy_to_open(
    intent: OrderIntent,
    quote: MarketQuote,
    positions: Mapping[str, TrendState],
    costs: ExecutionCosts,
) -> OrderIntent:
    fill_price = quote.open * (Decimal("1") + costs.one_way_slippage)
    distance = intent.expected_price - intent.common_stop_after
    common_stop = fill_price - distance
    existing = positions.get(intent.security)
    if existing is not None:
        common_stop = max(existing.common_stop, common_stop)
    return replace(
        intent,
        expected_price=fill_price,
        common_stop_after=common_stop,
        estimated_fee=(
            commission_fee(fill_price, intent.quantity)
            * costs.commission_multiplier
        ),
    )


def _audit_digest(records: tuple[ExecutionRecord, ...]) -> str:
    payload = json.dumps(
        [record.to_document() for record in records],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def process_day(
    day: TradingDay,
    state: PortfolioState,
    market: DailyMarket,
    *,
    costs: ExecutionCosts = ExecutionCosts(),
) -> DayResult:
    positions = {position.security: position for position in state.positions}
    cash = state.cash
    audit: list[ExecutionRecord] = []

    def record(
        intent: OrderIntent,
        status: ExecutionStatus,
        *,
        filled_quantity: int = 0,
        fill_price: Decimal | None = None,
        fee: Decimal = Decimal("0"),
        reason: str,
    ) -> None:
        audit.append(
            ExecutionRecord(
                sequence=len(audit) + 1,
                security=intent.security,
                action=intent.action,
                status=status,
                requested_quantity=intent.quantity,
                filled_quantity=filled_quantity,
                fill_price=fill_price,
                fee=fee,
                reason=reason,
            )
        )

    exits = tuple(
        sorted(
            (intent for intent in day.intents if intent.action == "full_exit"),
            key=lambda item: item.security,
        )
    )
    exit_securities = {intent.security for intent in exits}
    for intent in exits:
        quote = market.quotes.get(intent.security)
        reason = _sell_fill_reason(quote)
        position = positions.get(intent.security)
        if reason is not None or position is None:
            record(intent, "unfilled", reason=reason or "position_missing")
            continue
        quantity = position.quantity
        fill_price = quote.open * (Decimal("1") - costs.one_way_slippage)
        fee = commission_fee(fill_price, quantity) * costs.commission_multiplier
        cash += fill_price * quantity - fee
        del positions[intent.security]
        record(
            intent,
            "filled",
            filled_quantity=quantity,
            fill_price=fill_price,
            fee=fee,
            reason="full_exit",
        )

    reductions = tuple(
        sorted(
            (
                intent
                for intent in day.intents
                if intent.action == "mandatory_risk_reduction"
            ),
            key=lambda item: item.security,
        )
    )
    for intent in reductions:
        if intent.security in exit_securities:
            record(intent, "cancelled", reason="full_exit_precedence")
            continue
        quote = market.quotes.get(intent.security)
        reason = _sell_fill_reason(quote)
        position = positions.get(intent.security)
        if reason is not None or position is None:
            record(intent, "unfilled", reason=reason or "position_missing")
            continue
        quantity = min(intent.quantity, position.quantity)
        reduced = _reduce_position(position, quantity)
        if reduced is None:
            del positions[intent.security]
        else:
            positions[intent.security] = reduced
        fill_price = quote.open * (Decimal("1") - costs.one_way_slippage)
        fee = commission_fee(fill_price, quantity) * costs.commission_multiplier
        cash += fill_price * quantity - fee
        record(
            intent,
            "filled",
            filled_quantity=quantity,
            fill_price=fill_price,
            fee=fee,
            reason="mandatory_risk_reduction",
        )

    candidate_intents: list[OrderIntent] = []
    original_buys = tuple(
        sorted(
            (
                intent
                for intent in day.intents
                if intent.action in {"entry", "addition"}
            ),
            key=lambda item: (item.security, item.action),
        )
    )
    for intent in original_buys:
        if intent.security in exit_securities:
            record(intent, "cancelled", reason="full_exit_precedence")
            continue
        quote = market.quotes.get(intent.security)
        reason = _buy_fill_reason(quote)
        if reason is not None:
            record(intent, "unfilled", reason=reason)
            continue
        candidate_intents.append(_adjust_buy_to_open(intent, quote, positions, costs))

    current_state = PortfolioState(
        equity=state.equity,
        cash=cash,
        positions=tuple(positions[security] for security in sorted(positions)),
    )
    prices = dict(market.risk_inputs.prices)
    for security in set(positions) | {intent.security for intent in candidate_intents}:
        quote = market.quotes.get(security)
        prices[security] = None if quote is None else quote.open
    risk_inputs = replace(market.risk_inputs, prices=prices)
    allocation = allocate_a1(
        tuple(
            BuyRequest(intent, costs.commission_multiplier)
            for intent in candidate_intents
        ),
        PortfolioConstraints(state=current_state, risk_inputs=risk_inputs),
    )
    allocated = {intent.security: intent for intent in allocation.allocations}
    for intent in candidate_intents:
        fill = allocated.get(intent.security)
        if fill is None:
            record(intent, "unfilled", reason="allocation_constraint")
            continue
        if fill.action == "entry":
            positions[fill.security] = apply_entry_fill(
                security=fill.security,
                asset_group=fill.asset_group,
                execution_date=day.date,
                fill_price=fill.expected_price,
                quantity=fill.quantity,
                signal_n=fill.signal_n,
                standard_unit=fill.standard_unit,
            )
        else:
            positions[fill.security] = apply_addition_fill(
                positions[fill.security],
                fill,
                execution_date=day.date,
                fill_price=fill.expected_price,
                quantity=fill.quantity,
            )
        cash -= fill.expected_price * fill.quantity + fill.estimated_fee
        record(
            fill,
            "filled",
            filled_quantity=fill.quantity,
            fill_price=fill.expected_price,
            fee=fill.estimated_fee,
            reason="a1_allocation",
        )

    portfolio = PortfolioState(
        equity=state.equity,
        cash=cash,
        positions=tuple(positions[security] for security in sorted(positions)),
    )
    records = tuple(audit)
    return DayResult(
        portfolio=portfolio,
        audit=records,
        allocation=allocation,
        audit_sha256=_audit_digest(records),
    )
