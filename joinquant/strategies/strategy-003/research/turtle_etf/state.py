from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Literal


OrderAction = Literal[
    "entry",
    "addition",
    "full_exit",
    "mandatory_risk_reduction",
]


def commission_fee(price: Decimal, quantity: int) -> Decimal:
    normalized_price = _decimal(price, "commission_price", positive=True)
    if not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("commission quantity must be positive")
    return max(
        Decimal("5"),
        normalized_price * quantity * Decimal("0.000085"),
    )


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{field} must be finite and positive")
    return result


def _date(value: str, field: str) -> str:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD") from exc
    return value


@dataclass(frozen=True)
class Batch:
    execution_date: str
    quantity: int
    fill_price: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_date", _date(self.execution_date, "execution_date"))
        if not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("batch quantity must be positive")
        object.__setattr__(self, "fill_price", _decimal(self.fill_price, "fill_price", positive=True))


@dataclass(frozen=True)
class OrderIntent:
    security: str
    asset_group: str
    action: OrderAction
    quantity: int
    expected_price: Decimal
    signal_date: str
    execution_date: str
    signal_n: Decimal | None = None
    standard_unit: int | None = None
    common_stop_after: Decimal | None = None
    estimated_fee: Decimal = Decimal("0")
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.security or not self.asset_group:
            raise ValueError("security and asset_group must be non-empty")
        if self.action not in {
            "entry",
            "addition",
            "full_exit",
            "mandatory_risk_reduction",
        }:
            raise ValueError("unsupported order action")
        if not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("order quantity must be positive")
        object.__setattr__(
            self,
            "expected_price",
            _decimal(self.expected_price, "expected_price", positive=True),
        )
        object.__setattr__(self, "signal_date", _date(self.signal_date, "signal_date"))
        object.__setattr__(
            self,
            "execution_date",
            _date(self.execution_date, "execution_date"),
        )
        if self.execution_date <= self.signal_date:
            raise ValueError("execution_date must be after signal_date")
        object.__setattr__(
            self,
            "estimated_fee",
            _decimal(self.estimated_fee, "estimated_fee"),
        )
        if self.estimated_fee < 0:
            raise ValueError("estimated_fee must not be negative")
        if self.action in {"entry", "addition"}:
            if self.signal_n is None or self.common_stop_after is None:
                raise ValueError("buy intent requires signal_n and common_stop_after")
            if not isinstance(self.standard_unit, int) or self.standard_unit <= 0:
                raise ValueError("buy intent requires a positive standard_unit")
            object.__setattr__(self, "signal_n", _decimal(self.signal_n, "signal_n", positive=True))
            object.__setattr__(
                self,
                "common_stop_after",
                _decimal(self.common_stop_after, "common_stop_after"),
            )


@dataclass(frozen=True)
class TrendState:
    security: str
    asset_group: str
    signal_n: Decimal
    standard_unit: int
    initial_fill_price: Decimal
    batches: tuple[Batch, ...]
    common_stop: Decimal
    next_add_index: int = 1
    add_step_n: Decimal = Decimal("0.5")
    stop_n: Decimal = Decimal("2")
    last_add_request_date: str | None = None

    def __post_init__(self) -> None:
        if not self.security or not self.asset_group or not self.batches:
            raise ValueError("trend state identity and batches are required")
        object.__setattr__(self, "signal_n", _decimal(self.signal_n, "signal_n", positive=True))
        object.__setattr__(
            self,
            "initial_fill_price",
            _decimal(self.initial_fill_price, "initial_fill_price", positive=True),
        )
        object.__setattr__(self, "common_stop", _decimal(self.common_stop, "common_stop"))
        object.__setattr__(self, "add_step_n", _decimal(self.add_step_n, "add_step_n", positive=True))
        object.__setattr__(self, "stop_n", _decimal(self.stop_n, "stop_n", positive=True))
        object.__setattr__(self, "batches", tuple(self.batches))
        if not isinstance(self.standard_unit, int) or self.standard_unit <= 0:
            raise ValueError("standard_unit must be positive")
        if not isinstance(self.next_add_index, int) or self.next_add_index < 1:
            raise ValueError("next_add_index must be positive")
        if self.last_add_request_date is not None:
            object.__setattr__(
                self,
                "last_add_request_date",
                _date(self.last_add_request_date, "last_add_request_date"),
            )

    @property
    def quantity(self) -> int:
        return sum(batch.quantity for batch in self.batches)

    @property
    def next_add_level(self) -> Decimal:
        return self.initial_fill_price + (
            Decimal(self.next_add_index) * self.add_step_n * self.signal_n
        )

    @property
    def planned_loss(self) -> Decimal:
        net_loss = sum(
            (batch.fill_price - self.common_stop) * batch.quantity
            for batch in self.batches
        )
        return max(Decimal("0"), net_loss)


