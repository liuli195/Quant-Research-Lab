from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from types import MappingProxyType
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from .state import Batch, OrderIntent, TrendState, _decimal


@dataclass(frozen=True)
class CovarianceEstimate:
    securities: tuple[str, ...]
    matrix: tuple[tuple[Decimal, ...], ...]
    aligned_samples: int
    window_days: int

    def __post_init__(self) -> None:
        size = len(self.securities)
        if size == 0 or len(set(self.securities)) != size:
            raise ValueError("covariance securities must be non-empty and unique")
        if len(self.matrix) != size or any(len(row) != size for row in self.matrix):
            raise ValueError("covariance matrix dimensions are invalid")
        if self.aligned_samples < self.window_days or self.window_days < 2:
            raise ValueError("covariance sample evidence is invalid")

    def covers(self, securities: Sequence[str]) -> bool:
        return set(securities).issubset(self.securities)


@dataclass(frozen=True)
class PortfolioState:
    equity: Decimal
    cash: Decimal
    positions: tuple[TrendState, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "equity", _decimal(self.equity, "equity", positive=True))
        object.__setattr__(self, "cash", _decimal(self.cash, "cash"))
        object.__setattr__(self, "positions", tuple(self.positions))
        if self.cash < 0:
            raise ValueError("cash must not be negative")
        securities = [position.security for position in self.positions]
        if len(securities) != len(set(securities)):
            raise ValueError("portfolio positions must be unique by security")


@dataclass(frozen=True)
class RiskInputs:
    prices: Mapping[str, Decimal | None]
    median_turnover_20d: Mapping[str, Decimal]
    covariance: CovarianceEstimate | None
    lot_size: int = 100
    minimum_aligned_samples: int = 60
    minimum_turnover: Decimal = Decimal("100000000")
    maximum_order_turnover_fraction: Decimal = Decimal("0.01")
    security_risk_cap: Decimal = Decimal("0.0125")
    security_value_cap: Decimal = Decimal("0.30")
    asset_group_risk_cap: Decimal = Decimal("0.025")
    asset_group_value_cap: Decimal = Decimal("0.50")
    portfolio_risk_cap: Decimal = Decimal("0.05")
    portfolio_value_cap: Decimal = Decimal("1.00")
    target_volatility: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        prices = {
            str(security): None
            if value is None
            else _decimal(value, "price", positive=True)
            for security, value in self.prices.items()
        }
        turnover = {
            str(security): _decimal(value, "median_turnover_20d")
            for security, value in self.median_turnover_20d.items()
        }
        object.__setattr__(self, "prices", MappingProxyType(prices))
        object.__setattr__(self, "median_turnover_20d", MappingProxyType(turnover))
        if not isinstance(self.lot_size, int) or self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if not isinstance(self.minimum_aligned_samples, int) or self.minimum_aligned_samples < 2:
            raise ValueError("minimum_aligned_samples must be at least 2")
        for field in (
            "minimum_turnover",
            "maximum_order_turnover_fraction",
            "security_risk_cap",
            "security_value_cap",
            "asset_group_risk_cap",
            "asset_group_value_cap",
            "portfolio_risk_cap",
            "portfolio_value_cap",
            "target_volatility",
        ):
            object.__setattr__(self, field, _decimal(getattr(self, field), field, positive=True))


@dataclass(frozen=True)
class RiskDecision:
    allow_new_risk: bool
    approved: tuple[OrderIntent, ...]
    rejected: tuple[OrderIntent, ...]
    reason_codes: tuple[str, ...]
    projected_volatility: Decimal | None


def initial_unit(
    equity: Decimal,
    n_value: Decimal,
    risk_fraction: Decimal = Decimal("0.005"),
) -> int:
    equity_value = _decimal(equity, "equity", positive=True)
    n = _decimal(n_value, "n_value", positive=True)
    fraction = _decimal(risk_fraction, "risk_fraction", positive=True)
    return int(
        (equity_value * fraction / (Decimal("2") * n)).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )


