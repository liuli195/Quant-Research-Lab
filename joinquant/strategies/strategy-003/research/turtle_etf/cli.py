from __future__ import annotations

import argparse
import copy
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd
import numpy as np

if __package__ in {None, ""}:
    RESEARCH_ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(RESEARCH_ROOT))
    from turtle_etf.execution import (
        DailyMarket,
        ExecutionCosts,
        MarketQuote,
        TradingDay,
        process_day,
    )
    from turtle_etf.indicators import breakout_levels, turtle_n
    from turtle_etf.reporting import (
        OutputValidationError,
        ResearchResult,
        RunIdentity,
        decimal_text,
        validate_project_outputs,
        write_complete_reports,
        write_outputs,
    )
    from turtle_etf.risk import (
        PortfolioState,
        RiskInputs,
        estimate_covariance,
        initial_unit,
        portfolio_volatility,
        target_volatility_reductions,
    )
    from turtle_etf.signals import entry_signal, make_entry_intent
    from turtle_etf.state import commission_fee, request_addition, request_full_exit
else:
    from .execution import DailyMarket, ExecutionCosts, MarketQuote, TradingDay, process_day
    from .indicators import breakout_levels, turtle_n
    from .reporting import (
        OutputValidationError,
        ResearchResult,
        RunIdentity,
        decimal_text,
        validate_project_outputs,
        write_complete_reports,
        write_outputs,
    )
    from .risk import (
        PortfolioState,
        RiskInputs,
        estimate_covariance,
        initial_unit,
        portfolio_volatility,
        target_volatility_reductions,
    )
    from .signals import entry_signal, make_entry_intent
    from .state import commission_fee, request_addition, request_full_exit

from scripts.research.market_data.query import open_snapshot
from scripts.research.quant_analysis.contracts import (
    AnalysisBundle,
    BENCHMARK_IDS,
    read_analysis_table,
)
from scripts.research.quant_analysis.cvar import calculate_cvar_scenarios
from scripts.research.quant_analysis.evidence import (
    ScenarioResult,
    build_evidence_matrix,
    evidence_digest,
    validate_evidence_matrix,
)
from scripts.research.quant_analysis.metrics import calculate_performance
from scripts.research.quant_analysis.robustness import (
    asset_deletion_scenarios,
    calculate_bootstrap_scenarios,
    cost_execution_scenarios,
    fixed_period_scenarios,
    parameter_scenarios,
    rolling_three_year_scenarios,
    run_path_scenarios,
)
from scripts.research.quant_analysis.stress import (
    POSITION_SHOCKS,
    calculate_historical_stress,
    calculate_position_shocks,
)


