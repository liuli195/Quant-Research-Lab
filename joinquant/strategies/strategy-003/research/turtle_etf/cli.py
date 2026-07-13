from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

if __package__ in {None, ""}:
    RESEARCH_ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(RESEARCH_ROOT))
    from turtle_etf.execution import DailyMarket, MarketQuote, TradingDay, process_day
    from turtle_etf.indicators import breakout_levels, turtle_n
    from turtle_etf.reporting import (
        OutputValidationError,
        ResearchResult,
        RunIdentity,
        decimal_text,
        validate_project_outputs,
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
    from .execution import DailyMarket, MarketQuote, TradingDay, process_day
    from .indicators import breakout_levels, turtle_n
    from .reporting import (
        OutputValidationError,
        ResearchResult,
        RunIdentity,
        decimal_text,
        validate_project_outputs,
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


class ResearchEvidenceInsufficient(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectResult:
    status: str
    reason_codes: tuple[str, ...]
    output_dir: Path


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
    window_days = int(risk["covariance"]["window_days"])
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


def _simulate(
    *,
    config: Mapping[str, object],
    candidates: Sequence[Mapping[str, object]],
    identity: RunIdentity,
    snapshot_normalized_sha256: str,
    rows: Sequence[Mapping[str, object]],
) -> ResearchResult:
    frames, returns, securities = _prepare_frames(rows, config)
    groups = {
        str(item["security"]): str(item["asset_group"])
        for item in config["universe"]
    }
    dates = tuple(sorted(set(str(row["date"]) for row in rows)))
    initial_cash = Decimal(str(config["research"]["initial_cash"]))
    portfolio = PortfolioState(initial_cash, initial_cash)
    pending: TradingDay | None = None
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
        if pending is not None:
            day_result = process_day(
                pending,
                portfolio,
                DailyMarket(
                    quotes={
                        security: _quote(row) for security, row in rows_today.items()
                    },
                    risk_inputs=open_risk,
                ),
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
                    "planned_loss": decimal_text(position.planned_loss),
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

        pending = None
        if index + 1 >= len(dates):
            continue
        next_date = dates[index + 1]
        intents = list(
            target_volatility_reductions(
                portfolio,
                close_risk,
                signal_date=current_date,
                execution_date=next_date,
                reduction_target=Decimal(
                    str(config["risk"]["risk_reduction_target_volatility"])
                ),
            )
        )
        positions = {position.security: position for position in portfolio.positions}
        for security in securities:
            if security not in rows_today:
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
        pending = TradingDay(date=next_date, intents=tuple(intents))

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
    return ResearchResult(
        identity=identity,
        snapshot_normalized_sha256=snapshot_normalized_sha256,
        config=config,
        candidates=tuple(candidates),
        audit_rows=tuple(audit_rows),
        trade_rows=tuple(trade_rows),
        position_rows=tuple(position_rows),
        risk_rows=tuple(risk_rows),
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
    (output_dir / "project-status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": status,
                "reason_codes": list(reasons),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def run_research(
    config_path: Path,
    snapshot_path: Path,
    output_dir: Path,
    *,
    market_data_root: Path | None = None,
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
        result = _simulate(
            config=config,
            candidates=candidates,
            identity=identity,
            snapshot_normalized_sha256=snapshot_view.digest,
            rows=snapshot_view.rows,
        )
        write_outputs(result, output_dir)
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
        identity=identity,
    )
    return {"complete": 0, "failed": 1, "evidence_insufficient": 2}[result.status]


if __name__ == "__main__":
    raise SystemExit(main())