def estimate_covariance(
    returns: pd.DataFrame,
    *,
    securities: Sequence[str],
    days: int = 60,
    method: str = "sample",
    half_life_days: int | None = None,
) -> CovarianceEstimate | None:
    securities = tuple(securities)
    if not isinstance(days, int) or days < 2:
        raise ValueError("days must be at least 2")
    if not securities or len(securities) != len(set(securities)):
        raise ValueError("securities must be non-empty and unique")
    if any(security not in returns.columns for security in securities):
        return None
    numeric = returns.loc[:, list(securities)].apply(pd.to_numeric, errors="coerce")
    aligned = numeric.replace([np.inf, -np.inf], np.nan).dropna(how="any")
    if len(aligned) < days:
        return None
    window = aligned.tail(days)
    if method == "sample":
        covariance = window.cov()
    elif method == "ewma":
        if not isinstance(half_life_days, int) or half_life_days <= 0:
            raise ValueError("ewma covariance requires a positive half_life_days")
        decay = math.exp(math.log(0.5) / half_life_days)
        weights = np.power(decay, np.arange(len(window) - 1, -1, -1))
        weights = weights / weights.sum()
        values = window.to_numpy(dtype=float)
        centered = values - np.average(values, axis=0, weights=weights)
        denominator = 1.0 - float(np.sum(weights**2))
        matrix_values = (centered * weights[:, None]).T @ centered / denominator
        covariance = pd.DataFrame(
            matrix_values,
            index=window.columns,
            columns=window.columns,
        )
    else:
        raise ValueError("unsupported covariance method")
    matrix_values = covariance.to_numpy(dtype=float)
    if not np.isfinite(matrix_values).all():
        return None
    matrix = tuple(
        tuple(Decimal(str(value)) for value in row) for row in matrix_values
    )
    return CovarianceEstimate(
        securities=securities,
        matrix=matrix,
        aligned_samples=len(window),
        window_days=days,
    )


def _annualized_volatility(
    values: Mapping[str, Decimal],
    *,
    equity: Decimal,
    covariance: CovarianceEstimate,
) -> Decimal:
    weights = {
        security: float(value / equity) for security, value in values.items()
    }
    positions = {security: index for index, security in enumerate(covariance.securities)}
    variance = 0.0
    for left, left_weight in weights.items():
        for right, right_weight in weights.items():
            variance += (
                left_weight
                * float(covariance.matrix[positions[left]][positions[right]])
                * right_weight
            )
    if variance < -1e-15:
        raise ValueError("projected covariance variance is negative")
    annualized = math.sqrt(max(0.0, variance)) * math.sqrt(252.0)
    return Decimal(str(annualized))


def portfolio_volatility(
    state: PortfolioState,
    inputs: RiskInputs,
) -> Decimal | None:
    if not state.positions:
        return Decimal("0")
    securities = tuple(position.security for position in state.positions)
    covariance = inputs.covariance
    if (
        covariance is None
        or covariance.aligned_samples < inputs.minimum_aligned_samples
        or not covariance.covers(securities)
    ):
        return None
    values: dict[str, Decimal] = {}
    for position in state.positions:
        price = inputs.prices.get(position.security)
        if price is None:
            return None
        values[position.security] = price * position.quantity
    return _annualized_volatility(
        values,
        equity=state.equity,
        covariance=covariance,
    )


def target_volatility_reductions(
    state: PortfolioState,
    inputs: RiskInputs,
    *,
    signal_date: str,
    execution_date: str,
    reduction_target: Decimal = Decimal("0.095"),
) -> tuple[OrderIntent, ...]:
    target = _decimal(reduction_target, "reduction_target", positive=True)
    if target >= inputs.target_volatility:
        raise ValueError("reduction_target must be below target_volatility")
    current = portfolio_volatility(state, inputs)
    if current is None or current <= inputs.target_volatility:
        return ()
    scale = target / current
    reductions: list[OrderIntent] = []
    lot = Decimal(inputs.lot_size)
    for position in sorted(state.positions, key=lambda item: item.security):
        target_quantity = int(
            (Decimal(position.quantity) * scale / lot).to_integral_value(
                rounding=ROUND_FLOOR
            )
            * inputs.lot_size
        )
        reduction = position.quantity - target_quantity
        if reduction <= 0:
            continue
        price = inputs.prices.get(position.security)
        if price is None:
            return ()
        reductions.append(
            OrderIntent(
                security=position.security,
                asset_group=position.asset_group,
                action="mandatory_risk_reduction",
                quantity=reduction,
                expected_price=price,
                signal_date=signal_date,
                execution_date=execution_date,
                reason="target_volatility_reduction",
            )
        )
    return tuple(reductions)


def _position_projection(
    state: PortfolioState,
    exits: Sequence[OrderIntent],
) -> tuple[dict[str, dict[str, object]], Decimal]:
    full_exits = {intent.security for intent in exits if intent.action == "full_exit"}
    projected: dict[str, dict[str, object]] = {}
    cash = state.cash
    for position in state.positions:
        if position.security in full_exits:
            exit_intent = next(intent for intent in exits if intent.security == position.security)
            cash += exit_intent.expected_price * min(position.quantity, exit_intent.quantity)
            cash -= exit_intent.estimated_fee
            continue
        projected[position.security] = {
            "asset_group": position.asset_group,
            "batches": list(position.batches),
            "common_stop": position.common_stop,
        }
    return projected, cash


