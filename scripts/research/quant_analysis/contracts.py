from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq


STANDARD_TABLES = (
    "equity",
    "returns",
    "trades",
    "orders",
    "positions",
    "risk",
    "events",
    "benchmarks",
)
BENCHMARK_IDS = (
    "csi300_total_return_cny",
    "nasdaq100_total_return_cny",
)
_SCHEMA_VERSION = "1"
_TOLERANCE = 1e-9


class AnalysisContractError(ValueError):
    """Raised when a standard analysis table or bundle is incomplete."""


@dataclass(frozen=True)
class _TableSpec:
    schema: pa.Schema
    primary_key: tuple[str, ...]
    units: Mapping[str, str]
    allow_empty: bool = False


def _field(name: str, data_type: pa.DataType, *, nullable: bool = False) -> pa.Field:
    return pa.field(name, data_type, nullable=nullable)


_SPECS: dict[str, _TableSpec] = {
    "equity": _TableSpec(
        pa.schema(
            [
                _field("date", pa.string()),
                _field("portfolio_id", pa.string()),
                _field("currency", pa.string()),
                _field("equity", pa.float64()),
                _field("cash", pa.float64()),
                _field("positions_value", pa.float64()),
                _field("daily_pnl", pa.float64()),
                _field("fees", pa.float64()),
            ]
        ),
        ("date", "portfolio_id"),
        {
            "equity": "CNY",
            "cash": "CNY",
            "positions_value": "CNY",
            "daily_pnl": "CNY",
            "fees": "CNY",
        },
    ),
    "returns": _TableSpec(
        pa.schema(
            [
                _field("date", pa.string()),
                _field("portfolio_id", pa.string()),
                _field("return", pa.float64()),
                _field("equity", pa.float64()),
                _field("cash_return_contribution", pa.float64()),
            ]
        ),
        ("date", "portfolio_id"),
        {
            "return": "decimal",
            "equity": "CNY",
            "cash_return_contribution": "decimal",
        },
    ),
    "trades": _TableSpec(
        pa.schema(
            [
                _field("trade_id", pa.string()),
                _field("entry_date", pa.string()),
                _field("exit_date", pa.string()),
                _field("security", pa.string()),
                _field("asset_group", pa.string()),
                _field("quantity", pa.float64()),
                _field("entry_price", pa.float64()),
                _field("exit_price", pa.float64()),
                _field("fees", pa.float64()),
                _field("pnl", pa.float64()),
                _field("return", pa.float64()),
                _field("entry_reason", pa.string()),
                _field("exit_reason", pa.string()),
            ]
        ),
        ("trade_id",),
        {
            "quantity": "shares",
            "entry_price": "CNY/share",
            "exit_price": "CNY/share",
            "fees": "CNY",
            "pnl": "CNY",
            "return": "decimal",
        },
        allow_empty=True,
    ),
    "orders": _TableSpec(
        pa.schema(
            [
                _field("order_id", pa.string()),
                _field("date", pa.string()),
                _field("security", pa.string()),
                _field("side", pa.string()),
                _field("requested_quantity", pa.float64()),
                _field("filled_quantity", pa.float64()),
                _field("fill_price", pa.float64(), nullable=True),
                _field("fee", pa.float64()),
                _field("status", pa.string()),
                _field("reason", pa.string()),
            ]
        ),
        ("order_id",),
        {
            "requested_quantity": "shares",
            "filled_quantity": "shares",
            "fill_price": "CNY/share",
            "fee": "CNY",
        },
        allow_empty=True,
    ),
    "positions": _TableSpec(
        pa.schema(
            [
                _field("date", pa.string()),
                _field("security", pa.string()),
                _field("asset_group", pa.string()),
                _field("quantity", pa.float64()),
                _field("close", pa.float64()),
                _field("market_value", pa.float64()),
                _field("weight", pa.float64()),
                _field("planned_loss", pa.float64()),
                _field("common_stop", pa.float64()),
                _field("signal_n", pa.float64()),
                _field("stop_failure_loss", pa.float64()),
                _field("attribution_reason", pa.string()),
                _field("pnl_contribution", pa.float64()),
                _field("return_contribution", pa.float64()),
            ]
        ),
        ("date", "security"),
        {
            "quantity": "shares",
            "close": "CNY/share",
            "market_value": "CNY",
            "weight": "decimal",
            "planned_loss": "CNY",
            "common_stop": "CNY/share",
            "signal_n": "CNY/share",
            "stop_failure_loss": "CNY",
            "pnl_contribution": "CNY",
            "return_contribution": "decimal",
        },
        allow_empty=True,
    ),
    "risk": _TableSpec(
        pa.schema(
            [
                _field("date", pa.string()),
                _field("portfolio_id", pa.string()),
                _field("equity", pa.float64()),
                _field("cash", pa.float64()),
                _field("invested_ratio", pa.float64()),
                _field("cash_ratio", pa.float64()),
                _field("planned_risk", pa.float64()),
                _field("portfolio_risk_usage", pa.float64()),
                _field("portfolio_volatility", pa.float64(), nullable=True),
                _field("target_volatility_usage", pa.float64(), nullable=True),
            ]
        ),
        ("date", "portfolio_id"),
        {
            "equity": "CNY",
            "cash": "CNY",
            "invested_ratio": "decimal",
            "cash_ratio": "decimal",
            "planned_risk": "CNY",
            "portfolio_risk_usage": "decimal",
            "portfolio_volatility": "annual_decimal",
            "target_volatility_usage": "decimal",
        },
    ),
    "events": _TableSpec(
        pa.schema(
            [
                _field("event_id", pa.string()),
                _field("date", pa.string()),
                _field("sequence", pa.int64()),
                _field("security", pa.string(), nullable=True),
                _field("event_type", pa.string()),
                _field("status", pa.string()),
                _field("reason", pa.string()),
            ]
        ),
        ("event_id",),
        {"sequence": "ordinal"},
        allow_empty=True,
    ),
    "benchmarks": _TableSpec(
        pa.schema(
            [
                _field("date", pa.string()),
                _field("benchmark_id", pa.string()),
                _field("currency", pa.string()),
                _field("total_return_index", pa.float64()),
                _field("return", pa.float64()),
                _field("source_id", pa.string()),
            ]
        ),
        ("date", "benchmark_id"),
        {"total_return_index": "CNY_total_return_index", "return": "decimal"},
    ),
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _content_digest(rows: Sequence[Mapping[str, object]]) -> str:
    return hashlib.sha256(_canonical_bytes([dict(row) for row in rows])).hexdigest()


def _metadata(name: str, spec: _TableSpec, digest: str) -> dict[bytes, bytes]:
    return {
        b"schema_version": _SCHEMA_VERSION.encode("ascii"),
        b"table_name": name.encode("utf-8"),
        b"primary_key": _canonical_bytes(list(spec.primary_key)),
        b"units": _canonical_bytes(dict(spec.units)),
        b"content_sha256": digest.encode("ascii"),
    }


def _validate_date(value: object, field: str) -> None:
    try:
        date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise AnalysisContractError(f"{field} must use YYYY-MM-DD") from exc


def _validate_rows(name: str, rows: Sequence[Mapping[str, object]]) -> None:
    spec = _SPECS[name]
    fields = tuple(spec.schema.names)
    if not rows and not spec.allow_empty:
        raise AnalysisContractError(f"{name} must contain rows")
    seen: set[tuple[object, ...]] = set()
    for row in rows:
        if set(row) != set(fields):
            raise AnalysisContractError(f"{name} fields do not match the contract")
        key = tuple(row[field] for field in spec.primary_key)
        if key in seen:
            raise AnalysisContractError(f"{name} duplicate primary key: {key}")
        seen.add(key)
        for field in fields:
            value = row[field]
            arrow_field = spec.schema.field(field)
            if value is None and not arrow_field.nullable:
                raise AnalysisContractError(f"{name}.{field} must not be null")
            if value is not None and pa.types.is_floating(arrow_field.type):
                try:
                    numeric = float(value)
                except (TypeError, ValueError) as exc:
                    raise AnalysisContractError(
                        f"{name}.{field} must be numeric"
                    ) from exc
                if not math.isfinite(numeric):
                    raise AnalysisContractError(f"{name}.{field} must be finite")
        for field in ("date", "entry_date", "exit_date"):
            if field in row:
                _validate_date(row[field], f"{name}.{field}")
    if name == "trades":
        for row in rows:
            if str(row["exit_date"]) < str(row["entry_date"]):
                raise AnalysisContractError("trades exit_date precedes entry_date")
            if float(row["quantity"]) <= 0:
                raise AnalysisContractError("trades quantity must be positive")
    if name == "orders":
        for row in rows:
            if float(row["filled_quantity"]) > float(row["requested_quantity"]):
                raise AnalysisContractError("orders filled quantity exceeds request")


def write_analysis_table(
    name: str,
    rows: Iterable[Mapping[str, object]],
    output_dir: Path,
) -> Path:
    if name not in _SPECS:
        raise AnalysisContractError(f"unsupported analysis table: {name}")
    materialized = [dict(row) for row in rows]
    spec = _SPECS[name]
    materialized.sort(key=lambda row: tuple(row[field] for field in spec.primary_key))
    _validate_rows(name, materialized)
    try:
        table = pa.Table.from_pylist(materialized, schema=spec.schema)
    except (pa.ArrowException, TypeError, ValueError) as exc:
        raise AnalysisContractError(f"{name} values do not match the schema") from exc
    normalized_rows = table.to_pylist()
    digest = _content_digest(normalized_rows)
    table = table.replace_schema_metadata(_metadata(name, spec, digest))
    target = Path(output_dir) / f"{name}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        target,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
    )
    return target


