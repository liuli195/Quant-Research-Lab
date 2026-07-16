from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import pyarrow.parquet as pq
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from scripts.research.local_quant_research.result_package import (
    ResultContractError,
    validate_result_package,
)


CORE_DATASETS = (
    "results",
    "balances",
    "positions",
    "orders",
    "risk",
    "period_risks",
)
LOCAL_PHYSICAL_DATASETS = CORE_DATASETS[:4]
_SHA256 = re.compile(r"[0-9a-f]{64}")
_EXTENSION_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_LOCAL_TOP_LEVEL = {
    "schema_version",
    "object",
    "source",
    "authority",
    "run",
    "code",
    "params",
    "performance",
    "datasets",
    "source_benchmark_returns",
    "gate",
    "extensions",
}
_JOINQUANT_ONLY_FIELDS = {
    "collection_fence",
    "research_response",
    "research_lineage",
    "official_summary",
}


def _load_schema(path: Path) -> Mapping[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(document)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SchemaError) as exc:
        raise RuntimeError(f"analysis schema is invalid: {path.name}") from exc
    return document


_REPO_ROOT = Path(__file__).resolve().parents[3]
_LOCAL_SCHEMA = _load_schema(
    Path(__file__).resolve().parent / "schemas" / "local-backtest-manifest.schema.json"
)
_JOINQUANT_SCHEMA = _load_schema(
    _REPO_ROOT
    / ".agents"
    / "skills"
    / "joinquant-archive-sync"
    / "references"
    / "manifest.schema.json"
)
_FORMAT_CHECKER = FormatChecker()


class AnalysisManifestError(ValueError):
    """Raised when an analysis source cannot prove its declared identity."""


def _validate_schema(
    document: Mapping[str, object], schema: Mapping[str, object], name: str
) -> None:
    try:
        Draft202012Validator(
            schema, format_checker=_FORMAT_CHECKER
        ).validate(dict(document))
    except ValidationError as exc:
        raise AnalysisManifestError(f"{name} schema validation failed: {exc.message}") from exc


@dataclass(frozen=True)
class AnalysisSource:
    root: Path
    kind: str
    schema_version: int | str
    manifest: Mapping[str, object]
    authority: str | None = None
    backend: str | None = None
    formula_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())
        object.__setattr__(
            self, "manifest", MappingProxyType(dict(self.manifest))
        )


@dataclass(frozen=True)
class ValidationResult:
    status: str
    datasets: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "datasets", MappingProxyType(dict(self.datasets)))


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AnalysisManifestError(f"{field} must be an object")
    return value


def _exact_keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] | None = None,
    field: str,
) -> None:
    allowed = required | (optional or set())
    if not required.issubset(value) or set(value) - allowed:
        raise AnalysisManifestError(f"{field} structure is invalid")


def _non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise AnalysisManifestError(f"{field} must be a non-empty string")
    return value


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise AnalysisManifestError(f"{field} must be a lowercase SHA256")
    return value


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnalysisManifestError(f"{field} must be a non-negative integer")
    return value