def apply_entry_fill(
    *,
    security: str,
    asset_group: str,
    execution_date: str,
    fill_price: Decimal,
    quantity: int,
    signal_n: Decimal,
    standard_unit: int,
    add_step_n: Decimal = Decimal("0.5"),
    stop_n: Decimal = Decimal("2"),
) -> TrendState:
    price = _decimal(fill_price, "fill_price", positive=True)
    n_value = _decimal(signal_n, "signal_n", positive=True)
    stop_multiple = _decimal(stop_n, "stop_n", positive=True)
    batch = Batch(execution_date=execution_date, quantity=quantity, fill_price=price)
    return TrendState(
        security=security,
        asset_group=asset_group,
        signal_n=n_value,
        standard_unit=standard_unit,
        initial_fill_price=price,
        batches=(batch,),
        common_stop=price - stop_multiple * n_value,
        add_step_n=add_step_n,
        stop_n=stop_multiple,
    )


def request_addition(
    state: TrendState,
    *,
    signal_date: str,
    execution_date: str,
    close: Decimal,
    expected_price: Decimal,
) -> tuple[TrendState, OrderIntent | None]:
    signal_date = _date(signal_date, "signal_date")
    close_value = _decimal(close, "close", positive=True)
    expected = _decimal(expected_price, "expected_price", positive=True)
    if state.last_add_request_date == signal_date or close_value < state.next_add_level:
        return state, None
    candidate_stop = max(
        state.common_stop,
        expected - state.stop_n * state.signal_n,
    )
    requested_state = replace(state, last_add_request_date=signal_date)
    return requested_state, OrderIntent(
        security=state.security,
        asset_group=state.asset_group,
        action="addition",
        quantity=state.standard_unit,
        expected_price=expected,
        signal_date=signal_date,
        execution_date=execution_date,
        signal_n=state.signal_n,
        standard_unit=state.standard_unit,
        common_stop_after=candidate_stop,
        reason="fixed_addition_level",
    )


def apply_addition_fill(
    state: TrendState,
    intent: OrderIntent,
    *,
    execution_date: str,
    fill_price: Decimal,
    quantity: int,
) -> TrendState:
    if intent is None or intent.action != "addition" or intent.security != state.security:
        raise ValueError("fill does not match an addition intent")
    if execution_date != intent.execution_date:
        raise ValueError("fill execution date does not match the addition intent")
    if (
        intent.asset_group != state.asset_group
        or intent.signal_n != state.signal_n
        or intent.standard_unit != state.standard_unit
        or state.last_add_request_date != intent.signal_date
    ):
        raise ValueError("fill identity does not match the addition state")
    if not isinstance(quantity, int) or quantity <= 0 or quantity > intent.quantity:
        raise ValueError("filled quantity exceeds the addition intent")
    price = _decimal(fill_price, "fill_price", positive=True)
    new_stop = max(state.common_stop, price - state.stop_n * state.signal_n)
    batch = Batch(execution_date=execution_date, quantity=quantity, fill_price=price)
    return replace(
        state,
        batches=(*state.batches, batch),
        common_stop=new_stop,
        next_add_index=state.next_add_index + 1,
    )


def request_full_exit(
    state: TrendState,
    *,
    signal_date: str,
    execution_date: str,
    close: Decimal,
    exit_level: Decimal | None,
    expected_price: Decimal,
) -> OrderIntent | None:
    close_value = _decimal(close, "close", positive=True)
    level = None if exit_level is None else _decimal(exit_level, "exit_level")
    if close_value <= state.common_stop:
        reason = "protective_stop"
    elif level is not None and close_value < level:
        reason = "trend_exit"
    else:
        return None
    return OrderIntent(
        security=state.security,
        asset_group=state.asset_group,
        action="full_exit",
        quantity=state.quantity,
        expected_price=expected_price,
        signal_date=signal_date,
        execution_date=execution_date,
        signal_n=state.signal_n,
        reason=reason,
    )
