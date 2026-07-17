from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field as dataclass_field
from datetime import date
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Mapping

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from scripts.research.local_quant_research.contracts import (
    ExecutionBundle,
    ResultExtension,
)
from scripts.research.local_quant_research.evidence import (
    EvidenceError,
    validate_extension_table,
)


PACKAGE_SCHEMA_VERSION = "local-research-package/2"
FORMULA_VERSION = "unified-strategy-analysis/1"
CORE_DATASETS = ("results", "balances", "positions", "orders")
FORBIDDEN_REPORT_PHRASES = ("推荐", "稳健性通过", "适合实盘", "实盘准入")
_INTEGRITY_CHECKS = (
    "schema",
    "digests",
    "unique_keys",
    "cross_table_reconciliation",
    "readback",
)

_EXTENSION_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_CONFIG_FILES = {"scenario.json", "project-run.json", "code-identity.json"}
_EVIDENCE_FILES = {
    "market-snapshot.json",
    "runtime-lock.json",
    "performance.json",
    "environment.json",
}
_RESULTS_SCHEMA = pa.schema(
    [
        pa.field("benchmark_returns", pa.float64()),
        pa.field("returns", pa.float64(), nullable=False),
        pa.field("time", pa.string(), nullable=False),
    ]
)
_BALANCES_SCHEMA = pa.schema(
    [
        pa.field("total_value", pa.float64(), nullable=False),
        pa.field("net_value", pa.float64(), nullable=False),
        pa.field("cash", pa.float64(), nullable=False),
        pa.field("aval_cash", pa.float64(), nullable=False),
        pa.field("time", pa.string(), nullable=False),
    ]
)
_POSITIONS_SCHEMA = pa.schema(
    [
        pa.field("pindex", pa.int64(), nullable=False),
        pa.field("avg_cost", pa.float64(), nullable=False),
        pa.field("margin", pa.float64(), nullable=False),
        pa.field("amount", pa.float64(), nullable=False),
        pa.field("today_amount", pa.int64(), nullable=False),
        pa.field("hold_cost", pa.float64(), nullable=False),
        pa.field("side", pa.string(), nullable=False),
        pa.field("price", pa.float64(), nullable=False),
        pa.field("gains", pa.float64(), nullable=False),
        pa.field("daily_gains", pa.float64(), nullable=False),
        pa.field("closeable_amount", pa.int64(), nullable=False),
        pa.field("time", pa.string(), nullable=False),
        pa.field("security_name", pa.string(), nullable=False),
        pa.field("security", pa.string(), nullable=False),
    ]
)
_ORDERS_SCHEMA = pa.schema(
    [
        pa.field("match_time", pa.string()),
        pa.field("pindex", pa.int64(), nullable=False),
        pa.field("cancel_time", pa.string()),
        pa.field("action", pa.string(), nullable=False),
        pa.field("limit_price", pa.float64(), nullable=False),
        pa.field("comment", pa.string(), nullable=False),
        pa.field("entrust_time", pa.string(), nullable=False),
        pa.field("finish_time", pa.string()),
        pa.field("side", pa.string(), nullable=False),
        pa.field("price", pa.float64(), nullable=False),
        pa.field("commission", pa.float64(), nullable=False),
        pa.field("gains", pa.float64(), nullable=False),
        pa.field("type", pa.string(), nullable=False),
        pa.field("time", pa.string(), nullable=False),
        pa.field("security_name", pa.string(), nullable=False),
        pa.field("security", pa.string(), nullable=False),
        pa.field("filled", pa.int64(), nullable=False),
        pa.field("amount", pa.int64(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
    ]
)
_SCHEMAS = {
    "results": _RESULTS_SCHEMA,
    "balances": _BALANCES_SCHEMA,
    "positions": _POSITIONS_SCHEMA,
    "orders": _ORDERS_SCHEMA,
}
_UNIQUE_KEYS = {
    "results": ("time",),
    "balances": ("time",),
    "positions": ("time", "pindex", "security", "side"),
    "orders": ("time", "pindex", "security"),
}


class ResultContractError(ValueError):
    """Raised when a result package cannot prove the shared contract."""


@dataclass(frozen=True, slots=True)
class ResultPackageRequest:
    strategy_id: str
    scenario_id: str
    run_id: str
    output_dir: Path
    execution: ExecutionBundle
    extensions: tuple[ResultExtension, ...]
    code_files: Mapping[str, Path]
    config_documents: Mapping[str, object]
    evidence_documents: Mapping[str, object]
    performance_finalizer: Callable[
        [Mapping[str, float], Mapping[str, float]], Mapping[str, object]
    ] | None = None
    atomic_publish: bool = True


@dataclass(frozen=True, slots=True)
class ResultPackage:
    path: Path
    manifest: Mapping[str, object]
    package_sha256: str
    writer_stages: Mapping[str, float] = dataclass_field(default_factory=dict)
    writer_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class _CoreFacts:
    results: pa.Table
    balances: pa.Table
    positions: pa.Table
    orders: pa.Table

    def tables(self) -> dict[str, pa.Table]:
        return {name: getattr(self, name) for name in CORE_DATASETS}


@dataclass(frozen=True, slots=True)
class _FrozenInputs:
    code: Mapping[str, bytes]
    config: Mapping[str, object]
    evidence: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _ReportFacts:
    parameters: object
    time_range: Mapping[str, str | None]
    dataset_rows: Mapping[str, int]
    extension_rows: Mapping[str, int]
    orders: Mapping[str, int | float]
    positions: Mapping[str, object]
    net_value: Mapping[str, float]


def _with_writer_stages(
    inputs: _FrozenInputs,
    stages: Mapping[str, float],
    finalizer: Callable[
        [Mapping[str, float], Mapping[str, float]], Mapping[str, object]
    ] | None = None,
    *,
    writer_measurement: Mapping[str, float] | None = None,
) -> _FrozenInputs:
    evidence = dict(inputs.evidence)
    performance = evidence.get("performance.json")
    if not isinstance(performance, Mapping):
        raise ResultContractError("performance evidence must be an object")
    measurement = {
        "prefinalization_seconds": 0.0,
        **({} if writer_measurement is None else writer_measurement),
    }
    performance_document = (
        dict(finalizer(stages, measurement))
        if finalizer is not None
        else dict(performance)
    )
    existing_stages = performance_document.get("stages")
    stage_document = dict(existing_stages) if isinstance(existing_stages, Mapping) else {}
    stage_document.update({name: float(seconds) for name, seconds in stages.items()})
    performance_document["stages"] = stage_document
    performance_document["writer"] = {
        "prefinalization_seconds": float(measurement["prefinalization_seconds"])
    }
    performance_document["measurement_scope"] = {
        "actual_gate_basis": "returned_writer_seconds_through_writer_return",
        "persisted_writer_measurement": "writer_start_through_prefinalization_before_final_evidence_report_manifest_write",
        "persisted_measurement_excludes": "final_evidence_report_manifest_write_and_atomic_publish",
    }
    evidence["performance.json"] = performance_document
    return _FrozenInputs(inputs.code, inputs.config, evidence)


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ResultContractError("document keys must be strings")
            result[key] = _jsonable(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.generic):
        return _jsonable(value.item())
    if isinstance(value, Path):
        return value.as_posix()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not np.isfinite(value):
            raise ResultContractError("documents must contain finite numbers")
        return value
    raise ResultContractError(f"document value is not JSON-compatible: {type(value).__name__}")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_physical_zstd(path: Path, field: str) -> None:
    try:
        parquet = pq.ParquetFile(path)
        compression = {
            parquet.metadata.row_group(group).column(column).compression
            for group in range(parquet.metadata.num_row_groups)
            for column in range(parquet.metadata.num_columns)
        }
    except Exception as exc:
        raise ResultContractError(f"{field} Parquet metadata is invalid") from exc
    if compression and compression != {"ZSTD"}:
        raise ResultContractError(f"{field} physical compression is invalid")


def _safe_segment(value: str, *, field: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or value.endswith((" ", "."))
        or any(ord(character) < 32 or character in '<>:"/\\|?*' for character in value)
        or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise ResultContractError(f"{field} is unsafe")
    return value


def _safe_relative(value: str, *, suffix: str | None = None) -> Path:
    if not isinstance(value, str) or not value:
        raise ResultContractError("package file name is required")
    normalized = value.replace("\\", "/")
    windows = PureWindowsPath(value)
    posix = PurePosixPath(normalized)
    if windows.anchor or windows.drive or posix.is_absolute():
        raise ResultContractError("package file name is unsafe")
    raw_parts = normalized.split("/")
    if any(not part for part in raw_parts):
        raise ResultContractError("package file name is unsafe")
    parts = [
        _safe_segment(part, field="package file name")
        for part in raw_parts
    ]
    candidate = Path(*parts)
    if suffix is not None and candidate.suffix != suffix:
        candidate = candidate.with_name(candidate.name + suffix)
    return candidate


def _freeze_request_inputs(request: ResultPackageRequest) -> _FrozenInputs:
    _safe_segment(request.run_id, field="run_id")
    if not request.code_files:
        raise ResultContractError("archive-ready package requires strategy source code")

    code: dict[str, bytes] = {}
    documents: dict[str, dict[str, object]] = {"config": {}, "evidence": {}}
    for field, values, suffix in (
        ("code", request.code_files, None),
        ("config", request.config_documents, ".json"),
        ("evidence", request.evidence_documents, ".json"),
    ):
        destinations: set[str] = set()
        for name, value in values.items():
            relative = _safe_relative(name)
            if relative.as_posix() != name or (
                suffix is not None and relative.suffix != suffix
            ):
                raise ResultContractError(f"{field} package file identity is invalid")
            identity = relative.as_posix().casefold()
            if identity in destinations:
                raise ResultContractError(f"{field} package destinations must be unique")
            destinations.add(identity)
            if field == "code":
                source = Path(value)
                if not source.is_file():
                    raise ResultContractError(f"code file is missing: {name}")
                try:
                    code[name] = source.read_bytes()
                except OSError as exc:
                    raise ResultContractError(f"code file is unreadable: {name}") from exc
            else:
                documents[field][name] = _jsonable(value)
        required = _CONFIG_FILES if field == "config" else _EVIDENCE_FILES
        if field != "code" and not required.issubset(destinations):
            raise ResultContractError(f"archive-ready package is missing {field} evidence")
    return _FrozenInputs(
        code=code,
        config=documents["config"],
        evidence=documents["evidence"],
    )


def _python_scalar(value: object) -> object:
    if isinstance(value, np.generic):
        return value.item()
    return value


def _structured_array(
    value: object,
    *,
    required: tuple[str, ...],
    field: str,
    exact: bool,
) -> np.ndarray:
    array = np.asarray(value)
    names = array.dtype.names
    if array.ndim != 1 or names is None:
        raise ResultContractError(f"ledger.{field} must be a one-dimensional structured array")
    if not set(required).issubset(names) or (exact and set(names) != set(required)):
        raise ResultContractError(f"ledger.{field} fields do not match the contract")
    return array


def _record_rows(array: np.ndarray, schema: pa.Schema) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for record in array:
        row = {name: _python_scalar(record[name]) for name in schema.names}
        rows.append(row)
    return rows


def _table(rows: list[dict[str, object]], schema: pa.Schema) -> pa.Table:
    try:
        return pa.Table.from_pylist(rows, schema=schema)
    except (TypeError, ValueError) as exc:
        raise ResultContractError("ledger values do not match the Arrow contract") from exc


def _materialize_core(execution: ExecutionBundle) -> _CoreFacts:
    ledger = execution.final.ledger
    orders_value = ledger.orders
    assets_value = ledger.assets
    cash_value = ledger.cash
    value_value = ledger.value

    orders = _structured_array(
        orders_value,
        required=tuple(_ORDERS_SCHEMA.names),
        field="orders",
        exact=True,
    )
    assets = _structured_array(
        assets_value,
        required=tuple(_POSITIONS_SCHEMA.names),
        field="assets",
        exact=True,
    )
    cash = _structured_array(
        cash_value,
        required=("time", "cash"),
        field="cash",
        exact=True,
    )
    values = _structured_array(
        value_value,
        required=("time", "total_value", "returns", "benchmark_returns"),
        field="value",
        exact=True,
    )
    if len(cash) != len(values):
        raise ResultContractError("ledger cash and value rows do not reconcile")

    result_rows: list[dict[str, object]] = []
    balance_rows: list[dict[str, object]] = []
    for value_record, cash_record in zip(values, cash, strict=True):
        value_time = str(_python_scalar(value_record["time"]))
        cash_time = str(_python_scalar(cash_record["time"]))
        if value_time != cash_time:
            raise ResultContractError("ledger cash and value times do not reconcile")
        benchmark = float(value_record["benchmark_returns"])
        result_rows.append(
            {
                "benchmark_returns": None if np.isnan(benchmark) else benchmark,
                "returns": float(value_record["returns"]),
                "time": value_time,
            }
        )
        total_value = float(value_record["total_value"])
        cash_amount = float(cash_record["cash"])
        balance_rows.append(
            {
                "total_value": total_value,
                "net_value": total_value,
                "cash": cash_amount,
                "aval_cash": cash_amount,
                "time": value_time,
            }
        )

    facts = _CoreFacts(
        results=_table(result_rows, _RESULTS_SCHEMA),
        balances=_table(balance_rows, _BALANCES_SCHEMA),
        positions=_table(_record_rows(assets, _POSITIONS_SCHEMA), _POSITIONS_SCHEMA),
        orders=_table(_record_rows(orders, _ORDERS_SCHEMA), _ORDERS_SCHEMA),
    )
    return facts


def _validate_unique_key(
    table: pa.Table,
    key: tuple[str, ...],
    field: str,
    *,
    rows: list[dict[str, object]] | None = None,
) -> None:
    if not key or any(name not in table.schema.names for name in key):
        raise ResultContractError(f"{field} unique key is invalid")
    values = (
        [tuple(row[name] for name in key) for row in rows]
        if rows is not None
        else list(zip(*(table[name].to_pylist() for name in key), strict=True))
    )
    if any(any(item is None for item in row) for row in values):
        raise ResultContractError(f"{field} unique key contains null")
    if len(values) != len(set(values)):
        raise ResultContractError(f"{field} unique key is not unique")


def _validate_common_facts(
    facts: _CoreFacts,
    summaries: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, dict[str, object]]:
    tables = facts.tables()
    materialized_summaries = (
        {name: _table_summary(table) for name, table in tables.items()}
        if summaries is None
        else {name: dict(summary) for name, summary in summaries.items()}
    )
    for name, schema in _SCHEMAS.items():
        if tables[name].schema != schema:
            raise ResultContractError(f"{name} fields do not match the contract")
        rows = materialized_summaries[name]["rows"]
        if not isinstance(rows, list):
            raise ResultContractError(f"{name} logical rows are invalid")
        _validate_unique_key(tables[name], _UNIQUE_KEYS[name], name, rows=rows)
    if facts.results.num_rows != facts.balances.num_rows:
        raise ResultContractError("results and balances do not reconcile")
    if facts.results["benchmark_returns"].null_count != facts.results.num_rows:
        raise ResultContractError("source benchmark returns must remain null")

    result_rows = materialized_summaries["results"]["rows"]
    balance_rows = materialized_summaries["balances"]["rows"]
    position_rows = materialized_summaries["positions"]["rows"]
    order_rows = materialized_summaries["orders"]["rows"]
    if not all(
        isinstance(rows, list)
        for rows in (result_rows, balance_rows, position_rows, order_rows)
    ):
        raise ResultContractError("core logical rows are invalid")
    result_times = [str(item["time"]) for item in result_rows]
    if result_times != [str(item["time"]) for item in balance_rows]:
        raise ResultContractError("results and balances times do not reconcile")
    initial_cash: list[float] = []
    for result, balance in zip(result_rows, balance_rows, strict=True):
        denominator = 1.0 + float(result["returns"])
        if denominator <= 0.0:
            raise ResultContractError("cumulative return is invalid")
        implied = float(balance["total_value"]) / denominator
        if not np.isfinite(implied):
            raise ResultContractError("implied initial cash is invalid")
        initial_cash.append(implied)
    if initial_cash and max(initial_cash) - min(initial_cash) > 0.01:
        raise ResultContractError("returns do not use one configured initial cash value")

    result_time_set = set(result_times)
    position_value_by_time: dict[str, float] = {}
    for item in position_rows:
        time_text = str(item["time"])
        if time_text not in result_time_set:
            raise ResultContractError("position time is absent from results")
        position_value_by_time[time_text] = position_value_by_time.get(
            time_text, 0.0
        ) + float(item["amount"]) * float(item["price"])
    for balance in balance_rows:
        time_text = str(balance["time"])
        reconciled = float(balance["cash"]) + position_value_by_time.get(time_text, 0.0)
        if abs(float(balance["total_value"]) - reconciled) > 0.02:
            raise ResultContractError("balance does not reconcile with cash and positions")

    result_dates = {time_text[:10] for time_text in result_times}
    for item in order_rows:
        if str(item["time"])[:10] not in result_dates:
            raise ResultContractError("order date is absent from results")
        if not 0 <= int(item["filled"]) <= int(item["amount"]):
            raise ResultContractError("order filled amount is invalid")
    return materialized_summaries


def _validate_extension_contracts(
    extensions: tuple[ResultExtension, ...],
) -> dict[str, ResultExtension]:
    result: dict[str, ResultExtension] = {}
    for extension in extensions:
        if _EXTENSION_NAME.fullmatch(extension.name) is None:
            raise ResultContractError("extension name is invalid")
        if extension.name in result:
            raise ResultContractError("extension names must be unique")
        if not isinstance(extension.schema_version, str) or not extension.schema_version:
            raise ResultContractError("extension schema_version is required")
        try:
            validate_extension_table(extension.table)
        except EvidenceError as exc:
            raise ResultContractError(str(exc)) from exc
        _validate_unique_key(extension.table, extension.unique_key, extension.name)
        _jsonable(extension.evidence)
        result[extension.name] = extension
    return result


def _validate_extensions(
    extensions: tuple[ResultExtension, ...],
) -> tuple[dict[str, ResultExtension], dict[str, dict[str, object]]]:
    result = _validate_extension_contracts(extensions)
    summaries: dict[str, dict[str, object]] = {}
    for extension in result.values():
        summary = _table_summary(extension.table)
        rows = summary["rows"]
        if not isinstance(rows, list):
            raise ResultContractError("extension logical rows are invalid")
        _validate_unique_key(
            extension.table,
            extension.unique_key,
            extension.name,
            rows=rows,
        )
        summaries[extension.name] = summary
    return result, summaries


def _schema_document(schema: pa.Schema) -> list[dict[str, object]]:
    return [
        {"name": field.name, "type": str(field.type), "nullable": field.nullable}
        for field in schema
    ]


def _table_summary(table: pa.Table) -> dict[str, object]:
    return {"schema": _schema_document(table.schema), "rows": table.to_pylist()}


def _content_document(
    request: ResultPackageRequest,
    inputs: _FrozenInputs,
    extensions: Mapping[str, ResultExtension],
    core_summaries: Mapping[str, Mapping[str, object]],
    extension_summaries: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    code = {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in sorted(inputs.code.items())
    }
    return {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "object": {
            "kind": "local_research",
            "status": "complete",
            "strategy_id": request.strategy_id,
            "scenario_id": request.scenario_id,
            "run_id": request.run_id,
        },
        "authority": "local_research",
        "backend": "vectorbt",
        "formula_version": FORMULA_VERSION,
        "code": code,
        "config": dict(inputs.config),
        "evidence": dict(inputs.evidence),
        "datasets": {
            name: dict(summary) for name, summary in core_summaries.items()
        },
        "extensions": {
            name: {
                "schema_version": extension.schema_version,
                "unique_key": list(extension.unique_key),
                "evidence": dict(extension.evidence),
                "table": dict(extension_summaries[name]),
            }
            for name, extension in sorted(extensions.items())
        },
    }


def _time_range(table: pa.Table) -> dict[str, str | None]:
    if table.num_rows == 0 or "time" not in table.schema.names:
        return {"start": None, "end": None}
    values = [str(value)[:10] for value in table["time"].to_pylist()]
    try:
        for value in values:
            date.fromisoformat(value)
    except ValueError as exc:
        raise ResultContractError("table time must start with YYYY-MM-DD") from exc
    return {"start": min(values), "end": max(values)}


def _file_ref(
    root: Path,
    path: Path,
    *,
    rows: int | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
    }
    if rows is not None:
        result.update({"rows": rows, "format": "parquet", "compression": "zstd"})
    return result


def _table_entry(
    root: Path,
    path: Path,
    table: pa.Table,
    unique_key: tuple[str, ...],
) -> dict[str, object]:
    return {
        "required": True,
        "status": "complete",
        "rows": table.num_rows,
        "verified_empty": table.num_rows == 0,
        "time_range": _time_range(table),
        "schema": _schema_document(table.schema),
        "files": [_file_ref(root, path, rows=table.num_rows)],
        "evidence": {"fields": table.schema.names, "unique_key": list(unique_key)},
    }


def _write_code_files(root: Path, values: Mapping[str, bytes]) -> dict[str, dict[str, object]]:
    references: dict[str, dict[str, object]] = {}
    destinations: set[Path] = set()
    for name, payload in sorted(values.items()):
        relative = _safe_relative(name)
        destination = root / "code" / relative
        if destination in destinations:
            raise ResultContractError("code file destinations must be unique")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        references[name] = _file_ref(root, destination)
        destinations.add(destination)
    return references


def _write_documents(
    root: Path,
    directory: str,
    values: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    references: dict[str, dict[str, object]] = {}
    destinations: set[Path] = set()
    for name, document in sorted(values.items()):
        relative = _safe_relative(name, suffix=".json")
        destination = root / directory / relative
        if destination in destinations:
            raise ResultContractError(f"{directory} document destinations must be unique")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_json_bytes(document))
        references[name] = _file_ref(root, destination)
        destinations.add(destination)
    return references


def _column_sum(table: pa.Table, name: str) -> int | float:
    if table.num_rows == 0:
        return 0
    value = pc.sum(table[name]).as_py()
    return 0 if value is None else value


def _build_report_facts(
    *,
    config: Mapping[str, object],
    facts: _CoreFacts,
    extension_tables: Mapping[str, pa.Table],
) -> _ReportFacts:
    scenario = config.get("scenario.json")
    parameters = (
        scenario.get("parameters", {})
        if isinstance(scenario, Mapping)
        else {}
    )
    parameters = _jsonable(parameters)
    time_range = _time_range(facts.results)

    position_times = [str(value) for value in facts.positions["time"].to_pylist()]
    latest_position_time = max(position_times) if position_times else None
    if latest_position_time is None:
        latest_positions = facts.positions.slice(0, 0)
    else:
        latest_positions = facts.positions.filter(
            pc.equal(facts.positions["time"], latest_position_time)
        )
    latest_market_value = float(
        _column_sum(
            pa.table(
                {
                    "market_value": pc.multiply(
                        latest_positions["amount"], latest_positions["price"]
                    )
                }
            ),
            "market_value",
        )
    )

    balance_times = [str(value) for value in facts.balances["time"].to_pylist()]
    if balance_times:
        start_index = min(range(len(balance_times)), key=balance_times.__getitem__)
        end_index = max(range(len(balance_times)), key=balance_times.__getitem__)
        start_total_value = float(facts.balances["total_value"][start_index].as_py())
        end_total_value = float(facts.balances["total_value"][end_index].as_py())
        start_net_value = float(facts.balances["net_value"][start_index].as_py())
        end_net_value = float(facts.balances["net_value"][end_index].as_py())
        cumulative_return = float(facts.results["returns"][end_index].as_py())
    else:
        start_total_value = end_total_value = 0.0
        start_net_value = end_net_value = 0.0
        cumulative_return = 0.0
    return _ReportFacts(
        parameters=parameters,
        time_range=time_range,
        dataset_rows={name: table.num_rows for name, table in facts.tables().items()},
        extension_rows={
            name: table.num_rows for name, table in sorted(extension_tables.items())
        },
        orders={
            "records": facts.orders.num_rows,
            "requested_amount": int(_column_sum(facts.orders, "amount")),
            "filled_amount": int(_column_sum(facts.orders, "filled")),
            "commission": float(_column_sum(facts.orders, "commission")),
        },
        positions={
            "records": facts.positions.num_rows,
            "latest_time": latest_position_time,
            "latest_records": latest_positions.num_rows,
            "latest_market_value": latest_market_value,
        },
        net_value={
            "start_total_value": start_total_value,
            "end_total_value": end_total_value,
            "start_net_value": start_net_value,
            "end_net_value": end_net_value,
            "cumulative_return": cumulative_return,
        },
    )


def _report_payloads(
    *,
    identity: Mapping[str, object],
    evidence: Mapping[str, object],
    report_facts: _ReportFacts,
    package_sha256: str,
) -> tuple[bytes, bytes]:
    parameters = report_facts.parameters
    performance = _jsonable(evidence.get("performance.json"))
    time_range = report_facts.time_range
    orders = report_facts.orders
    positions = report_facts.positions
    net_value = report_facts.net_value

    integrity_gate = {
        "status": "pass",
        "exceptions": [],
        "checks": list(_INTEGRITY_CHECKS),
    }
    metrics: dict[str, object] = {
        "identity": dict(identity),
        "package_sha256": package_sha256,
        "parameters": parameters,
        "time_range": time_range,
        "datasets": dict(report_facts.dataset_rows),
        "extensions": dict(report_facts.extension_rows),
        "orders": dict(orders),
        "positions": dict(positions),
        "net_value": dict(net_value),
        "performance": performance,
        "integrity_gate": integrity_gate,
    }
    parameters_text = _json_bytes(parameters).decode("utf-8").rstrip()
    performance_text = _json_bytes(performance).decode("utf-8").rstrip()
    lines = [
        "# 本地研究执行摘要",
        "",
        f"- 策略：`{identity['strategy_id']}`",
        f"- 场景：`{identity['scenario_id']}`",
        f"- 运行：`{identity['run_id']}`",
        f"- 内容摘要：`{package_sha256}`",
        "",
        "## 参数与配置",
        "",
        "```json",
        parameters_text,
        "```",
        "",
        "## 时间范围",
        "",
        f"- 开始：`{time_range['start']}`",
        f"- 结束：`{time_range['end']}`",
        "",
        "## 核心事实行数",
        "",
    ]
    lines.extend(
        f"- `{name}`：{rows}" for name, rows in report_facts.dataset_rows.items()
    )
    lines.extend(
        [
            "",
            "## 成交摘要",
            "",
            f"- 订单记录：{orders['records']}",
            f"- 委托数量：{orders['requested_amount']}",
            f"- 成交数量：{orders['filled_amount']}",
            f"- 佣金：{float(orders['commission']):.6f}",
            "",
            "## 持仓摘要",
            "",
            f"- 持仓记录：{positions['records']}",
            f"- 最新时点：`{positions['latest_time']}`",
            f"- 最新持仓数量：{positions['latest_records']}",
            f"- 最新持仓市值：{float(positions['latest_market_value']):.6f}",
            "",
            "## 净值摘要",
            "",
            f"- 起始总资产：{float(net_value['start_total_value']):.6f}",
            f"- 结束总资产：{float(net_value['end_total_value']):.6f}",
            f"- 起始净值：{float(net_value['start_net_value']):.6f}",
            f"- 结束净值：{float(net_value['end_net_value']):.6f}",
            f"- 累计收益：{float(net_value['cumulative_return']):.12f}",
            "",
            "## 性能",
            "",
            "```json",
            performance_text,
            "```",
            "",
            "## 完整性门禁",
            "",
            "- 状态：`pass`",
            "- 例外：无",
        ]
    )
    lines.extend(f"- 检查：`{check}`" for check in _INTEGRITY_CHECKS)
    lines.extend(["", "## 扩展行数", ""])
    if report_facts.extension_rows:
        lines.extend(
            f"- `{name}`：{rows}"
            for name, rows in report_facts.extension_rows.items()
        )
    else:
        lines.append("- 无")
    summary_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    return summary_bytes, _json_bytes(metrics)


def _write_report(
    root: Path,
    request: ResultPackageRequest,
    inputs: _FrozenInputs,
    report_facts: _ReportFacts,
    package_sha256: str,
) -> tuple[dict[str, dict[str, object]], dict[str, bytes]]:
    report_dir = root / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    metrics = report_dir / "metrics.json"
    summary_bytes, metrics_bytes = _report_payloads(
        identity={
            "kind": "local_research",
            "status": "complete",
            "strategy_id": request.strategy_id,
            "scenario_id": request.scenario_id,
            "run_id": request.run_id,
        },
        evidence=inputs.evidence,
        report_facts=report_facts,
        package_sha256=package_sha256,
    )
    if any(phrase.encode("utf-8") in summary_bytes for phrase in FORBIDDEN_REPORT_PHRASES):
        raise ResultContractError("execution report contains forbidden judgment")
    metrics.write_bytes(metrics_bytes)
    summary = report_dir / "execution-summary.md"
    summary.write_bytes(summary_bytes)
    return (
        {
            "execution-summary": _file_ref(root, summary),
            "metrics": _file_ref(root, metrics),
        },
        {"execution-summary": summary_bytes, "metrics": metrics_bytes},
    )


def _readback_tables(
    root: Path,
    facts: _CoreFacts,
    extensions: Mapping[str, ResultExtension],
) -> tuple[_CoreFacts, dict[str, pa.Table]]:
    try:
        for name in CORE_DATASETS:
            _validate_physical_zstd(
                root / "data" / f"{name}.parquet", name
            )
        for name in extensions:
            _validate_physical_zstd(
                root / "extensions" / name / "data.parquet", name
            )
        core = _CoreFacts(
            **{
                name: pq.read_table(root / "data" / f"{name}.parquet")
                for name in CORE_DATASETS
            }
        )
        extension_tables = {
            name: pq.read_table(root / "extensions" / name / "data.parquet")
            for name in extensions
        }
    except Exception as exc:
        raise ResultContractError("result package readback failed") from exc
    for name, expected in facts.tables().items():
        observed = core.tables()[name]
        if not observed.equals(expected, check_metadata=True):
            raise ResultContractError(f"{name} readback changed logical facts")
    for name, extension in extensions.items():
        observed = extension_tables[name]
        if not observed.equals(extension.table, check_metadata=True):
            raise ResultContractError(f"extension {name} readback changed logical facts")
    return core, extension_tables


def _manifest_table_entries(
    root: Path,
    facts: _CoreFacts,
    extensions: Mapping[str, ResultExtension],
    extension_tables: Mapping[str, pa.Table],
) -> tuple[dict[str, object], dict[str, object]]:
    datasets: dict[str, object] = {
        name: _table_entry(
            root,
            root / "data" / f"{name}.parquet",
            table,
            _UNIQUE_KEYS[name],
        )
        for name, table in facts.tables().items()
    }
    extension_entries: dict[str, object] = {
        name: {
            **_table_entry(
                root,
                root / "extensions" / name / "data.parquet",
                extension_tables[name],
                extension.unique_key,
            ),
            "schema_version": extension.schema_version,
            "strategy_evidence": _jsonable(extension.evidence),
        }
        for name, extension in sorted(extensions.items())
    }
    return datasets, extension_entries


def _manifest(
    request: ResultPackageRequest,
    package_sha256: str,
    code: Mapping[str, Mapping[str, object]],
    config: Mapping[str, Mapping[str, object]],
    evidence: Mapping[str, Mapping[str, object]],
    reports: Mapping[str, Mapping[str, object]],
    datasets: Mapping[str, object],
    extension_entries: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "object": {
            "kind": "local_research",
            "status": "complete",
            "strategy_id": request.strategy_id,
            "scenario_id": request.scenario_id,
            "run_id": request.run_id,
        },
        "authority": "local_research",
        "backend": "vectorbt",
        "formula_version": FORMULA_VERSION,
        "package_sha256": package_sha256,
        "code": dict(code),
        "config": dict(config),
        "evidence": dict(evidence),
        "datasets": dict(datasets),
        "extensions": dict(extension_entries),
        "reports": dict(reports),
        "gate": {
            "status": "pass",
            "exceptions": [],
            "checks": list(_INTEGRITY_CHECKS),
        },
    }


def _read_json(path: Path, field: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResultContractError(f"{field} is unreadable") from exc


def _resolve_declared(
    root: Path,
    reference: object,
    field: str,
    *,
    expected_path: str,
) -> Path:
    if not isinstance(reference, Mapping):
        raise ResultContractError(f"{field} reference is invalid")
    if set(reference) not in ({"path", "sha256", "bytes"}, {"path", "sha256", "bytes", "rows", "format", "compression"}):
        raise ResultContractError(f"{field} reference fields are invalid")
    relative = reference.get("path")
    digest = reference.get("sha256")
    size = reference.get("bytes")
    if not isinstance(relative, str) or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ResultContractError(f"{field} reference identity is invalid")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ResultContractError(f"{field} reference size is invalid")
    try:
        candidate = _safe_relative(relative)
    except ResultContractError as exc:
        raise ResultContractError(f"{field} reference path is unsafe") from exc
    if candidate.as_posix() != relative:
        raise ResultContractError(f"{field} reference path is unsafe")
    if relative != expected_path:
        raise ResultContractError(f"{field} file identity is invalid")
    path = (root / candidate).resolve()
    if root not in path.parents or not path.is_file():
        raise ResultContractError(f"{field} declared file is missing")
    if path.stat().st_size != size or _sha256_file(path) != digest:
        raise ResultContractError(f"{field} digest or size mismatch")
    return path


def _validate_table_entry(
    root: Path,
    name: str,
    entry_value: object,
    *,
    schema: pa.Schema | None,
    expected_path: str,
    extension: bool,
) -> tuple[pa.Table, tuple[str, ...], dict[str, object]]:
    if not isinstance(entry_value, Mapping):
        raise ResultContractError(f"{name} declaration is invalid")
    required = {
        "required",
        "status",
        "rows",
        "verified_empty",
        "time_range",
        "schema",
        "files",
        "evidence",
    }
    if extension:
        required |= {"schema_version", "strategy_evidence"}
    if set(entry_value) != required:
        raise ResultContractError(f"{name} declaration fields are invalid")
    rows = entry_value["rows"]
    if (
        entry_value["required"] is not True
        or entry_value["status"] != "complete"
        or isinstance(rows, bool)
        or not isinstance(rows, int)
        or rows < 0
        or entry_value["verified_empty"] is not (rows == 0)
    ):
        raise ResultContractError(f"{name} declaration status is invalid")
    files = entry_value["files"]
    if not isinstance(files, list) or len(files) != 1:
        raise ResultContractError(f"{name} must declare one Parquet file")
    reference = files[0]
    path = _resolve_declared(
        root,
        reference,
        name,
        expected_path=expected_path,
    )
    if (
        not isinstance(reference, Mapping)
        or reference.get("rows") != rows
        or reference.get("format") != "parquet"
        or reference.get("compression") != "zstd"
    ):
        raise ResultContractError(f"{name} Parquet declaration is invalid")
    _validate_physical_zstd(path, name)
    try:
        table = pq.read_table(path)
    except Exception as exc:
        raise ResultContractError(f"{name} Parquet readback failed") from exc
    if extension:
        try:
            validate_extension_table(table)
        except EvidenceError as exc:
            raise ResultContractError(str(exc)) from exc
    if table.num_rows != rows:
        raise ResultContractError(f"{name} row count mismatch")
    declared_schema = entry_value["schema"]
    if declared_schema != _schema_document(table.schema):
        raise ResultContractError(f"{name} schema declaration mismatch")
    if schema is not None and table.schema != schema:
        raise ResultContractError(f"{name} fields do not match the contract")
    evidence = entry_value["evidence"]
    if not isinstance(evidence, Mapping) or set(evidence) != {"fields", "unique_key"}:
        raise ResultContractError(f"{name} evidence is invalid")
    if evidence["fields"] != table.schema.names or not isinstance(evidence["unique_key"], list):
        raise ResultContractError(f"{name} evidence fields are invalid")
    unique_key = tuple(evidence["unique_key"])
    materialized_summary = _table_summary(table)
    logical_rows = materialized_summary.get("rows")
    if not isinstance(logical_rows, list):
        raise ResultContractError(f"{name} logical rows are invalid")
    _validate_unique_key(table, unique_key, name, rows=logical_rows)
    if entry_value["time_range"] != _time_range(table):
        raise ResultContractError(f"{name} time range mismatch")
    return table, unique_key, materialized_summary


def _validate_result_package_document(
    root: Path,
    document: object,
) -> Mapping[str, object]:
    if not isinstance(document, dict):
        raise ResultContractError("manifest must be an object")
    required = {
        "schema_version",
        "object",
        "authority",
        "backend",
        "formula_version",
        "package_sha256",
        "code",
        "config",
        "evidence",
        "datasets",
        "extensions",
        "reports",
        "gate",
    }
    if set(document) != required or document["schema_version"] != PACKAGE_SCHEMA_VERSION:
        raise ResultContractError("unsupported result package manifest")
    identity = document["object"]
    if (
        not isinstance(identity, Mapping)
        or set(identity) != {"kind", "status", "strategy_id", "scenario_id", "run_id"}
        or identity["kind"] != "local_research"
        or identity["status"] != "complete"
        or any(
            not isinstance(identity[name], str) or not identity[name].strip()
            for name in ("strategy_id", "scenario_id", "run_id")
        )
        or document["authority"] != "local_research"
        or document["backend"] != "vectorbt"
        or document["formula_version"] != FORMULA_VERSION
        or not isinstance(document["package_sha256"], str)
        or _SHA256.fullmatch(document["package_sha256"]) is None
    ):
        raise ResultContractError("result package identity is invalid")
    _safe_segment(str(identity["run_id"]), field="run_id")
    gate = document["gate"]
    if (
        not isinstance(gate, Mapping)
        or set(gate) != {"status", "exceptions", "checks"}
        or gate["status"] != "pass"
        or gate["exceptions"] != []
        or not isinstance(gate["checks"], list)
        or not gate["checks"]
    ):
        raise ResultContractError("result package gate did not pass")

    section_values: dict[str, Mapping[str, object]] = {}
    resolved_sections: dict[str, dict[str, Path]] = {}
    for section in ("code", "config", "evidence"):
        values = document[section]
        if not isinstance(values, Mapping) or any(
            not isinstance(name, str) for name in values
        ):
            raise ResultContractError(f"{section} declaration is invalid")
        if section == "code" and not values:
            raise ResultContractError("archive-ready package requires strategy source code")
        required_files = (
            _CONFIG_FILES
            if section == "config"
            else _EVIDENCE_FILES if section == "evidence" else set()
        )
        if not required_files.issubset(values):
            raise ResultContractError(
                f"archive-ready package is missing {section} evidence"
            )
        resolved: dict[str, Path] = {}
        for name, reference in values.items():
            relative = _safe_relative(name)
            if relative.as_posix() != name or (
                section in {"config", "evidence"} and relative.suffix != ".json"
            ):
                raise ResultContractError(f"{section} file identity is invalid")
            resolved[name] = _resolve_declared(
                root,
                reference,
                f"{section}.{name}",
                expected_path=f"{section}/{name}",
            )
        section_values[section] = values
        resolved_sections[section] = resolved

    reports = document["reports"]
    if not isinstance(reports, Mapping) or set(reports) != {
        "execution-summary",
        "metrics",
    }:
        raise ResultContractError("reports declaration is incomplete")
    report_paths = {
        "execution-summary": "report/execution-summary.md",
        "metrics": "report/metrics.json",
    }
    resolved_sections["reports"] = {
        name: _resolve_declared(
            root,
            reports[name],
            f"reports.{name}",
            expected_path=report_paths[name],
        )
        for name in report_paths
    }

    datasets = document["datasets"]
    if not isinstance(datasets, Mapping) or set(datasets) != set(CORE_DATASETS):
        raise ResultContractError("result package datasets are invalid")
    core_tables: dict[str, pa.Table] = {}
    core_summaries: dict[str, dict[str, object]] = {}
    for name in CORE_DATASETS:
        table, key, summary = _validate_table_entry(
            root,
            name,
            datasets[name],
            schema=_SCHEMAS[name],
            expected_path=f"data/{name}.parquet",
            extension=False,
        )
        if key != _UNIQUE_KEYS[name]:
            raise ResultContractError(f"{name} unique key does not match the contract")
        core_tables[name] = table
        core_summaries[name] = summary
    facts = _CoreFacts(**core_tables)
    core_summaries = _validate_common_facts(facts, core_summaries)

    extensions_value = document["extensions"]
    if not isinstance(extensions_value, Mapping):
        raise ResultContractError("extensions declaration is invalid")
    extension_content: dict[str, object] = {}
    extension_tables: dict[str, pa.Table] = {}
    for name, entry_value in extensions_value.items():
        if not isinstance(name, str) or _EXTENSION_NAME.fullmatch(name) is None:
            raise ResultContractError("extension name is invalid")
        if not isinstance(entry_value, Mapping):
            raise ResultContractError(f"extension {name} declaration is invalid")
        schema_version = entry_value.get("schema_version")
        if not isinstance(schema_version, str) or not schema_version:
            raise ResultContractError(f"extension {name} schema_version is invalid")
        strategy_evidence = entry_value.get("strategy_evidence")
        if not isinstance(strategy_evidence, Mapping):
            raise ResultContractError(
                f"extension {name} strategy_evidence must be an object"
            )
        table, key, summary = _validate_table_entry(
            root,
            name,
            entry_value,
            schema=None,
            expected_path=f"extensions/{name}/data.parquet",
            extension=True,
        )
        extension_content[name] = {
            "schema_version": schema_version,
            "unique_key": list(key),
            "evidence": _jsonable(strategy_evidence),
            "table": summary,
        }
        extension_tables[name] = table

    content: dict[str, object] = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "object": dict(identity),
        "authority": document["authority"],
        "backend": document["backend"],
        "formula_version": document["formula_version"],
        "datasets": core_summaries,
        "extensions": extension_content,
    }
    content["code"] = {
        name: hashlib.sha256(file.read_bytes()).hexdigest()
        for name, file in resolved_sections["code"].items()
    }
    for section in ("config", "evidence"):
        content[section] = {
            name: _read_json(file, f"{section}.{name}")
            for name, file in resolved_sections[section].items()
        }
    scenario_document = content["config"].get("scenario.json")
    if (
        not isinstance(scenario_document, Mapping)
        or scenario_document.get("scenario_id") != identity["scenario_id"]
    ):
        raise ResultContractError(
            "result package scenario identity differs from frozen configuration"
        )
    if _canonical_digest(content) != document["package_sha256"]:
        raise ResultContractError("result package logical digest mismatch")

    report_payloads = {
        name: path.read_bytes()
        for name, path in resolved_sections["reports"].items()
    }
    try:
        report_text = report_payloads["execution-summary"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResultContractError("execution report is unreadable") from exc
    if any(phrase in report_text for phrase in FORBIDDEN_REPORT_PHRASES):
        raise ResultContractError("execution report contains forbidden judgment")
    report_facts = _build_report_facts(
        config=content["config"],
        facts=facts,
        extension_tables=extension_tables,
    )
    summary_bytes, metrics_bytes = _report_payloads(
        identity=identity,
        evidence=content["evidence"],
        report_facts=report_facts,
        package_sha256=document["package_sha256"],
    )
    if (
        report_payloads["execution-summary"] != summary_bytes
        or report_payloads["metrics"] != metrics_bytes
    ):
        raise ResultContractError("result package report does not match package facts")
    return document


def validate_result_package(path: Path) -> Mapping[str, object]:
    root = Path(path).resolve()
    document = _read_json(root / "manifest.json", "manifest")
    return _validate_result_package_document(root, document)


def write_result_package(request: ResultPackageRequest) -> ResultPackage:
    writer_started = time.perf_counter()
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (request.strategy_id, request.scenario_id, request.run_id)
    ):
        raise ResultContractError("result package identity is incomplete")
    inputs = _freeze_request_inputs(request)
    core_started = time.perf_counter()
    extensions = _validate_extension_contracts(request.extensions)
    facts = _materialize_core(request.execution)
    writer_stages = {
        "core_facts": time.perf_counter() - core_started,
        "parquet_materialize": 0.0,
        "readback_validate": 0.0,
        "report_and_manifest": 0.0,
    }
    target = Path(request.output_dir).resolve()
    if target.exists():
        core_summaries = _validate_common_facts(facts)
        _, extension_summaries = _validate_extensions(request.extensions)
        existing = validate_result_package(target)
        try:
            existing_performance = _read_json(
                target / "evidence/performance.json",
                "performance",
            )
        except ResultContractError:
            raise
        if not isinstance(existing_performance, Mapping):
            raise ResultContractError("result package performance evidence is invalid")
        existing_inputs = _FrozenInputs(
            inputs.code,
            inputs.config,
            {**inputs.evidence, "performance.json": dict(existing_performance)},
        )
        package_sha256 = _canonical_digest(
            _content_document(
                request,
                existing_inputs,
                extensions,
                core_summaries,
                extension_summaries,
            )
        )
        if existing["package_sha256"] == package_sha256:
            existing_stages = existing_performance.get("stages")
            return ResultPackage(
                target,
                existing,
                package_sha256,
                dict(existing_stages) if isinstance(existing_stages, Mapping) else {},
                time.perf_counter() - writer_started,
            )
        raise ResultContractError("result package digest conflict")

    if request.atomic_publish:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.is_dir():
        raise ResultContractError("pre-staged result parent is missing")
    scenario_document = inputs.config.get("scenario.json")
    if (
        not isinstance(scenario_document, Mapping)
        or scenario_document.get("scenario_id") != request.scenario_id
    ):
        raise ResultContractError(
            "result package scenario identity differs from frozen configuration"
        )
    staging = (
        target.parent / f".{request.run_id}.{uuid.uuid4().hex}.tmp"
        if request.atomic_publish
        else target
    )
    try:
        staging.mkdir()
        (staging / "data").mkdir()
        (staging / "extensions").mkdir()
        code = _write_code_files(staging, inputs.code)
        config = _write_documents(staging, "config", inputs.config)
        parquet_started = time.perf_counter()
        for name, table in facts.tables().items():
            pq.write_table(table, staging / "data" / f"{name}.parquet", compression="zstd")
        for name, extension in sorted(extensions.items()):
            directory = staging / "extensions" / name
            directory.mkdir()
            pq.write_table(extension.table, directory / "data.parquet", compression="zstd")
        writer_stages["parquet_materialize"] = time.perf_counter() - parquet_started
        readback_started = time.perf_counter()
        readback_facts, readback_extensions = _readback_tables(
            staging, facts, extensions
        )
        core_summaries = _validate_common_facts(readback_facts)
        extension_summaries: dict[str, dict[str, object]] = {}
        for name, extension in extensions.items():
            summary = _table_summary(readback_extensions[name])
            rows = summary["rows"]
            if not isinstance(rows, list):
                raise ResultContractError("extension logical rows are invalid")
            _validate_unique_key(
                readback_extensions[name],
                extension.unique_key,
                name,
                rows=rows,
            )
            extension_summaries[name] = summary
        writer_stages["readback_validate"] = time.perf_counter() - readback_started

        report_started = time.perf_counter()
        report_facts = _build_report_facts(
            config=inputs.config,
            facts=readback_facts,
            extension_tables=readback_extensions,
        )
        datasets, extension_entries = _manifest_table_entries(
            staging,
            readback_facts,
            extensions,
            readback_extensions,
        )
        writer_stages["report_and_manifest"] = time.perf_counter() - report_started
        prefinalization_seconds = time.perf_counter() - writer_started
        final_inputs = _with_writer_stages(
            inputs,
            writer_stages,
            request.performance_finalizer,
            writer_measurement={
                "prefinalization_seconds": prefinalization_seconds,
            },
        )
        package_sha256 = _canonical_digest(
            _content_document(
                request,
                final_inputs,
                extensions,
                core_summaries,
                extension_summaries,
            )
        )
        evidence = _write_documents(staging, "evidence", final_inputs.evidence)
        reports, _ = _write_report(
            staging,
            request,
            final_inputs,
            report_facts,
            package_sha256,
        )
        manifest = _manifest(
            request,
            package_sha256,
            code,
            config,
            evidence,
            reports,
            datasets,
            extension_entries,
        )
        (staging / "manifest.json").write_bytes(_json_bytes(manifest))
        if request.atomic_publish:
            os.replace(staging, target)
        writer_finished = time.perf_counter()
        writer_stages["report_and_manifest"] = writer_finished - report_started
        return ResultPackage(
            target,
            manifest,
            package_sha256,
            dict(writer_stages),
            writer_finished - writer_started,
        )
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging)
        if isinstance(exc, ResultContractError):
            raise
        raise ResultContractError("result package write or publish failed") from exc
