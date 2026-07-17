from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Mapping

import duckdb
import pyarrow.parquet as pq

from scripts.research.analysis_data.derived import register_return_views
from scripts.research.analysis_data.manifest import (
    CORE_DATASETS,
    LOCAL_PHYSICAL_DATASETS,
    AnalysisManifestError,
    AnalysisSource,
    open_analysis_source,
)


_SCHEMAS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "results": (
        ("benchmark_returns", "DOUBLE"),
        ("returns", "DOUBLE"),
        ("time", "VARCHAR"),
    ),
    "balances": (
        ("total_value", "DOUBLE"),
        ("net_value", "DOUBLE"),
        ("cash", "DOUBLE"),
        ("aval_cash", "DOUBLE"),
        ("time", "VARCHAR"),
    ),
    "positions": (
        ("pindex", "BIGINT"),
        ("avg_cost", "DOUBLE"),
        ("margin", "DOUBLE"),
        ("amount", "DOUBLE"),
        ("today_amount", "BIGINT"),
        ("hold_cost", "DOUBLE"),
        ("side", "VARCHAR"),
        ("price", "DOUBLE"),
        ("gains", "DOUBLE"),
        ("daily_gains", "DOUBLE"),
        ("closeable_amount", "BIGINT"),
        ("time", "VARCHAR"),
        ("security_name", "VARCHAR"),
        ("security", "VARCHAR"),
    ),
    "orders": (
        ("match_time", "VARCHAR"),
        ("pindex", "BIGINT"),
        ("cancel_time", "VARCHAR"),
        ("action", "VARCHAR"),
        ("limit_price", "DOUBLE"),
        ("comment", "VARCHAR"),
        ("entrust_time", "VARCHAR"),
        ("finish_time", "VARCHAR"),
        ("side", "VARCHAR"),
        ("price", "DOUBLE"),
        ("commission", "DOUBLE"),
        ("gains", "DOUBLE"),
        ("type", "VARCHAR"),
        ("time", "VARCHAR"),
        ("security_name", "VARCHAR"),
        ("security", "VARCHAR"),
        ("filled", "BIGINT"),
        ("amount", "BIGINT"),
        ("status", "VARCHAR"),
    ),
    "risk": (
        ("__version", "BIGINT"),
        ("algorithm_return", "DOUBLE"),
        ("algorithm_volatility", "DOUBLE"),
        ("alpha", "DOUBLE"),
        ("annual_algo_return", "DOUBLE"),
        ("annual_bm_return", "DOUBLE"),
        ("avg_excess_return", "DOUBLE"),
        ("avg_position_days", "DOUBLE"),
        ("avg_trade_return", "DOUBLE"),
        ("benchmark_return", "DOUBLE"),
        ("benchmark_volatility", "DOUBLE"),
        ("beta", "DOUBLE"),
        ("day_win_ratio", "DOUBLE"),
        ("excess_return", "DOUBLE"),
        ("excess_return_max_drawdown", "DOUBLE"),
        ("excess_return_max_drawdown_period", "VARCHAR"),
        ("excess_return_sharpe", "DOUBLE"),
        ("information", "DOUBLE"),
        ("lose_count", "BIGINT"),
        ("max_drawdown", "DOUBLE"),
        ("max_drawdown_period", "VARCHAR"),
        ("max_leverage", "DOUBLE"),
        ("period_label", "VARCHAR"),
        ("profit_loss_ratio", "DOUBLE"),
        ("sharpe", "DOUBLE"),
        ("sortino", "DOUBLE"),
        ("trading_days", "BIGINT"),
        ("treasury_return", "DOUBLE"),
        ("turnover_rate", "DOUBLE"),
        ("win_count", "BIGINT"),
        ("win_ratio", "DOUBLE"),
    ),
    "period_risks": (("metric", "VARCHAR"), ("payload_json", "VARCHAR")),
}


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _empty_query(schema: tuple[tuple[str, str], ...]) -> str:
    columns = ", ".join(
        f"cast(null as {kind}) as {_quote_identifier(name)}" for name, kind in schema
    )
    return f"select {columns} where false"


