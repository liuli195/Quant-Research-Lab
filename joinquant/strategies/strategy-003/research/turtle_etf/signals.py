from __future__ import annotations

from decimal import Decimal

from .state import OrderIntent, _decimal


def entry_signal(close: Decimal, entry_level: Decimal | None) -> bool:
    if entry_level is None:
        return False
    return _decimal(close, "close", positive=True) > _decimal(
        entry_level, "entry_level", positive=True
    )


def trend_exit_signal(close: Decimal, exit_level: Decimal | None) -> bool:
    if exit_level is None:
        return False
    return _decimal(close, "close", positive=True) < _decimal(
        exit_level, "exit_level", positive=True
    )


def make_entry_intent(
    *,
    security: str,
    asset_group: str,
    signal_date: str,
    execution_date: str,
    expected_price: Decimal,
    quantity: int,
    signal_n: Decimal,
    standard_unit: int,
    stop_n: Decimal = Decimal("2"),
) -> OrderIntent:
    price = _decimal(expected_price, "expected_price", positive=True)
    n_value = _decimal(signal_n, "signal_n", positive=True)
    stop_multiple = _decimal(stop_n, "stop_n", positive=True)
    return OrderIntent(
        security=security,
        asset_group=asset_group,
        action="entry",
        quantity=quantity,
        expected_price=price,
        signal_date=signal_date,
        execution_date=execution_date,
        signal_n=n_value,
        standard_unit=standard_unit,
        common_stop_after=price - stop_multiple * n_value,
        reason="entry_breakout",
    )