def evaluate_risk(
    requests: Sequence[OrderIntent],
    state: PortfolioState,
    inputs: RiskInputs,
) -> RiskDecision:
    requests = tuple(requests)
    exits = tuple(
        intent
        for intent in requests
        if intent.action in {"full_exit", "mandatory_risk_reduction"}
    )
    buys = tuple(
        intent for intent in requests if intent.action in {"entry", "addition"}
    )
    held = tuple(position.security for position in state.positions)
    held_inputs_valid = all(inputs.prices.get(security) is not None for security in held)
    held_inputs_valid = held_inputs_valid and (
        not held
        or (
            inputs.covariance is not None
            and inputs.covariance.aligned_samples >= inputs.minimum_aligned_samples
            and inputs.covariance.covers(held)
        )
    )
    if not held_inputs_valid:
        return RiskDecision(
            allow_new_risk=False,
            approved=exits,
            rejected=buys,
            reason_codes=("held_risk_input_missing",),
            projected_volatility=None,
        )
    if not buys:
        return RiskDecision(True, exits, (), (), None)

    reasons: list[str] = []
    projected, available_cash = _position_projection(state, exits)
    for intent in buys:
        if intent.quantity % inputs.lot_size:
            reasons.append("invalid_lot")
        if intent.standard_unit is None or intent.quantity > intent.standard_unit:
            reasons.append("standard_unit_cap")
        turnover = inputs.median_turnover_20d.get(intent.security)
        if turnover is None or turnover < inputs.minimum_turnover:
            reasons.append("liquidity_floor")
        elif (
            intent.expected_price * intent.quantity
            > turnover * inputs.maximum_order_turnover_fraction
        ):
            reasons.append("order_liquidity_cap")
        existing = projected.get(intent.security)
        if intent.action == "entry" and existing is not None:
            reasons.append("invalid_position_transition")
            continue
        if intent.action == "addition" and existing is None:
            reasons.append("invalid_position_transition")
            continue
        if intent.common_stop_after is None:
            reasons.append("invalid_common_stop")
            continue
        if existing is not None and intent.common_stop_after < existing["common_stop"]:
            reasons.append("common_stop_decrease")
            continue
        if existing is None:
            existing = {
                "asset_group": intent.asset_group,
                "batches": [],
                "common_stop": intent.common_stop_after,
            }
            projected[intent.security] = existing
        existing["common_stop"] = intent.common_stop_after
        existing["batches"].append(
            Batch(
                execution_date=intent.execution_date,
                quantity=intent.quantity,
                fill_price=intent.expected_price,
            )
        )
        available_cash -= intent.expected_price * intent.quantity + intent.estimated_fee

    if available_cash < 0:
        reasons.append("insufficient_cash")

    projected_securities = tuple(sorted(projected))
    covariance = inputs.covariance
    covariance_valid = (
        covariance is not None
        and covariance.aligned_samples >= inputs.minimum_aligned_samples
        and covariance.covers(projected_securities)
    )
    if not covariance_valid:
        reasons.append("covariance_unavailable")

    values: dict[str, Decimal] = {}
    risks: dict[str, Decimal] = {}
    groups: dict[str, str] = {}
    for security, position in projected.items():
        batches = position["batches"]
        price = next(
            (
                intent.expected_price
                for intent in reversed(buys)
                if intent.security == security
            ),
            inputs.prices.get(security),
        )
        if price is None:
            reasons.append("price_unavailable")
            continue
        quantity = sum(batch.quantity for batch in batches)
        stop = position["common_stop"]
        values[security] = _decimal(price, "price", positive=True) * quantity
        risks[security] = max(
            Decimal("0"),
            sum((batch.fill_price - stop) * batch.quantity for batch in batches),
        )
        groups[security] = str(position["asset_group"])

    group_values: dict[str, Decimal] = {}
    group_risks: dict[str, Decimal] = {}
    for security in values:
        group = groups[security]
        group_values[group] = group_values.get(group, Decimal("0")) + values[security]
        group_risks[group] = group_risks.get(group, Decimal("0")) + risks[security]
        if values[security] > state.equity * inputs.security_value_cap:
            reasons.append("security_value_cap")
        if risks[security] > state.equity * inputs.security_risk_cap:
            reasons.append("security_risk_cap")
    if any(value > state.equity * inputs.asset_group_value_cap for value in group_values.values()):
        reasons.append("group_value_cap")
    if any(value > state.equity * inputs.asset_group_risk_cap for value in group_risks.values()):
        reasons.append("group_risk_cap")
    if sum(values.values()) > state.equity * inputs.portfolio_value_cap:
        reasons.append("portfolio_value_cap")
    if sum(risks.values()) > state.equity * inputs.portfolio_risk_cap:
        reasons.append("portfolio_risk_cap")

    projected_volatility: Decimal | None = None
    if covariance_valid and len(values) == len(projected):
        projected_volatility = _annualized_volatility(
            values,
            equity=state.equity,
            covariance=covariance,
        )
        if projected_volatility > inputs.target_volatility:
            reasons.append("target_volatility")

    unique_reasons = tuple(dict.fromkeys(reasons))
    return RiskDecision(
        allow_new_risk=True,
        approved=exits if unique_reasons else requests,
        rejected=buys if unique_reasons else (),
        reason_codes=unique_reasons,
        projected_volatility=projected_volatility,
    )