def _parquet_query(path: Path, schema: tuple[tuple[str, str], ...]) -> str:
    source = f"read_parquet({_quote_literal(path.as_posix())})"
    columns = ", ".join(
        f"cast({_quote_identifier(name)} as {kind}) as {_quote_identifier(name)}"
        for name, kind in schema
    )
    return f"select {columns} from {source}"


def _declared_parquet_path(source: AnalysisSource, name: str) -> Path | None:
    entry = source.manifest["datasets"][name]
    files = entry["files"]
    for reference in files:
        if reference.get("path") == f"data/{name}.parquet":
            return source.root / str(reference["path"])
    return None


def _validate_physical_fields(
    source: AnalysisSource, name: str, path: Path
) -> None:
    actual = tuple(pq.read_schema(path).names)
    expected = tuple(field for field, _ in _SCHEMAS[name])
    fields_match = (
        actual == expected
        if source.kind in {"local_backtest", "local_research"}
        else len(actual) == len(expected) and set(actual) == set(expected)
    )
    if not fields_match:
        raise AnalysisManifestError(
            f"{source.kind} {name} fields do not match the observed contract"
        )
    if source.kind in {"local_backtest", "local_research"} and name == "results":
        schema = pq.read_schema(path)
        if (
            str(schema.field("benchmark_returns").type) != "double"
            or str(schema.field("returns").type) != "double"
            or str(schema.field("time").type) != "string"
        ):
            raise AnalysisManifestError("local results physical types are invalid")
        present = pq.read_table(path, columns=["benchmark_returns"])[
            "benchmark_returns"
        ].null_count
        expected_nulls = int(source.manifest["datasets"]["results"]["rows"])
        if present != expected_nulls:
            raise AnalysisManifestError(
                "local results.benchmark_returns must be entirely null"
            )


@dataclass
class AnalysisDatabase:
    source: AnalysisSource
    connection: duckdb.DuckDBPyConnection

    @property
    def table_names(self) -> tuple[str, ...]:
        if self.source.kind == "local_research":
            return LOCAL_PHYSICAL_DATASETS
        return CORE_DATASETS

    def reference_status(self, name: str) -> tuple[str, str | None]:
        if name not in self.table_names:
            raise KeyError(name)
        entry = self.source.manifest["datasets"][name]
        return str(entry["status"]), (
            None if "reason" not in entry else str(entry["reason"])
        )

    def extension(self, name: str) -> duckdb.DuckDBPyRelation:
        if self.source.kind != "local_research":
            raise KeyError(name)
        extensions = self.source.manifest["extensions"]
        if not isinstance(extensions, Mapping) or name not in extensions:
            raise KeyError(name)
        entry = extensions[name]
        if not isinstance(entry, Mapping):
            raise AnalysisManifestError(f"extension {name} declaration is invalid")
        files = entry.get("files")
        if not isinstance(files, list) or len(files) != 1:
            raise AnalysisManifestError(f"extension {name} file is missing")
        reference = files[0]
        if not isinstance(reference, Mapping) or not isinstance(reference.get("path"), str):
            raise AnalysisManifestError(f"extension {name} file is invalid")
        path = self.source.root / str(reference["path"])
        return self.connection.sql(
            f"select * from read_parquet({_quote_literal(path.as_posix())})"
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> AnalysisDatabase:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def open_analysis_database(result_dir: Path) -> AnalysisDatabase:
    source = open_analysis_source(result_dir)
    connection = duckdb.connect(":memory:")
    try:
        names = (
            LOCAL_PHYSICAL_DATASETS
            if source.kind == "local_research"
            else CORE_DATASETS
        )
        for name in names:
            path = _declared_parquet_path(source, name)
            schema = _SCHEMAS[name]
            if path is None:
                query = _empty_query(schema)
            else:
                _validate_physical_fields(source, name, path)
                query = _parquet_query(path, schema)
            connection.execute(
                f"create view {_quote_identifier(name)} as {query}"
            )
        register_return_views(connection)
        return AnalysisDatabase(source=source, connection=connection)
    except Exception:
        connection.close()
        raise
