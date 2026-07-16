from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from scripts.research.local_quant_research.contracts import (
    ExecutionBundle,
    ResultExtension,
)


PACKAGE_SCHEMA_VERSION = "local-research-package/2"
FORMULA_VERSION = "unified-strategy-analysis/1"
CORE_DATASETS = ("results", "balances", "positions", "orders")
FORBIDDEN_REPORT_PHRASES = ("推荐", "稳健性通过", "适合实盘", "实盘准入")

_EXTENSION_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
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


@dataclass(frozen=True, slots=True)
class ResultPackage:
    path: Path
    manifest: Mapping[str, object]
    package_sha256: str


@dataclass(frozen=True, slots=True)
class _CoreFacts:
    results: pa.Table
    balances: pa.Table
    positions: pa.Table
    orders: pa.Table

    def tables(self) -> dict[str, pa.Table]:
        return {name: getattr(self, name) for name in CORE_DATASETS}


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


def _safe_relative(value: str, *, suffix: str | None = None) -> Path:
    if not isinstance(value, str) or not value:
        raise ResultContractError("package file name is required")
    normalized = value.replace("\\", "/")
    candidate = Path(normalized)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.name in {"", "."}:
        raise ResultContractError("package file name is unsafe")
    if suffix is not None and candidate.suffix != suffix:
        candidate = candidate.with_name(candidate.name + suffix)
    return candidate


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
    _validate_common_facts(facts)
    return facts


def _validate_unique_key(table: pa.Table, key: tuple[str, ...], field: str) -> None:
    if not key or any(name not in table.schema.names for name in key):
        raise ResultContractError(f"{field} unique key is invalid")
    values = list(zip(*(table[name].to_pylist() for name in key), strict=True))
    if any(any(item is None for item in row) for row in values):
        raise ResultContractError(f"{field} unique key contains null")
    if len(values) != len(set(values)):
        raise ResultContractError(f"{field} unique key is not unique")


def _validate_common_facts(facts: _CoreFacts) -> None:
    tables = facts.tables()
    for name, schema in _SCHEMAS.items():
        if tables[name].schema != schema:
            raise ResultContractError(f"{name} fields do not match the contract")
        _validate_unique_key(tables[name], _UNIQUE_KEYS[name], name)
    if facts.results.num_rows != facts.balances.num_rows:
        raise ResultContractError("results and balances do not reconcile")
    if facts.results["benchmark_returns"].null_count != facts.results.num_rows:
        raise ResultContractError("source benchmark returns must remain null")

    result_rows = facts.results.to_pylist()
    balance_rows = facts.balances.to_pylist()
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
    for item in facts.positions.to_pylist():
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
    for item in facts.orders.to_pylist():
        if str(item["time"])[:10] not in result_dates:
            raise ResultContractError("order date is absent from results")
        if not 0 <= int(item["filled"]) <= int(item["amount"]):
            raise ResultContractError("order filled amount is invalid")