def _date_or_none(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnalysisManifestError(f"{field} must use YYYY-MM-DD or null")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise AnalysisManifestError(f"{field} must use YYYY-MM-DD or null") from exc
    return value


def _file_ref(value: object, field: str, *, parquet: bool = False) -> Mapping[str, object]:
    result = _object(value, field)
    required = {"path", "sha256", "bytes"}
    if parquet:
        required |= {"rows", "format", "compression"}
    _exact_keys(result, required=required, field=field)
    path = _non_empty_string(result["path"], f"{field}.path")
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AnalysisManifestError(f"{field}.path is unsafe")
    _sha(result["sha256"], f"{field}.sha256")
    _non_negative_int(result["bytes"], f"{field}.bytes")
    if parquet:
        _non_negative_int(result["rows"], f"{field}.rows")
        if result["format"] != "parquet" or result["compression"] != "zstd":
            raise AnalysisManifestError(f"{field} must declare zstd Parquet")
    return result


def _validate_complete_dataset(name: str, value: object) -> None:
    dataset = _object(value, f"datasets.{name}")
    _exact_keys(
        dataset,
        required={
            "required",
            "status",
            "rows",
            "verified_empty",
            "time_range",
            "files",
            "evidence",
        },
        field=f"datasets.{name}",
    )
    if dataset["required"] is not True or dataset["status"] != "complete":
        raise AnalysisManifestError(f"datasets.{name} must be complete and required")
    rows = _non_negative_int(dataset["rows"], f"datasets.{name}.rows")
    if dataset["verified_empty"] is not (rows == 0):
        raise AnalysisManifestError(f"datasets.{name}.verified_empty is inconsistent")
    time_range = _object(dataset["time_range"], f"datasets.{name}.time_range")
    _exact_keys(
        time_range,
        required={"start", "end"},
        field=f"datasets.{name}.time_range",
    )
    start = _date_or_none(time_range["start"], f"datasets.{name}.time_range.start")
    end = _date_or_none(time_range["end"], f"datasets.{name}.time_range.end")
    if rows == 0:
        if start is not None or end is not None:
            raise AnalysisManifestError(f"datasets.{name}.time_range must be empty")
    elif start is None or end is None or start > end:
        raise AnalysisManifestError(f"datasets.{name}.time_range is invalid")
    files = dataset["files"]
    if not isinstance(files, list) or len(files) != 1:
        raise AnalysisManifestError(f"datasets.{name} must have one Parquet file")
    file_ref = _file_ref(files[0], f"datasets.{name}.files[0]", parquet=True)
    if file_ref["path"] != f"data/{name}.parquet" or file_ref["rows"] != rows:
        raise AnalysisManifestError(f"datasets.{name} file identity is invalid")
    evidence = _object(dataset["evidence"], f"datasets.{name}.evidence")
    _exact_keys(
        evidence,
        required={"fields", "unique_key"},
        field=f"datasets.{name}.evidence",
    )
    for key in ("fields", "unique_key"):
        if not isinstance(evidence[key], list) or not evidence[key]:
            raise AnalysisManifestError(f"datasets.{name}.evidence.{key} is invalid")


def _validate_missing_dataset(name: str, value: object) -> None:
    dataset = _object(value, f"datasets.{name}")
    _exact_keys(
        dataset,
        required={
            "required",
            "status",
            "reason",
            "rows",
            "verified_empty",
            "files",
        },
        field=f"datasets.{name}",
    )
    if (
        dataset["required"] is not False
        or dataset["status"] != "missing_at_source"
        or dataset["reason"] != "computed_by_strategy_analysis"
        or dataset["rows"] != 0
        or dataset["verified_empty"] is not True
        or dataset["files"] != []
    ):
        raise AnalysisManifestError(f"datasets.{name} must be missing at source")


def validate_local_manifest_document(document: Mapping[str, object]) -> None:
    if not isinstance(document, Mapping):
        raise AnalysisManifestError("local manifest must be an object")
    _validate_schema(document, _LOCAL_SCHEMA, "local manifest")
    required = _LOCAL_TOP_LEVEL - {"extensions"}
    _exact_keys(
        document,
        required=required,
        optional={"extensions"},
        field="local manifest",
    )
    if set(document) & _JOINQUANT_ONLY_FIELDS:
        raise AnalysisManifestError("local manifest contains JoinQuant evidence")
    if document["schema_version"] != "local-backtest/1":
        raise AnalysisManifestError("unsupported local manifest schema")

    object_identity = _object(document["object"], "object")
    _exact_keys(
        object_identity,
        required={"kind", "local_id", "status"},
        field="object",
    )
    if object_identity["kind"] != "local_backtest" or object_identity["status"] != "complete":
        raise AnalysisManifestError("local object identity is invalid")
    _non_empty_string(object_identity["local_id"], "object.local_id")

    source = _object(document["source"], "source")
    _exact_keys(
        source,
        required={"kind", "engine", "accounting"},
        field="source",
    )
    if source["kind"] != "local_vectorbt":
        raise AnalysisManifestError("local source identity is invalid")
    if document["authority"] != "local_research":
        raise AnalysisManifestError("local authority is invalid")
    run = _object(document["run"], "run")
    _exact_keys(
        run,
        required={"run_id", "scenario_id", "snapshot_id"},
        field="run",
    )
    for field in ("run_id", "scenario_id"):
        _non_empty_string(run[field], f"run.{field}")
    _sha(run["snapshot_id"], "run.snapshot_id")
    engine = _object(source["engine"], "source.engine")
    _exact_keys(
        engine,
        required={
            "backend",
            "adapter_version",
            "vectorbt",
            "numba",
            "numpy",
            "pandas",
        },
        field="source.engine",
    )
    if engine["backend"] != "vectorbt.Portfolio.from_order_func":
        raise AnalysisManifestError("local execution backend is invalid")
    for field in ("adapter_version", "vectorbt", "numba", "numpy", "pandas"):
        _non_empty_string(engine[field], f"source.engine.{field}")
    accounting = _object(source["accounting"], "source.accounting")
    accounting_contract = {
        "version": "turtle-etf-corporate-actions/1",
        "corporate_action_mode": "point_in_time_total_return_approximation",
        "continuity_factor_basis": "raw_previous_close_over_current_pre_close",
        "corporate_action_metadata_timing": "audit_only_may_be_retrospective",
        "price_basis": "continuous_economic_price",
        "quantity_basis": "economic_units",
        "cash_dividend_mode": "implicit_reinvestment_on_ex_date",
        "pay_date_cash_supported": False,
        "exact_joinquant_reconciliation": False,
    }
    _exact_keys(
        accounting,
        required={*accounting_contract, "corporate_actions_sha256"},
        field="source.accounting",
    )
    if any(accounting.get(field) != value for field, value in accounting_contract.items()):
        raise AnalysisManifestError("source.accounting precision boundary is invalid")
    _sha(
        accounting["corporate_actions_sha256"],
        "source.accounting.corporate_actions_sha256",
    )

    code = _object(document["code"], "code")
    code_ref = _file_ref(code, "code")
    if code_ref["path"] != "code.py":
        raise AnalysisManifestError("code.path must be code.py")
    params = _object(document["params"], "params")
    _exact_keys(params, required={"current", "version"}, field="params")
    current_params = _file_ref(params["current"], "params.current")
    version_params = _file_ref(params["version"], "params.version")
    if current_params["path"] != "params.json":
        raise AnalysisManifestError("params.current.path must be params.json")
    if (
        version_params["path"] != f"params_versions/{version_params['sha256']}.json"
        or current_params["sha256"] != version_params["sha256"]
    ):
        raise AnalysisManifestError("params version identity is invalid")
    performance = _file_ref(document["performance"], "performance")
    if performance["path"] != "performance.json":
        raise AnalysisManifestError("performance.path must be performance.json")

    datasets = _object(document["datasets"], "datasets")
    if set(datasets) != set(CORE_DATASETS):
        raise AnalysisManifestError("local manifest must declare six core datasets")
    for name in LOCAL_PHYSICAL_DATASETS:
        _validate_complete_dataset(name, datasets[name])
    for name in ("risk", "period_risks"):
        _validate_missing_dataset(name, datasets[name])

    benchmark = _object(document["source_benchmark_returns"], "source_benchmark_returns")
    _exact_keys(
        benchmark,
        required={"status", "reason", "null_rows"},
        field="source_benchmark_returns",
    )
    if (
        benchmark["status"] != "missing_at_source"
        or benchmark["reason"] != "independent_benchmark_set"
        or benchmark["null_rows"] != datasets["results"]["rows"]
    ):
        raise AnalysisManifestError("source benchmark return evidence is invalid")

    gate = _object(document["gate"], "gate")
    _exact_keys(
        gate,
        required={"status", "exceptions", "checks"},
        field="gate",
    )
    if gate["status"] not in {"pass", "fail"}:
        raise AnalysisManifestError("local manifest gate status is invalid")
    if not isinstance(gate["exceptions"], list):
        raise AnalysisManifestError("local manifest gate exceptions are invalid")
    if gate["status"] == "pass" and gate["exceptions"] != []:
        raise AnalysisManifestError("passing local manifest has exceptions")
    if not isinstance(gate["checks"], list) or not gate["checks"]:
        raise AnalysisManifestError("local manifest gate did not pass")
    if "extensions" in document and not isinstance(document["extensions"], Mapping):
        raise AnalysisManifestError("extensions must be an object")


def _resolve_file(root: Path, relative: object, field: str) -> Path:
    value = _non_empty_string(relative, field)
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AnalysisManifestError(f"{field} is unsafe")
    root_resolved = root.resolve()
    resolved = (root_resolved / candidate).resolve()
    if root_resolved not in resolved.parents:
        raise AnalysisManifestError(f"{field} escapes the source")
    return resolved


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_declared_file(root: Path, reference: Mapping[str, object], field: str) -> Path:
    path = _resolve_file(root, reference["path"], f"{field}.path")
    if not path.is_file():
        raise AnalysisManifestError(f"{field} is missing")
    if path.stat().st_size != reference["bytes"] or _file_digest(path) != reference["sha256"]:
        raise AnalysisManifestError(f"{field} digest or size mismatch")
    return path


def _validate_local_files(root: Path, document: Mapping[str, object]) -> None:
    code = _object(document["code"], "code")
    _verify_declared_file(root, code, "code")
    params = _object(document["params"], "params")
    _verify_declared_file(
        root, _object(params["current"], "params.current"), "params.current"
    )
    _verify_declared_file(
        root, _object(params["version"], "params.version"), "params.version"
    )
    _verify_declared_file(
        root, _object(document["performance"], "performance"), "performance"
    )
    datasets = _object(document["datasets"], "datasets")
    for name in LOCAL_PHYSICAL_DATASETS:
        entry = _object(datasets[name], f"datasets.{name}")
        reference = _object(entry["files"][0], f"datasets.{name}.files[0]")
        path = _verify_declared_file(root, reference, f"datasets.{name}.files[0]")
        try:
            rows = pq.ParquetFile(path).metadata.num_rows
        except Exception as exc:
            raise AnalysisManifestError(f"datasets.{name} is invalid Parquet") from exc
        if rows != entry["rows"] or rows != reference["rows"]:
            raise AnalysisManifestError(f"datasets.{name} row count mismatch")


def _validate_joinquant_document(document: Mapping[str, object]) -> None:
    _validate_schema(document, _JOINQUANT_SCHEMA, "joinquant manifest")
    if document.get("schema_version") != 1 or isinstance(
        document.get("schema_version"), bool
    ):
        raise AnalysisManifestError("unsupported joinquant manifest schema")
    object_identity = _object(document.get("object"), "joinquant object")
    if object_identity.get("kind") != "backtest":
        raise AnalysisManifestError("joinquant manifest is not a backtest")
    source = _object(document.get("source"), "joinquant source")
    if not isinstance(source.get("url"), str) or not isinstance(source.get("aliases"), list):
        raise AnalysisManifestError("joinquant source identity is invalid")
    gate = _object(document.get("gate"), "joinquant gate")
    gate_exceptions = gate.get("exceptions")
    allowed_exceptions = {"attribution_log:missing_at_source"}
    if (
        gate.get("status") != "pass"
        or not isinstance(gate_exceptions, list)
        or any(item not in allowed_exceptions for item in gate_exceptions)
    ):
        raise AnalysisManifestError("joinquant archive gate did not pass")
    datasets = _object(document.get("datasets"), "joinquant datasets")
    for name in CORE_DATASETS:
        entry = _object(datasets.get(name), f"joinquant datasets.{name}")
        if entry.get("required") is not True or entry.get("status") != "complete":
            raise AnalysisManifestError(f"joinquant datasets.{name} is incomplete")
        _non_negative_int(entry.get("rows"), f"joinquant datasets.{name}.rows")


def _joinquant_parquet_reference(
    entry: Mapping[str, object], name: str
) -> Mapping[str, object] | None:
    files = entry.get("files", [])
    if not isinstance(files, list):
        raise AnalysisManifestError(f"joinquant datasets.{name}.files is invalid")
    matches = [
        item
        for item in files
        if isinstance(item, Mapping)
        and item.get("path") == f"data/{name}.parquet"
        and item.get("format") == "parquet"
    ]
    if len(matches) > 1:
        raise AnalysisManifestError(f"joinquant datasets.{name} has duplicate Parquet files")
    return None if not matches else matches[0]


def _validate_joinquant_files(root: Path, document: Mapping[str, object]) -> None:
    datasets = _object(document["datasets"], "joinquant datasets")
    for name in CORE_DATASETS:
        entry = _object(datasets[name], f"joinquant datasets.{name}")
        rows = int(entry["rows"])
        reference = _joinquant_parquet_reference(entry, name)
        if reference is None:
            if rows != 0 or entry.get("verified_empty") is not True:
                raise AnalysisManifestError(
                    f"joinquant datasets.{name} lacks required Parquet evidence"
                )
            continue
        for field in ("path", "sha256", "bytes", "rows"):
            if field not in reference:
                raise AnalysisManifestError(
                    f"joinquant datasets.{name} file evidence is incomplete"
                )
        _sha(reference["sha256"], f"joinquant datasets.{name}.sha256")
        _non_negative_int(reference["bytes"], f"joinquant datasets.{name}.bytes")
        _non_negative_int(reference["rows"], f"joinquant datasets.{name}.rows")
        path = _verify_declared_file(
            root, reference, f"joinquant datasets.{name}.parquet"
        )
        try:
            physical_rows = pq.ParquetFile(path).metadata.num_rows
        except Exception as exc:
            raise AnalysisManifestError(
                f"joinquant datasets.{name} is invalid Parquet"
            ) from exc
        if physical_rows != rows or physical_rows != reference["rows"]:
            raise AnalysisManifestError(f"joinquant datasets.{name} row count mismatch")


def _validate_research_file_reference(
    value: object,
    field: str,
    *,
    parquet: bool,
) -> Mapping[str, object]:
    reference = _object(value, field)
    required = {"path", "sha256", "bytes"}
    if parquet:
        required |= {"rows", "format", "compression"}
    _exact_keys(reference, required=required, field=field)
    path = _non_empty_string(reference["path"], f"{field}.path")
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AnalysisManifestError(f"{field}.path is unsafe")
    _sha(reference["sha256"], f"{field}.sha256")
    _non_negative_int(reference["bytes"], f"{field}.bytes")
    if parquet:
        _non_negative_int(reference["rows"], f"{field}.rows")
        if reference["format"] != "parquet" or reference["compression"] != "snappy":
            raise AnalysisManifestError(f"{field} must declare snappy Parquet")
    return reference


def _validate_research_table_entry(
    name: str,
    value: object,
    *,
    extension: bool,
) -> Mapping[str, object]:
    field = f"extensions.{name}" if extension else f"datasets.{name}"
    entry = _object(value, field)
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
    _exact_keys(entry, required=required, field=field)
    rows = _non_negative_int(entry["rows"], f"{field}.rows")
    if (
        entry["required"] is not True
        or entry["status"] != "complete"
        or entry["verified_empty"] is not (rows == 0)
    ):
        raise AnalysisManifestError(f"{field} completion evidence is invalid")
    schema = entry["schema"]
    if not isinstance(schema, list) or not schema:
        raise AnalysisManifestError(f"{field}.schema is invalid")
    schema_names: list[str] = []
    for index, item in enumerate(schema):
        definition = _object(item, f"{field}.schema[{index}]")
        _exact_keys(
            definition,
            required={"name", "type", "nullable"},
            field=f"{field}.schema[{index}]",
        )
        schema_names.append(
            _non_empty_string(definition["name"], f"{field}.schema[{index}].name")
        )
        _non_empty_string(definition["type"], f"{field}.schema[{index}].type")
        if not isinstance(definition["nullable"], bool):
            raise AnalysisManifestError(f"{field}.schema[{index}].nullable is invalid")
    if len(schema_names) != len(set(schema_names)):
        raise AnalysisManifestError(f"{field}.schema fields are duplicated")
    evidence = _object(entry["evidence"], f"{field}.evidence")
    _exact_keys(
        evidence,
        required={"fields", "unique_key"},
        field=f"{field}.evidence",
    )
    if evidence["fields"] != schema_names:
        raise AnalysisManifestError(f"{field}.evidence.fields is invalid")
    unique_key = evidence["unique_key"]
    if (
        not isinstance(unique_key, list)
        or not unique_key
        or any(item not in schema_names for item in unique_key)
        or len(unique_key) != len(set(unique_key))
    ):
        raise AnalysisManifestError(f"{field}.evidence.unique_key is invalid")
    time_range = _object(entry["time_range"], f"{field}.time_range")
    _exact_keys(time_range, required={"start", "end"}, field=f"{field}.time_range")
    start = _date_or_none(time_range["start"], f"{field}.time_range.start")
    end = _date_or_none(time_range["end"], f"{field}.time_range.end")
    if (start is None) != (end is None) or (start is not None and start > end):
        raise AnalysisManifestError(f"{field}.time_range is invalid")
    files = entry["files"]
    if not isinstance(files, list) or len(files) != 1:
        raise AnalysisManifestError(f"{field} must declare one Parquet file")
    reference = _validate_research_file_reference(
        files[0], f"{field}.files[0]", parquet=True
    )
    expected_path = (
        f"extensions/{name}/data.parquet"
        if extension
        else f"data/{name}.parquet"
    )
    if reference["path"] != expected_path or reference["rows"] != rows:
        raise AnalysisManifestError(f"{field} file identity is invalid")
    if extension:
        _non_empty_string(entry["schema_version"], f"{field}.schema_version")
        _object(entry["strategy_evidence"], f"{field}.strategy_evidence")
    return entry


def validate_local_research_manifest_document(document: Mapping[str, object]) -> None:
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
    _exact_keys(document, required=required, field="local research manifest")
    if document["schema_version"] != "local-research-package/2":
        raise AnalysisManifestError("unsupported local research package schema")
    identity = _object(document["object"], "object")
    _exact_keys(
        identity,
        required={"kind", "status", "strategy_id", "scenario_id", "run_id"},
        field="object",
    )
    if identity["kind"] != "local_research" or identity["status"] != "complete":
        raise AnalysisManifestError("local research object identity is invalid")
    for name in ("strategy_id", "scenario_id", "run_id"):
        _non_empty_string(identity[name], f"object.{name}")
    if document["authority"] != "local_research" or document["backend"] != "vectorbt":
        raise AnalysisManifestError("local research execution identity is invalid")
    _non_empty_string(document["formula_version"], "formula_version")
    _sha(document["package_sha256"], "package_sha256")

    datasets = _object(document["datasets"], "datasets")
    if set(datasets) != set(LOCAL_PHYSICAL_DATASETS):
        raise AnalysisManifestError("local research package must declare four datasets")
    for name in LOCAL_PHYSICAL_DATASETS:
        _validate_research_table_entry(name, datasets[name], extension=False)
    extensions = _object(document["extensions"], "extensions")
    for name, entry in extensions.items():
        if not isinstance(name, str) or _EXTENSION_NAME.fullmatch(name) is None:
            raise AnalysisManifestError("local research extension name is invalid")
        _validate_research_table_entry(name, entry, extension=True)
    for section in ("code", "config", "evidence", "reports"):
        values = _object(document[section], section)
        for name, reference in values.items():
            _non_empty_string(name, f"{section} name")
            _validate_research_file_reference(
                reference, f"{section}.{name}", parquet=False
            )
    reports = _object(document["reports"], "reports")
    if set(reports) != {"execution-summary", "metrics"}:
        raise AnalysisManifestError("local research reports are incomplete")
    gate = _object(document["gate"], "gate")
    _exact_keys(gate, required={"status", "exceptions", "checks"}, field="gate")
    if (
        gate["status"] != "pass"
        or gate["exceptions"] != []
        or not isinstance(gate["checks"], list)
        or not gate["checks"]
    ):
        raise AnalysisManifestError("local research package gate did not pass")


def _validate_local_research_files(
    root: Path, document: Mapping[str, object]
) -> None:
    for section in ("code", "config", "evidence", "reports"):
        values = _object(document[section], section)
        for name, reference in values.items():
            _verify_declared_file(
                root,
                _object(reference, f"{section}.{name}"),
                f"{section}.{name}",
            )
    datasets = _object(document["datasets"], "datasets")
    entries = [
        (f"datasets.{name}", _object(datasets[name], f"datasets.{name}"))
        for name in LOCAL_PHYSICAL_DATASETS
    ]
    extensions = _object(document["extensions"], "extensions")
    entries.extend(
        (f"extensions.{name}", _object(entry, f"extensions.{name}"))
        for name, entry in extensions.items()
    )
    for field, entry in entries:
        reference = _object(entry["files"][0], f"{field}.files[0]")
        path = _verify_declared_file(root, reference, f"{field}.files[0]")
        try:
            parquet = pq.ParquetFile(path)
            rows = parquet.metadata.num_rows
            compression = {
                parquet.metadata.row_group(group).column(column).compression
                for group in range(parquet.metadata.num_row_groups)
                for column in range(parquet.metadata.num_columns)
            }
        except Exception as exc:
            raise AnalysisManifestError(f"{field} is invalid Parquet") from exc
        if rows != entry["rows"] or rows != reference["rows"]:
            raise AnalysisManifestError(f"{field} row count mismatch")
        if compression and compression != {"SNAPPY"}:
            raise AnalysisManifestError(f"{field} physical compression is invalid")


def open_analysis_source(result_dir: Path) -> AnalysisSource:
    root = Path(result_dir).resolve()
    manifest_path = root / "manifest.json"
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisManifestError("analysis manifest is unreadable") from exc
    if not isinstance(document, dict):
        raise AnalysisManifestError("analysis manifest must be an object")
    version = document.get("schema_version")
    if version == 1 and not isinstance(version, bool):
        _validate_joinquant_document(document)
        _validate_joinquant_files(root, document)
        kind = "joinquant_backtest"
    elif version == "local-backtest/1":
        validate_local_manifest_document(document)
        if _object(document["gate"], "gate")["status"] != "pass":
            raise AnalysisManifestError("local archive gate did not pass")
        _validate_local_files(root, document)
        kind = "local_backtest"
    elif version == "local-research-package/2":
        try:
            document = dict(validate_result_package(root))
        except ResultContractError as exc:
            raise AnalysisManifestError(str(exc)) from exc
        kind = "local_research"
    else:
        raise AnalysisManifestError("unsupported analysis manifest schema")
    return AnalysisSource(
        root=root,
        kind=kind,
        schema_version=version,
        manifest=document,
        authority=(
            str(document["authority"])
            if isinstance(document.get("authority"), str)
            else None
        ),
        backend=(
            str(document["backend"])
            if isinstance(document.get("backend"), str)
            else None
        ),
        formula_version=(
            str(document["formula_version"])
            if isinstance(document.get("formula_version"), str)
            else None
        ),
    )


def validate_analysis_source(source: AnalysisSource) -> ValidationResult:
    if source.kind == "joinquant_backtest":
        _validate_joinquant_document(source.manifest)
        _validate_joinquant_files(source.root, source.manifest)
    elif source.kind == "local_backtest":
        validate_local_manifest_document(source.manifest)
        _validate_local_files(source.root, source.manifest)
    elif source.kind == "local_research":
        try:
            validate_result_package(source.root)
        except ResultContractError as exc:
            raise AnalysisManifestError(str(exc)) from exc
    else:
        raise AnalysisManifestError("unsupported analysis source kind")
    datasets = _object(source.manifest["datasets"], "datasets")
    names = LOCAL_PHYSICAL_DATASETS if source.kind == "local_research" else CORE_DATASETS
    return ValidationResult(
        status="pass",
        datasets={name: str(_object(datasets[name], name)["status"]) for name in names},
    )