@dataclass(frozen=True)
class AnalysisBundle:
    path: Path
    tables: Mapping[str, tuple[Mapping[str, object], ...]]
    digest: str

    def __post_init__(self) -> None:
        frozen = {
            name: tuple(MappingProxyType(dict(row)) for row in rows)
            for name, rows in self.tables.items()
        }
        object.__setattr__(self, "tables", MappingProxyType(frozen))

    def rows(self, name: str) -> tuple[Mapping[str, object], ...]:
        try:
            return self.tables[name]
        except KeyError as exc:
            raise AnalysisContractError(f"unknown bundle table: {name}") from exc


def _read_table(name: str, path: Path) -> list[dict[str, object]]:
    spec = _SPECS[name]
    try:
        table = pq.read_table(path)
    except (OSError, pa.ArrowException) as exc:
        raise AnalysisContractError(f"invalid Parquet table: {name}") from exc
    if not table.schema.remove_metadata().equals(spec.schema):
        raise AnalysisContractError(f"{name} schema does not match the contract")
    rows = table.to_pylist()
    _validate_rows(name, rows)
    expected_metadata = _metadata(name, spec, _content_digest(rows))
    if table.schema.metadata != expected_metadata:
        raise AnalysisContractError(f"{name} metadata, units or digest mismatch")
    return rows