def _validate_extensions(
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
        if not isinstance(extension.table, pa.Table):
            raise ResultContractError("extension table must be an Arrow table")
        _validate_unique_key(extension.table, extension.unique_key, extension.name)
        _jsonable(extension.evidence)
        result[extension.name] = extension
    return result


def _schema_document(schema: pa.Schema) -> list[dict[str, object]]:
    return [
        {"name": field.name, "type": str(field.type), "nullable": field.nullable}
        for field in schema
    ]


def _table_summary(table: pa.Table) -> dict[str, object]:
    return {"schema": _schema_document(table.schema), "rows": table.to_pylist()}


def _content_document(
    request: ResultPackageRequest,
    facts: _CoreFacts,
    extensions: Mapping[str, ResultExtension],
) -> dict[str, object]:
    code = {
        name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for name, path in sorted(request.code_files.items())
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
        "config": dict(request.config_documents),
        "evidence": dict(request.evidence_documents),
        "datasets": {
            name: _table_summary(table) for name, table in facts.tables().items()
        },
        "extensions": {
            name: {
                "schema_version": extension.schema_version,
                "unique_key": list(extension.unique_key),
                "evidence": dict(extension.evidence),
                "table": _table_summary(extension.table),
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
        result.update({"rows": rows, "format": "parquet", "compression": "snappy"})
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


def _write_code_files(root: Path, values: Mapping[str, Path]) -> dict[str, dict[str, object]]:
    references: dict[str, dict[str, object]] = {}
    destinations: set[Path] = set()
    for name, source_value in sorted(values.items()):
        relative = _safe_relative(name)
        destination = root / "code" / relative
        if destination in destinations:
            raise ResultContractError("code file destinations must be unique")
        source = Path(source_value)
        if not source.is_file():
            raise ResultContractError(f"code file is missing: {name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
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


def _write_report(
    root: Path,
    request: ResultPackageRequest,
    facts: _CoreFacts,
    extensions: Mapping[str, ResultExtension],
    package_sha256: str,
) -> dict[str, dict[str, object]]:
    report_dir = root / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    metrics = report_dir / "metrics.json"
    metrics.write_bytes(
        _json_bytes(
            {
                "package_sha256": package_sha256,
                "datasets": {
                    name: table.num_rows for name, table in facts.tables().items()
                },
                "extensions": {
                    name: extension.table.num_rows
                    for name, extension in sorted(extensions.items())
                },
            }
        )
    )
    summary = report_dir / "execution-summary.md"
    lines = [
        "# 本地研究执行摘要",
        "",
        f"- 策略：`{request.strategy_id}`",
        f"- 场景：`{request.scenario_id}`",
        f"- 运行：`{request.run_id}`",
        f"- 内容摘要：`{package_sha256}`",
        "",
        "## 核心事实行数",
        "",
    ]
    lines.extend(
        f"- `{name}`：{table.num_rows}" for name, table in facts.tables().items()
    )
    lines.extend(["", "## 扩展行数", ""])
    if extensions:
        lines.extend(
            f"- `{name}`：{extension.table.num_rows}"
            for name, extension in sorted(extensions.items())
        )
    else:
        lines.append("- 无")
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "execution-summary": _file_ref(root, summary),
        "metrics": _file_ref(root, metrics),
    }


def _readback_tables(
    root: Path,
    facts: _CoreFacts,
    extensions: Mapping[str, ResultExtension],
) -> tuple[_CoreFacts, dict[str, pa.Table]]:
    try:
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
    _validate_common_facts(core)
    for name, expected in facts.tables().items():
        observed = core.tables()[name]
        if _table_summary(observed) != _table_summary(expected):
            raise ResultContractError(f"{name} readback changed logical facts")
    for name, extension in extensions.items():
        observed = extension_tables[name]
        _validate_unique_key(observed, extension.unique_key, name)
        if _table_summary(observed) != _table_summary(extension.table):
            raise ResultContractError(f"extension {name} readback changed logical facts")
    return core, extension_tables


def _manifest(
    root: Path,
    request: ResultPackageRequest,
    facts: _CoreFacts,
    extensions: Mapping[str, ResultExtension],
    package_sha256: str,
    code: Mapping[str, Mapping[str, object]],
    config: Mapping[str, Mapping[str, object]],
    evidence: Mapping[str, Mapping[str, object]],
    reports: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    datasets = {
        name: _table_entry(
            root,
            root / "data" / f"{name}.parquet",
            table,
            _UNIQUE_KEYS[name],
        )
        for name, table in facts.tables().items()
    }
    extension_entries = {
        name: {
            **_table_entry(
                root,
                root / "extensions" / name / "data.parquet",
                extension.table,
                extension.unique_key,
            ),
            "schema_version": extension.schema_version,
            "strategy_evidence": _jsonable(extension.evidence),
        }
        for name, extension in sorted(extensions.items())
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
        "package_sha256": package_sha256,
        "code": dict(code),
        "config": dict(config),
        "evidence": dict(evidence),
        "datasets": datasets,
        "extensions": extension_entries,
        "reports": dict(reports),
        "gate": {
            "status": "pass",
            "exceptions": [],
            "checks": [
                "schema",
                "digests",
                "unique_keys",
                "cross_table_reconciliation",
                "readback",
            ],
        },
    }


def _read_json(path: Path, field: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResultContractError(f"{field} is unreadable") from exc


def _resolve_declared(root: Path, reference: object, field: str) -> Path:
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
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ResultContractError(f"{field} reference path is unsafe")
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
) -> tuple[pa.Table, tuple[str, ...]]:
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
    optional = {"schema_version", "strategy_evidence"}
    if not required.issubset(entry_value) or set(entry_value) - required - optional:
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
    path = _resolve_declared(root, reference, name)
    if (
        not isinstance(reference, Mapping)
        or reference.get("rows") != rows
        or reference.get("format") != "parquet"
        or reference.get("compression") != "snappy"
    ):
        raise ResultContractError(f"{name} Parquet declaration is invalid")
    try:
        table = pq.read_table(path)
    except Exception as exc:
        raise ResultContractError(f"{name} Parquet readback failed") from exc
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
    _validate_unique_key(table, unique_key, name)
    if entry_value["time_range"] != _time_range(table):
        raise ResultContractError(f"{name} time range mismatch")
    return table, unique_key


def validate_result_package(path: Path) -> Mapping[str, object]:
    root = Path(path).resolve()
    document = _read_json(root / "manifest.json", "manifest")
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
        or any(not isinstance(identity[name], str) or not identity[name] for name in ("strategy_id", "scenario_id", "run_id"))
        or document["authority"] != "local_research"
        or document["backend"] != "vectorbt"
        or document["formula_version"] != FORMULA_VERSION
        or not isinstance(document["package_sha256"], str)
        or _SHA256.fullmatch(document["package_sha256"]) is None
    ):
        raise ResultContractError("result package identity is invalid")
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

    datasets = document["datasets"]
    if not isinstance(datasets, Mapping) or set(datasets) != set(CORE_DATASETS):
        raise ResultContractError("result package datasets are invalid")
    core_tables: dict[str, pa.Table] = {}
    for name in CORE_DATASETS:
        table, key = _validate_table_entry(root, name, datasets[name], schema=_SCHEMAS[name])
        if key != _UNIQUE_KEYS[name]:
            raise ResultContractError(f"{name} unique key does not match the contract")
        core_tables[name] = table
    facts = _CoreFacts(**core_tables)
    _validate_common_facts(facts)

    extensions_value = document["extensions"]
    if not isinstance(extensions_value, Mapping):
        raise ResultContractError("extensions declaration is invalid")
    extension_content: dict[str, object] = {}
    for name, entry_value in extensions_value.items():
        if not isinstance(name, str) or _EXTENSION_NAME.fullmatch(name) is None:
            raise ResultContractError("extension name is invalid")
        if not isinstance(entry_value, Mapping):
            raise ResultContractError(f"extension {name} declaration is invalid")
        schema_version = entry_value.get("schema_version")
        if not isinstance(schema_version, str) or not schema_version:
            raise ResultContractError(f"extension {name} schema_version is invalid")
        table, key = _validate_table_entry(root, name, entry_value, schema=None)
        extension_content[name] = {
            "schema_version": schema_version,
            "unique_key": list(key),
            "evidence": entry_value.get("strategy_evidence"),
            "table": _table_summary(table),
        }

    content: dict[str, object] = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "object": dict(identity),
        "authority": document["authority"],
        "backend": document["backend"],
        "formula_version": document["formula_version"],
        "datasets": {
            name: _table_summary(table) for name, table in core_tables.items()
        },
        "extensions": extension_content,
    }
    for section in ("code", "config", "evidence", "reports"):
        values = document[section]
        if not isinstance(values, Mapping) or any(not isinstance(name, str) for name in values):
            raise ResultContractError(f"{section} declaration is invalid")
        resolved = {
            name: _resolve_declared(root, reference, f"{section}.{name}")
            for name, reference in values.items()
        }
        if section == "code":
            content[section] = {
                name: hashlib.sha256(file.read_bytes()).hexdigest()
                for name, file in resolved.items()
            }
        elif section in {"config", "evidence"}:
            content[section] = {
                name: _read_json(file, f"{section}.{name}")
                for name, file in resolved.items()
            }
        else:
            summary = resolved.get("execution-summary")
            if summary is None or resolved.get("metrics") is None:
                raise ResultContractError("reports declaration is incomplete")
            report = summary.read_text(encoding="utf-8")
            if any(phrase in report for phrase in FORBIDDEN_REPORT_PHRASES):
                raise ResultContractError("execution report contains forbidden judgment")
    if _canonical_digest(content) != document["package_sha256"]:
        raise ResultContractError("result package logical digest mismatch")
    return document


def write_result_package(request: ResultPackageRequest) -> ResultPackage:
    if any(
        not isinstance(value, str) or not value
        for value in (request.strategy_id, request.scenario_id, request.run_id)
    ):
        raise ResultContractError("result package identity is incomplete")
    extensions = _validate_extensions(request.extensions)
    for name, path in request.code_files.items():
        _safe_relative(name)
        if not Path(path).is_file():
            raise ResultContractError(f"code file is missing: {name}")
    for values in (request.config_documents, request.evidence_documents):
        for name, document in values.items():
            _safe_relative(name, suffix=".json")
            _jsonable(document)

    facts = _materialize_core(request.execution)
    package_sha256 = _canonical_digest(_content_document(request, facts, extensions))
    target = Path(request.output_dir).resolve()
    if target.exists():
        existing = validate_result_package(target)
        if existing["package_sha256"] == package_sha256:
            return ResultPackage(target, existing, package_sha256)
        raise ResultContractError("result package digest conflict")

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{request.run_id}.{uuid.uuid4().hex}.tmp"
    try:
        staging.mkdir()
        (staging / "data").mkdir()
        (staging / "extensions").mkdir()
        code = _write_code_files(staging, request.code_files)
        config = _write_documents(staging, "config", request.config_documents)
        evidence = _write_documents(staging, "evidence", request.evidence_documents)
        for name, table in facts.tables().items():
            pq.write_table(table, staging / "data" / f"{name}.parquet", compression="snappy")
        for name, extension in sorted(extensions.items()):
            directory = staging / "extensions" / name
            directory.mkdir()
            pq.write_table(extension.table, directory / "data.parquet", compression="snappy")
        _readback_tables(staging, facts, extensions)
        reports = _write_report(staging, request, facts, extensions, package_sha256)
        manifest = _manifest(
            staging,
            request,
            facts,
            extensions,
            package_sha256,
            code,
            config,
            evidence,
            reports,
        )
        (staging / "manifest.json").write_bytes(_json_bytes(manifest))
        validated = validate_result_package(staging)
        os.replace(staging, target)
        return ResultPackage(target, validated, package_sha256)
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging)
        if isinstance(exc, ResultContractError):
            raise
        raise ResultContractError("result package write or publish failed") from exc