class ResearchEvidenceInsufficient(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectResult:
    status: str
    reason_codes: tuple[str, ...]
    output_dir: Path


_CANDIDATE_IDS = (
    "baseline",
    "entry-40",
    "entry-60",
    "stop-1.5n",
    "stop-2.5n",
    "covariance-120d",
    "covariance-ewma-30d",
)


def _merge_candidate_overrides(
    base_config: Mapping[str, object],
    overrides: Mapping[str, object],
) -> dict[str, object]:
    merged = copy.deepcopy(dict(base_config))
    for dotted_path, value in overrides.items():
        parts = str(dotted_path).split(".")
        target = merged
        for part in parts[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                raise ValueError(f"candidate override path is invalid: {dotted_path}")
            target = child
        field = parts[-1]
        if isinstance(value, Mapping) and isinstance(target.get(field), dict):
            target[field].update(copy.deepcopy(dict(value)))
        else:
            target[field] = copy.deepcopy(value)
    return merged


def run_candidate_set(
    base_config: Mapping[str, object],
    candidates: Sequence[Mapping[str, object]],
    run_candidate,
    *,
    baseline_result: object | None = None,
) -> tuple[tuple[str, object], ...]:
    candidate_ids = tuple(str(item.get("id")) for item in candidates)
    if candidate_ids != _CANDIDATE_IDS:
        raise ValueError("candidate set differs from the frozen seven")
    results: list[tuple[str, object]] = []
    for item in candidates:
        overrides = item.get("overrides")
        if not isinstance(overrides, Mapping):
            raise ValueError("candidate overrides must be a mapping")
        config = _merge_candidate_overrides(base_config, overrides)
        candidate_id = str(item["id"])
        value = (
            baseline_result
            if candidate_id == "baseline" and baseline_result is not None
            else run_candidate(config)
        )
        results.append((candidate_id, value))
    return tuple(results)


def _load_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchEvidenceInsufficient(f"invalid_{label}") from exc
    if not isinstance(value, dict):
        raise ResearchEvidenceInsufficient(f"invalid_{label}")
    return value


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    result = Decimal(str(value))
    return result if result.is_finite() else None


def _risk_inputs(
    *,
    config: Mapping[str, object],
    securities: tuple[str, ...],
    frames: Mapping[str, pd.DataFrame],
    returns: pd.DataFrame,
    through_date: str,
    prices: Mapping[str, Decimal | None],
) -> RiskInputs:
    risk = config["risk"]
    covariance_config = risk["covariance"]
    window_days = int(covariance_config["window_days"])
    through_returns = returns.loc[:through_date]
    eligible_securities = tuple(
        security
        for security in securities
        if sum(
            pd.notna(value) and math.isfinite(float(value))
            for value in pd.to_numeric(
                through_returns[security],
                errors="coerce",
            )
        )
        >= window_days
    )
    covariance = (
        None
        if not eligible_securities
        else estimate_covariance(
            through_returns,
            securities=eligible_securities,
            days=window_days,
            method=str(covariance_config.get("method", "sample")),
            half_life_days=(
                None
                if covariance_config.get("half_life_days") is None
                else int(covariance_config["half_life_days"])
            ),
        )
    )
    turnover: dict[str, Decimal] = {}
    for security in securities:
        values = pd.to_numeric(
            frames[security].loc[:through_date, "money"],
            errors="coerce",
        ).dropna()
        if len(values) >= 20:
            turnover[security] = Decimal(str(values.tail(20).median()))
    return RiskInputs(
        prices=prices,
        median_turnover_20d=turnover,
        covariance=covariance,
        minimum_aligned_samples=int(risk["minimum_aligned_samples"]),
        security_risk_cap=Decimal(str(risk["security_risk_cap"])),
        security_value_cap=Decimal(str(risk["security_value_cap"])),
        asset_group_risk_cap=Decimal(str(risk["asset_group_risk_cap"])),
        asset_group_value_cap=Decimal(str(risk["asset_group_value_cap"])),
        portfolio_risk_cap=Decimal(str(risk["portfolio_risk_cap"])),
        portfolio_value_cap=Decimal(str(risk["portfolio_value_cap"])),
        target_volatility=Decimal(str(risk["target_volatility"])),
    )


def _prepare_frames(
    rows: Sequence[Mapping[str, object]],
    config: Mapping[str, object],
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, tuple[str, ...]]:
    universe = tuple(str(item["security"]) for item in config["universe"])
    frame = pd.DataFrame([dict(row) for row in rows])
    if frame.empty or set(frame["security"].unique()) != set(universe):
        raise ResearchEvidenceInsufficient("snapshot_universe_mismatch")
    frames: dict[str, pd.DataFrame] = {}
    signal = config["signal"]
    for security in universe:
        selected = frame.loc[frame["security"] == security].copy()
        selected = selected.sort_values("date").set_index("date", drop=False)
        selected["n"] = turtle_n(selected, days=int(signal["n_days"]))
        levels = breakout_levels(
            selected,
            entry_days=int(signal["entry_days"]),
            exit_days=int(signal["exit_days"]),
        )
        selected["entry_high"] = levels["entry_high"]
        selected["exit_low"] = levels["exit_low"]
        frames[security] = selected
    close = frame.pivot(index="date", columns="security", values="close")
    close = close.sort_index().apply(pd.to_numeric, errors="coerce")
    returns = close.pct_change(fill_method=None)
    return frames, returns, universe


def _quote(row: pd.Series) -> MarketQuote:
    return MarketQuote(
        open=_decimal(row["open"]),
        paused=bool(row["paused"]),
        high_limit=_decimal(row["high_limit"]),
        low_limit=_decimal(row["low_limit"]),
    )


def _percent(value: Decimal) -> str:
    return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"


def _standard_orders(
    audit_rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in audit_rows:
        action = str(item["action"])
        filled_quantity = float(item["filled_quantity"])
        fill_price = (
            None if item.get("fill_price") in {None, ""} else float(item["fill_price"])
        )
        fee = float(item.get("fee", 0.0))
        rows.append(
            {
                "order_id": f"{item['date']}:{int(item['sequence']):06d}",
                "date": str(item["date"]),
                "security": str(item["security"]),
                "side": "buy" if action in {"entry", "addition"} else "sell",
                "requested_quantity": float(item["requested_quantity"]),
                "filled_quantity": filled_quantity,
                "fill_price": fill_price,
                "fee": fee,
                "status": str(item["status"]),
                "reason": str(item["reason"]),
            }
        )
    return rows


def _round_trip_trades(
    fills: Sequence[Mapping[str, object]],
    groups: Mapping[str, str],
) -> list[dict[str, object]]:
    lots: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    closed: list[dict[str, object]] = []
    trade_sequence = 0
    for item in sorted(
        fills,
        key=lambda row: (str(row["date"]), int(row["sequence"])),
    ):
        security = str(item["security"])
        action = str(item["action"])
        quantity = int(item["quantity"])
        price = float(item["fill_price"])
        fee = float(item["fee"])
        if action in {"entry", "addition"}:
            lots[security].append(
                {
                    "date": str(item["date"]),
                    "quantity": quantity,
                    "price": price,
                    "fee_per_share": fee / quantity,
                    "reason": str(item["reason"]),
                }
            )
            continue
        remaining = quantity
        while remaining > 0:
            if not lots[security]:
                raise ValueError(f"sell fill exceeds tracked entry lots: {security}")
            lot = lots[security][0]
            matched = min(remaining, int(lot["quantity"]))
            entry_fee = float(lot["fee_per_share"]) * matched
            exit_fee = fee * matched / quantity
            entry_notional = float(lot["price"]) * matched
            pnl = (price - float(lot["price"])) * matched - entry_fee - exit_fee
            trade_sequence += 1
            closed.append(
                {
                    "trade_id": f"trade-{trade_sequence:08d}",
                    "entry_date": str(lot["date"]),
                    "exit_date": str(item["date"]),
                    "security": security,
                    "asset_group": groups[security],
                    "quantity": float(matched),
                    "entry_price": float(lot["price"]),
                    "exit_price": price,
                    "fees": entry_fee + exit_fee,
                    "pnl": pnl,
                    "return": pnl / (entry_notional + entry_fee),
                    "entry_reason": str(lot["reason"]),
                    "exit_reason": str(item["reason"]),
                }
            )
            lot["quantity"] = int(lot["quantity"]) - matched
            if int(lot["quantity"]) == 0:
                lots[security].pop(0)
            remaining -= matched
    return closed


def _standard_analysis_rows(
    *,
    dates: Sequence[str],
    groups: Mapping[str, str],
    audit_rows: Sequence[Mapping[str, object]],
    trade_rows: Sequence[Mapping[str, object]],
    position_rows: Sequence[Mapping[str, object]],
    risk_rows: Sequence[Mapping[str, object]],
    benchmark_rows: Sequence[Mapping[str, object]],
) -> dict[str, list[dict[str, object]]]:
    orders = _standard_orders(audit_rows)
    fees_by_date: defaultdict[str, float] = defaultdict(float)
    orders_by_date_security: defaultdict[
        tuple[str, str], list[Mapping[str, object]]
    ] = defaultdict(list)
    for row in orders:
        fees_by_date[str(row["date"])] += float(row["fee"])
        if row["status"] == "filled":
            orders_by_date_security[(str(row["date"]), str(row["security"]))].append(row)
    actions_by_date_security: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for row in audit_rows:
        if row["status"] == "filled":
            actions_by_date_security[(str(row["date"]), str(row["security"]))].append(
                str(row["action"])
            )
    legacy_positions: defaultdict[
        str, dict[str, Mapping[str, object]]
    ] = defaultdict(dict)
    for row in position_rows:
        legacy_positions[str(row["date"])][str(row["security"])] = row
    legacy_risk = {str(row["date"]): row for row in risk_rows}
    equity_rows: list[dict[str, object]] = []
    return_rows: list[dict[str, object]] = []
    standard_positions: list[dict[str, object]] = []
    standard_risk: list[dict[str, object]] = []
    previous_equity: float | None = None
    previous_positions: dict[str, Mapping[str, object]] = {}
    for current_date in dates:
        risk = legacy_risk[current_date]
        equity = float(risk["equity"])
        cash = float(risk["cash"])
        current_positions = legacy_positions[current_date]
        positions_value = sum(
            float(row["market_value"]) for row in current_positions.values()
        )
        daily_return = 0.0 if previous_equity is None else equity / previous_equity - 1.0
        attributed_return = 0.0
        traded_securities = {
            security
            for date_security in orders_by_date_security
            if date_security[0] == current_date
            for security in (date_security[1],)
        }
        attribution_securities = sorted(
            set(current_positions) | set(previous_positions) | traded_securities
        )
        for security in attribution_securities:
            row = current_positions.get(security)
            previous = previous_positions.get(security)
            template = row if row is not None else previous
            if template is None:
                raise ValueError("position attribution identity is missing")
            transactions = orders_by_date_security[(current_date, security)]
            buys = sum(
                float(item["filled_quantity"]) * float(item["fill_price"])
                for item in transactions
                if item["side"] == "buy"
            )
            sales = sum(
                float(item["filled_quantity"]) * float(item["fill_price"])
                for item in transactions
                if item["side"] == "sell"
            )
            transaction_fees = sum(float(item["fee"]) for item in transactions)
            market_value = 0.0 if row is None else float(row["market_value"])
            previous_value = (
                0.0 if previous is None else float(previous["market_value"])
            )
            pnl_contribution = (
                0.0
                if previous_equity is None
                else market_value - previous_value + sales - buys - transaction_fees
            )
            return_contribution = (
                0.0
                if previous_equity is None
                else pnl_contribution / previous_equity
            )
            attributed_return += return_contribution
            actions = actions_by_date_security[(current_date, security)]
            attribution_reason = (
                "+".join(sorted(set(actions)))
                if actions
                else ("initial_position" if previous_equity is None else "holding")
            )
            close = (
                float(row["close"])
                if row is not None
                else (
                    float(transactions[-1]["fill_price"])
                    if transactions
                    else float(previous["close"])
                )
            )
            standard_positions.append(
                {
                    "date": current_date,
                    "security": security,
                    "asset_group": str(template["asset_group"]),
                    "quantity": 0.0 if row is None else float(row["quantity"]),
                    "close": close,
                    "market_value": market_value,
                    "weight": market_value / equity,
                    "planned_loss": 0.0 if row is None else float(row["planned_loss"]),
                    "common_stop": 0.0 if row is None else float(row["common_stop"]),
                    "signal_n": 0.0 if row is None else float(row["signal_n"]),
                    "stop_failure_loss": (
                        0.0 if row is None else float(row["stop_failure_loss"])
                    ),
                    "attribution_reason": attribution_reason,
                    "pnl_contribution": pnl_contribution,
                    "return_contribution": return_contribution,
                }
            )
        if not math.isclose(attributed_return, daily_return, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("actual security PnL does not reconcile to portfolio return")
        equity_rows.append(
            {
                "date": current_date,
                "portfolio_id": "strategy-003",
                "currency": "CNY",
                "equity": equity,
                "cash": cash,
                "positions_value": positions_value,
                "daily_pnl": 0.0 if previous_equity is None else equity - previous_equity,
                "fees": fees_by_date[current_date],
            }
        )
        return_rows.append(
            {
                "date": current_date,
                "portfolio_id": "strategy-003",
                "return": daily_return,
                "equity": equity,
                "cash_return_contribution": 0.0,
            }
        )
        standard_risk.append(
            {
                "date": current_date,
                "portfolio_id": "strategy-003",
                "equity": equity,
                "cash": cash,
                "invested_ratio": float(risk["invested_ratio"]),
                "cash_ratio": float(risk["cash_ratio"]),
                "planned_risk": float(risk["portfolio_planned_risk"]),
                "portfolio_risk_usage": float(risk["portfolio_risk_usage"]),
                "portfolio_volatility": (
                    None
                    if risk["portfolio_volatility"] in {None, ""}
                    else float(risk["portfolio_volatility"])
                ),
                "target_volatility_usage": (
                    None
                    if risk["target_volatility_usage"] in {None, ""}
                    else float(risk["target_volatility_usage"])
                ),
            }
        )
        previous_equity = equity
        previous_positions = dict(current_positions)
    events = [
        {
            "event_id": f"{row['date']}:{int(row['sequence']):06d}",
            "date": str(row["date"]),
            "sequence": int(row["sequence"]),
            "security": str(row["security"]) or None,
            "event_type": str(row["action"]),
            "status": str(row["status"]),
            "reason": str(row["reason"]),
        }
        for row in audit_rows
    ]
    return {
        "equity": equity_rows,
        "returns": return_rows,
        "trades": _round_trip_trades(trade_rows, groups),
        "orders": orders,
        "positions": standard_positions,
        "risk": standard_risk,
        "events": events,
        "benchmarks": [dict(row) for row in benchmark_rows],
    }


def _simulate(
    *,
    config: Mapping[str, object],
    candidates: Sequence[Mapping[str, object]],
    identity: RunIdentity,
    snapshot_normalized_sha256: str,
    rows: Sequence[Mapping[str, object]],
    benchmark_rows: Sequence[Mapping[str, object]],
) -> ResearchResult:
    excluded_securities = {
        str(value) for value in config.get("exclude_securities", ())
    }
    excluded_groups = {
        str(value) for value in config.get("exclude_asset_groups", ())
    }
    config = {
        **dict(config),
        "universe": [
            dict(item)
            for item in config["universe"]
            if str(item["security"]) not in excluded_securities
            and str(item["asset_group"]) not in excluded_groups
        ],
    }
    if not config["universe"]:
        raise ResearchEvidenceInsufficient("scenario_universe_empty")
    research_window = config.get("research_window", {})
    start_date = str(research_window.get("start_date", "0001-01-01"))
    end_date = str(research_window.get("end_date", "9999-12-31"))
    allowed_securities = {str(item["security"]) for item in config["universe"]}
    rows = tuple(
        row
        for row in rows
        if start_date <= str(row["date"]) <= end_date
        and str(row["security"]) in allowed_securities
    )
    benchmark_rows = tuple(
        row
        for row in benchmark_rows
        if start_date <= str(row["date"]) <= end_date
    )
    if not rows:
        raise ResearchEvidenceInsufficient("scenario_window_empty")
    frames, returns, securities = _prepare_frames(rows, config)
    groups = {
        str(item["security"]): str(item["asset_group"])
        for item in config["universe"]
    }
    dates = tuple(sorted(set(str(row["date"]) for row in rows)))
    initial_cash = Decimal(str(config["research"]["initial_cash"]))
    portfolio = PortfolioState(initial_cash, initial_cash)
    pending_by_date: defaultdict[str, list[object]] = defaultdict(list)
    costs_config = config.get("costs", {})
    execution_costs = ExecutionCosts(
        commission_multiplier=Decimal(
            str(costs_config.get("commission_multiplier", 1.0))
        ),
        one_way_slippage=Decimal(str(costs_config.get("one_way_slippage", 0.0))),
    )
    additional_delay_days = int(
        config.get("execution", {}).get("additional_delay_days", 0)
    )
    if additional_delay_days < 0:
        raise ValueError("additional_delay_days must not be negative")
    audit_rows: list[dict[str, object]] = []
    trade_rows: list[dict[str, object]] = []
    position_rows: list[dict[str, object]] = []
    risk_rows: list[dict[str, object]] = []
    leave_cash = Counter()
    invested_ratios: list[Decimal] = []
    cash_ratios: list[Decimal] = []
    portfolio_risk_usage: list[Decimal] = []
    target_volatility_usage: list[Decimal] = []
    maximum_group_value: dict[str, Decimal] = {}
    maximum_group_risk: dict[str, Decimal] = {}
    last_close: dict[str, Decimal] = {}

    for index, current_date in enumerate(dates):
        rows_today = {
            security: frames[security].loc[current_date]
            for security in securities
            if current_date in frames[security].index
        }
        previous_date = dates[max(0, index - 1)]
        open_prices = {
            security: (
                None
                if security not in rows_today
                else _decimal(rows_today[security]["open"])
            )
            for security in securities
        }
        open_risk = _risk_inputs(
            config=config,
            securities=securities,
            frames=frames,
            returns=returns,
            through_date=previous_date,
            prices=open_prices,
        )
        pending_intents = tuple(pending_by_date.pop(current_date, ()))
        if pending_intents:
            day_result = process_day(
                TradingDay(date=current_date, intents=pending_intents),
                portfolio,
                DailyMarket(
                    quotes={
                        security: _quote(row) for security, row in rows_today.items()
                    },
                    risk_inputs=open_risk,
                ),
                costs=execution_costs,
            )
            portfolio = day_result.portfolio
            for item in day_result.audit:
                row = {
                    "date": current_date,
                    **item.to_document(),
                    "allocation_sha256": day_result.allocation.audit_sha256,
                }
                audit_rows.append(row)
                if item.status == "filled":
                    trade_rows.append(
                        {
                            "date": current_date,
                            "sequence": item.sequence,
                            "security": item.security,
                            "action": item.action,
                            "quantity": item.filled_quantity,
                            "fill_price": decimal_text(item.fill_price),
                            "fee": decimal_text(item.fee),
                            "reason": item.reason,
                        }
                    )
                else:
                    leave_cash[item.reason] += 1

        close_prices = {
            security: (
                None
                if security not in rows_today
                else _decimal(rows_today[security]["close"])
            )
            for security in securities
        }
        last_close.update(
            {
                security: value
                for security, value in close_prices.items()
                if value is not None
            }
        )
        equity = portfolio.cash + sum(
            (
                last_close[position.security] * position.quantity
                for position in portfolio.positions
                if position.security in last_close
            ),
            Decimal("0"),
        )
        portfolio = PortfolioState(equity, portfolio.cash, portfolio.positions)
        close_risk = _risk_inputs(
            config=config,
            securities=securities,
            frames=frames,
            returns=returns,
            through_date=current_date,
            prices=close_prices,
        )
        group_values: dict[str, Decimal] = {}
        group_risks: dict[str, Decimal] = {}
        for position in portfolio.positions:
            close_price = last_close.get(position.security)
            if close_price is None:
                continue
            value = close_price * position.quantity
            group = position.asset_group
            group_values[group] = group_values.get(group, Decimal("0")) + value
            group_risks[group] = group_risks.get(group, Decimal("0")) + position.planned_loss
            position_rows.append(
                {
                    "date": current_date,
                    "security": position.security,
                    "asset_group": group,
                    "quantity": position.quantity,
                    "close": decimal_text(close_price),
                    "market_value": decimal_text(value),
                    "common_stop": decimal_text(position.common_stop),
                    "signal_n": decimal_text(position.signal_n),
                    "planned_loss": decimal_text(position.planned_loss),
                    "stop_failure_loss": decimal_text(
                        max(
                            Decimal("0"),
                            (
                                close_price
                                - (
                                    position.common_stop
                                    - Decimal("2") * position.signal_n
                                )
                            )
                            * position.quantity,
                        )
                    ),
                }
            )
        invested = sum(group_values.values(), Decimal("0")) / equity
        cash_ratio = portfolio.cash / equity
        planned_risk = sum(group_risks.values(), Decimal("0"))
        risk_usage = planned_risk / (
            equity * Decimal(str(config["risk"]["portfolio_risk_cap"]))
        )
        volatility = portfolio_volatility(portfolio, close_risk)
        volatility_usage = (
            Decimal("0")
            if volatility is None
            else volatility / Decimal(str(config["risk"]["target_volatility"]))
        )
        group_value_usage = {
            group: value
            / (equity * Decimal(str(config["risk"]["asset_group_value_cap"])))
            for group, value in group_values.items()
        }
        group_risk_usage = {
            group: value
            / (equity * Decimal(str(config["risk"]["asset_group_risk_cap"])))
            for group, value in group_risks.items()
        }
        for group, value in group_value_usage.items():
            maximum_group_value[group] = max(maximum_group_value.get(group, Decimal("0")), value)
        for group, value in group_risk_usage.items():
            maximum_group_risk[group] = max(maximum_group_risk.get(group, Decimal("0")), value)
        if invested < Decimal("1"):
            leave_cash["risk_or_no_active_trend"] += 1
        invested_ratios.append(invested)
        cash_ratios.append(cash_ratio)
        portfolio_risk_usage.append(risk_usage)
        target_volatility_usage.append(volatility_usage)
        risk_rows.append(
            {
                "date": current_date,
                "equity": decimal_text(equity),
                "cash": decimal_text(portfolio.cash),
                "invested_ratio": decimal_text(invested),
                "cash_ratio": decimal_text(cash_ratio),
                "portfolio_planned_risk": decimal_text(planned_risk),
                "portfolio_risk_usage": decimal_text(risk_usage),
                "portfolio_volatility": decimal_text(volatility),
                "target_volatility_usage": decimal_text(volatility_usage),
                "asset_group_value_usage": json.dumps(
                    {key: decimal_text(value) for key, value in group_value_usage.items()},
                    sort_keys=True,
                ),
                "asset_group_risk_usage": json.dumps(
                    {key: decimal_text(value) for key, value in group_risk_usage.items()},
                    sort_keys=True,
                ),
                "eligible_securities": json.dumps(
                    list(
                        ()
                        if close_risk.covariance is None
                        else close_risk.covariance.securities
                    ),
                    sort_keys=True,
                ),
                "cold_start_securities": json.dumps(
                    [
                        security
                        for security in securities
                        if close_risk.covariance is None
                        or security not in close_risk.covariance.securities
                    ],
                    sort_keys=True,
                ),
                "leave_cash_reasons": json.dumps(dict(sorted(leave_cash.items())), sort_keys=True),
            }
        )

        execution_index = index + 1 + additional_delay_days
        if execution_index >= len(dates):
            continue
        next_date = dates[execution_index]
        pending_securities = {
            str(intent.security)
            for scheduled in pending_by_date.values()
            for intent in scheduled
        }
        intents = list(
            intent
            for intent in target_volatility_reductions(
                    portfolio,
                    close_risk,
                    signal_date=current_date,
                    execution_date=next_date,
                    reduction_target=Decimal(
                        str(config["risk"]["risk_reduction_target_volatility"])
                    ),
                )
            if intent.security not in pending_securities
        )
        positions = {position.security: position for position in portfolio.positions}
        for security in securities:
            if security not in rows_today:
                continue
            if security in pending_securities:
                continue
            row = rows_today[security]
            close = _decimal(row["close"])
            n_value = _decimal(row["n"])
            entry_high = _decimal(row["entry_high"])
            exit_low = _decimal(row["exit_low"])
            position = positions.get(security)
            if close is None:
                continue
            if position is not None:
                exit_intent = request_full_exit(
                    position,
                    signal_date=current_date,
                    execution_date=next_date,
                    close=close,
                    exit_level=exit_low,
                    expected_price=close,
                )
                if exit_intent is not None:
                    intents.append(
                        replace(
                            exit_intent,
                            estimated_fee=commission_fee(close, exit_intent.quantity),
                        )
                    )
                    continue
                requested, addition = request_addition(
                    position,
                    signal_date=current_date,
                    execution_date=next_date,
                    close=close,
                    expected_price=close,
                )
                positions[security] = requested
                if addition is not None:
                    intents.append(
                        replace(
                            addition,
                            estimated_fee=commission_fee(close, addition.quantity),
                        )
                    )
            elif n_value is not None and entry_signal(close, entry_high):
                quantity = initial_unit(
                    portfolio.equity,
                    n_value,
                    Decimal(str(config["risk"]["risk_per_unit"])),
                )
                if quantity > 0:
                    entry = make_entry_intent(
                        security=security,
                        asset_group=groups[security],
                        signal_date=current_date,
                        execution_date=next_date,
                        expected_price=close,
                        quantity=quantity,
                        signal_n=n_value,
                        standard_unit=quantity,
                        stop_n=Decimal(str(config["signal"]["stop_n"])),
                    )
                    intents.append(
                        replace(
                            entry,
                            estimated_fee=commission_fee(close, quantity),
                        )
                    )
        portfolio = PortfolioState(
            portfolio.equity,
            portfolio.cash,
            tuple(positions[security] for security in sorted(positions)),
        )
        pending_by_date[next_date].extend(intents)

    count = Decimal(len(invested_ratios))
    metrics = {
        "audit_events": len(audit_rows),
        "filled_trades": len(trade_rows),
        "average_invested_ratio": _percent(sum(invested_ratios) / count),
        "median_invested_ratio": _percent(statistics.median(invested_ratios)),
        "below_half_ratio": _percent(
            Decimal(sum(value < Decimal("0.5") for value in invested_ratios)) / count
        ),
        "near_full_ratio": _percent(
            Decimal(sum(value >= Decimal("0.9") for value in invested_ratios)) / count
        ),
        "average_cash_ratio": _percent(sum(cash_ratios) / count),
        "leave_cash_reasons": dict(sorted(leave_cash.items())),
        "maximum_asset_group_value_usage": {
            key: _percent(value) for key, value in sorted(maximum_group_value.items())
        },
        "maximum_asset_group_risk_usage": {
            key: _percent(value) for key, value in sorted(maximum_group_risk.items())
        },
        "maximum_portfolio_risk_usage": _percent(max(portfolio_risk_usage)),
        "maximum_target_volatility_usage": _percent(max(target_volatility_usage)),
    }
    analysis_rows = _standard_analysis_rows(
        dates=dates,
        groups=groups,
        audit_rows=audit_rows,
        trade_rows=trade_rows,
        position_rows=position_rows,
        risk_rows=risk_rows,
        benchmark_rows=benchmark_rows,
    )
    return ResearchResult(
        identity=identity,
        snapshot_normalized_sha256=snapshot_normalized_sha256,
        config=config,
        candidates=tuple(candidates),
        audit_rows=tuple(audit_rows),
        trade_rows=tuple(trade_rows),
        position_rows=tuple(position_rows),
        risk_rows=tuple(risk_rows),
        analysis_rows=analysis_rows,
        metrics=metrics,
        recommendation="proceed_to_joinquant",
        reasons=(
            "deterministic_local_flow_completed",
            "fixed_candidate_package_preserved",
            "joinquant_formal_backtest_required",
        ),
    )


def _write_status(output_dir: Path, status: str, reasons: Sequence[str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": 1,
        "status": status,
        "reason_codes": list(reasons),
    }
    if status == "complete":
        document["next_action"] = "human_confirmation_required"
    (output_dir / "project-status.json").write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _analysis_bundle(result: ResearchResult) -> AnalysisBundle:
    digest_input = {
        name: [dict(row) for row in rows]
        for name, rows in result.analysis_rows.items()
    }
    return AnalysisBundle(
        path=Path("."),
        tables=result.analysis_rows,
        digest=evidence_digest(digest_input),
    )


def _complete_robustness_results(
    *,
    base_config: Mapping[str, object],
    candidates: Sequence[Mapping[str, object]],
    identity: RunIdentity,
    snapshot_normalized_sha256: str,
    rows: Sequence[Mapping[str, object]],
    benchmark_rows: Sequence[Mapping[str, object]],
    baseline: ResearchResult,
    candidate_results: Sequence[tuple[str, ResearchResult]],
) -> tuple[ScenarioResult, ...]:
    dates = sorted({str(row["date"]) for row in rows})
    securities = tuple(str(item["security"]) for item in base_config["universe"])
    asset_groups = tuple(
        sorted({str(item["asset_group"]) for item in base_config["universe"]})
    )
    path_scenarios = (
        *fixed_period_scenarios(dates[-1]),
        *rolling_three_year_scenarios(max(dates[0], "2015-01-01"), dates[-1]),
        *asset_deletion_scenarios(
            securities=securities,
            asset_groups=asset_groups,
        ),
        *cost_execution_scenarios(),
    )

    def run_scenario(config: dict[str, object]) -> dict[str, float | int | None]:
        scenario_result = _simulate(
            config=config,
            candidates=candidates,
            identity=identity,
            snapshot_normalized_sha256=snapshot_normalized_sha256,
            rows=rows,
            benchmark_rows=benchmark_rows,
        )
        metrics = calculate_performance(_analysis_bundle(scenario_result))
        return {
            key: value
            for key, value in {
                **metrics,
                "audit_events": len(scenario_result.audit_rows),
                "filled_orders": len(scenario_result.trade_rows),
            }.items()
            if value is None or isinstance(value, (int, float))
        }

    baseline_bundle = _analysis_bundle(baseline)
    candidates_by_id = dict(candidate_results)

    def candidate_metrics(config: dict[str, object]) -> dict[str, float | int | None]:
        candidate = candidates_by_id[str(config["scenario_id"])]
        return {
            key: value
            for key, value in calculate_performance(_analysis_bundle(candidate)).items()
            if value is None or isinstance(value, (int, float))
        }

    returns = np.asarray(
        [float(row["return"]) for row in baseline_bundle.rows("returns")],
        dtype=np.float64,
    )
    return (
        *run_path_scenarios(
            base_config,
            parameter_scenarios(),
            candidate_metrics,
        ),
        *run_path_scenarios(base_config, path_scenarios, run_scenario),
        *calculate_bootstrap_scenarios(returns),
        *calculate_historical_stress(baseline_bundle),
        *calculate_position_shocks(
            baseline_bundle.rows("positions"),
            POSITION_SHOCKS,
        ),
        *calculate_cvar_scenarios(returns),
    )


def run_research(
    config_path: Path,
    snapshot_path: Path,
    output_dir: Path,
    *,
    market_data_root: Path | None = None,
    benchmark_input: Path | None = None,
    identity: RunIdentity | None = None,
) -> ProjectResult:
    output_dir = Path(output_dir)
    try:
        if identity is None:
            raise ResearchEvidenceInsufficient("missing_run_identity")
        config = _load_object(config_path, "project_config")
        candidates_document = _load_object(
            Path(config_path).with_name("candidates.json"),
            "candidate_config",
        )
        candidates = candidates_document.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 7:
            raise ResearchEvidenceInsufficient("invalid_candidate_config")
        snapshot_document = _load_object(snapshot_path, "snapshot")
        if snapshot_document.get("snapshot_id") != identity.snapshot_id:
            raise ResearchEvidenceInsufficient("snapshot_identity_mismatch")
        root = (
            Path(market_data_root)
            if market_data_root is not None
            else Path(snapshot_path).resolve().parents[1]
        )
        snapshot_view = open_snapshot(identity.snapshot_id, root=root)
        if benchmark_input is None or not Path(benchmark_input).is_file():
            raise ResearchEvidenceInsufficient("missing_benchmark_input")
        all_benchmark_rows = read_analysis_table("benchmarks", benchmark_input)
        expected_dates = {str(row["date"]) for row in snapshot_view.rows}
        benchmark_rows = tuple(
            row for row in all_benchmark_rows if str(row["date"]) in expected_dates
        )
        coverage = {
            benchmark_id: {
                str(row["date"])
                for row in benchmark_rows
                if row["benchmark_id"] == benchmark_id
            }
            for benchmark_id in BENCHMARK_IDS
        }
        if any(dates != expected_dates for dates in coverage.values()):
            raise ResearchEvidenceInsufficient("incomplete_benchmark_input")
        result = _simulate(
            config=config,
            candidates=candidates,
            identity=identity,
            snapshot_normalized_sha256=snapshot_view.digest,
            rows=snapshot_view.rows,
            benchmark_rows=benchmark_rows,
        )
        candidate_results = run_candidate_set(
            config,
            candidates,
            lambda candidate_config: _simulate(
                config=candidate_config,
                candidates=candidates,
                identity=identity,
                snapshot_normalized_sha256=snapshot_view.digest,
                rows=snapshot_view.rows,
                benchmark_rows=benchmark_rows,
            ),
            baseline_result=result,
        )
        if any(not isinstance(value, ResearchResult) for _, value in candidate_results):
            raise TypeError("candidate runner returned an invalid result")
        robustness_results = _complete_robustness_results(
            base_config=config,
            candidates=candidates,
            identity=identity,
            snapshot_normalized_sha256=snapshot_view.digest,
            rows=snapshot_view.rows,
            benchmark_rows=benchmark_rows,
            baseline=result,
            candidate_results=candidate_results,
        )
        write_outputs(result, output_dir)
        evidence_path = build_evidence_matrix(
            robustness_results,
            output_dir / "local-evidence-matrix.parquet",
        )
        validate_evidence_matrix(evidence_path)
        write_complete_reports(
            baseline=result,
            candidate_results=candidate_results,
            robustness_results=robustness_results,
            output_dir=output_dir,
        )
        validate_project_outputs(output_dir, identity)
        _write_status(output_dir, "complete", ())
        return ProjectResult("complete", (), output_dir)
    except ResearchEvidenceInsufficient as exc:
        reason = str(exc)
        _write_status(output_dir, "evidence_insufficient", (reason,))
        return ProjectResult("evidence_insufficient", (reason,), output_dir)
    except (OutputValidationError, ValueError, KeyError, TypeError, ArithmeticError):
        _write_status(output_dir, "failed", ("deterministic_research_failed",))
        return ProjectResult("failed", ("deterministic_research_failed",), output_dir)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--market-data-root", type=Path, required=True)
    parser.add_argument("--project-config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--code-sha256", required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--benchmark-input", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    identity = RunIdentity(
        run_id=args.run_id,
        snapshot_id=args.snapshot_id,
        code_sha256=args.code_sha256,
        config_sha256=args.config_sha256,
    )
    result = run_research(
        args.project_config,
        args.snapshot_manifest,
        args.output_dir,
        market_data_root=args.market_data_root,
        benchmark_input=args.benchmark_input,
        identity=identity,
    )
    return {"complete": 0, "failed": 1, "evidence_insufficient": 2}[result.status]


if __name__ == "__main__":
    raise SystemExit(main())