def read_analysis_table(
    name: str,
    path: Path,
) -> tuple[Mapping[str, object], ...]:
    if name not in _SPECS:
        raise AnalysisContractError(f"unsupported analysis table: {name}")
    return tuple(MappingProxyType(row) for row in _read_table(name, Path(path)))


def _close(left: object, right: object) -> bool:
    left_value = float(left)
    right_value = float(right)
    scale = max(1.0, abs(left_value), abs(right_value))
    return abs(left_value - right_value) <= _TOLERANCE * scale


def _validate_cross_table(rows: Mapping[str, Sequence[Mapping[str, object]]]) -> None:
    equity = {str(row["date"]): row for row in rows["equity"]}
    returns = {str(row["date"]): row for row in rows["returns"]}
    risk = {str(row["date"]): row for row in rows["risk"]}
    if set(equity) != set(returns) or set(equity) != set(risk):
        raise AnalysisContractError("equity, returns and risk dates do not align")
    positions_by_date: dict[str, list[Mapping[str, object]]] = {
        current_date: [] for current_date in equity
    }
    for row in rows["positions"]:
        current_date = str(row["date"])
        if current_date not in positions_by_date:
            raise AnalysisContractError("positions date is outside the equity series")
        positions_by_date[current_date].append(row)
    ordered_dates = sorted(equity)
    previous_equity: float | None = None
    for current_date in ordered_dates:
        equity_row = equity[current_date]
        return_row = returns[current_date]
        risk_row = risk[current_date]
        if not _close(
            float(equity_row["cash"]) + float(equity_row["positions_value"]),
            equity_row["equity"],
        ):
            raise AnalysisContractError("cash plus positions does not equal equity")
        positions_value = sum(
            float(row["market_value"]) for row in positions_by_date[current_date]
        )
        if not _close(positions_value, equity_row["positions_value"]):
            raise AnalysisContractError("positions do not reconcile to equity")
        if not _close(return_row["equity"], equity_row["equity"]):
            raise AnalysisContractError("returns equity does not reconcile")
        if not _close(risk_row["equity"], equity_row["equity"]) or not _close(
            risk_row["cash"], equity_row["cash"]
        ):
            raise AnalysisContractError("risk cash and equity do not reconcile")
        if not _close(
            float(risk_row["invested_ratio"]) + float(risk_row["cash_ratio"]), 1.0
        ):
            raise AnalysisContractError("risk invested and cash ratios do not sum to one")
        weights = sum(float(row["weight"]) for row in positions_by_date[current_date])
        if not _close(weights, risk_row["invested_ratio"]):
            raise AnalysisContractError("position weights do not reconcile to risk")
        contributions = sum(
            float(row["return_contribution"])
            for row in positions_by_date[current_date]
        ) + float(return_row["cash_return_contribution"])
        if not _close(contributions, return_row["return"]):
            raise AnalysisContractError("position attribution does not reconcile to return")
        attributed_pnl = sum(
            float(row["pnl_contribution"])
            for row in positions_by_date[current_date]
        )
        if not _close(attributed_pnl, equity_row["daily_pnl"]):
            raise AnalysisContractError("position PnL does not reconcile to equity PnL")
        expected_return = (
            0.0
            if previous_equity is None
            else float(equity_row["equity"]) / previous_equity - 1.0
        )
        if not _close(expected_return, return_row["return"]):
            raise AnalysisContractError("equity and daily return do not reconcile")
        previous_equity = float(equity_row["equity"])

    benchmark_rows: dict[str, list[Mapping[str, object]]] = {
        key: [] for key in BENCHMARK_IDS
    }
    for row in rows["benchmarks"]:
        benchmark_id = str(row["benchmark_id"])
        if benchmark_id not in benchmark_rows:
            raise AnalysisContractError(f"unsupported benchmark: {benchmark_id}")
        if row["currency"] != "CNY" or not str(row["source_id"]).strip():
            raise AnalysisContractError("benchmark currency and source are required")
        benchmark_rows[benchmark_id].append(row)
    expected_dates = set(ordered_dates)
    if any(
        {str(row["date"]) for row in items} != expected_dates
        for items in benchmark_rows.values()
    ):
        raise AnalysisContractError("benchmark dates do not align with strategy returns")
    for benchmark_id, items in benchmark_rows.items():
        ordered = sorted(items, key=lambda row: str(row["date"]))
        if len({str(row["source_id"]) for row in ordered}) != 1:
            raise AnalysisContractError(
                f"benchmark source changes within the series: {benchmark_id}"
            )
        previous_index: float | None = None
        for row in ordered:
            current_index = float(row["total_return_index"])
            if current_index <= 0:
                raise AnalysisContractError("benchmark total return index must be positive")
            if previous_index is not None and not _close(
                current_index,
                previous_index * (1.0 + float(row["return"])),
            ):
                raise AnalysisContractError(
                    f"benchmark total return index does not match return: {benchmark_id}"
                )
            previous_index = current_index


def validate_analysis_bundle(output_dir: Path) -> AnalysisBundle:
    root = Path(output_dir)
    missing = [name for name in STANDARD_TABLES if not (root / f"{name}.parquet").is_file()]
    if missing:
        raise AnalysisContractError("missing tables: " + ", ".join(missing))
    tables = {
        name: _read_table(name, root / f"{name}.parquet")
        for name in STANDARD_TABLES
    }
    _validate_cross_table(tables)
    bundle_digest = hashlib.sha256(
        _canonical_bytes(
            {
                name: _content_digest(tables[name])
                for name in STANDARD_TABLES
            }
        )
    ).hexdigest()
    return AnalysisBundle(path=root, tables=tables, digest=bundle_digest)
