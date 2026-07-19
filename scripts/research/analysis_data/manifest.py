from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, cast

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
        Draft202012Validator(schema, format_checker=_FORMAT_CHECKER).validate(
            dict(document)
        )
    except ValidationError as exc:
        raise AnalysisManifestError(
            f"{name} schema validation failed: {exc.message}"
        ) from exc


@dataclass(frozen=True)
class AnalysisSource:
    root: Path
    kind: str
    schema_version: int | str
    manifest: Mapping[str, object]
    data_prefix: str = "data"
    snapshot_id: str | None = None
    authority: str | None = None
    backend: str | None = None
    formula_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())
        object.__setattr__(self, "manifest", MappingProxyType(dict(self.manifest)))


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AnalysisManifestError(f"{field} must be an object")
    return value


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


def _validate_complete_dataset(name: str, dataset: Mapping[str, object]) -> None:
    rows = cast(int, dataset["rows"])
    if dataset["verified_empty"] is not (rows == 0):
        raise AnalysisManifestError(f"datasets.{name}.verified_empty is inconsistent")
    time_range = cast(Mapping[str, object], dataset["time_range"])
    start = cast(str | None, time_range["start"])
    end = cast(str | None, time_range["end"])
    if rows == 0:
        if start is not None or end is not None:
            raise AnalysisManifestError(f"datasets.{name}.time_range must be empty")
    elif start is None or end is None or start > end:
        raise AnalysisManifestError(f"datasets.{name}.time_range is invalid")
    file_ref = cast(Mapping[str, object], cast(list[object], dataset["files"])[0])
    if file_ref["path"] != f"data/{name}.parquet" or file_ref["rows"] != rows:
        raise AnalysisManifestError(f"datasets.{name} file identity is invalid")


def validate_local_manifest_document(document: Mapping[str, object]) -> None:
    if not isinstance(document, Mapping):
        raise AnalysisManifestError("local manifest must be an object")
    _validate_schema(document, _LOCAL_SCHEMA, "local manifest")
    code = cast(Mapping[str, object], document["code"])
    if code["path"] != "code.py":
        raise AnalysisManifestError("code.path must be code.py")
    params = cast(Mapping[str, object], document["params"])
    current_params = cast(Mapping[str, object], params["current"])
    version_params = cast(Mapping[str, object], params["version"])
    if current_params["path"] != "params.json":
        raise AnalysisManifestError("params.current.path must be params.json")
    if (
        version_params["path"] != f"params_versions/{version_params['sha256']}.json"
        or current_params["sha256"] != version_params["sha256"]
    ):
        raise AnalysisManifestError("params version identity is invalid")
    performance = cast(Mapping[str, object], document["performance"])
    if performance["path"] != "performance.json":
        raise AnalysisManifestError("performance.path must be performance.json")

    datasets = cast(Mapping[str, object], document["datasets"])
    for name in LOCAL_PHYSICAL_DATASETS:
        _validate_complete_dataset(name, cast(Mapping[str, object], datasets[name]))

    benchmark = cast(Mapping[str, object], document["source_benchmark_returns"])
    results = cast(Mapping[str, object], datasets["results"])
    if benchmark["null_rows"] != results["rows"]:
        raise AnalysisManifestError("source benchmark return evidence is invalid")

    gate = cast(Mapping[str, object], document["gate"])
    if gate["status"] == "pass" and gate["exceptions"] != []:
        raise AnalysisManifestError("passing local manifest has exceptions")


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


def _verify_declared_file(
    root: Path, reference: Mapping[str, object], field: str
) -> Path:
    path = _resolve_file(root, reference["path"], f"{field}.path")
    if not path.is_file():
        raise AnalysisManifestError(f"{field} is missing")
    if (
        path.stat().st_size != reference["bytes"]
        or _file_digest(path) != reference["sha256"]
    ):
        raise AnalysisManifestError(f"{field} digest or size mismatch")
    return path


def _validate_local_files(root: Path, document: Mapping[str, object]) -> None:
    code = cast(Mapping[str, object], document["code"])
    _verify_declared_file(root, code, "code")
    params = cast(Mapping[str, object], document["params"])
    _verify_declared_file(
        root, cast(Mapping[str, object], params["current"]), "params.current"
    )
    _verify_declared_file(
        root, cast(Mapping[str, object], params["version"]), "params.version"
    )
    _verify_declared_file(
        root, cast(Mapping[str, object], document["performance"]), "performance"
    )
    datasets = cast(Mapping[str, object], document["datasets"])
    for name in LOCAL_PHYSICAL_DATASETS:
        entry = cast(Mapping[str, object], datasets[name])
        reference = cast(Mapping[str, object], cast(list[object], entry["files"])[0])
        path = _verify_declared_file(root, reference, f"datasets.{name}.files[0]")
        try:
            rows = pq.ParquetFile(path).metadata.num_rows
        except Exception as exc:
            raise AnalysisManifestError(f"datasets.{name} is invalid Parquet") from exc
        if rows != entry["rows"] or rows != reference["rows"]:
            raise AnalysisManifestError(f"datasets.{name} row count mismatch")


def _validate_joinquant_document(
    document: Mapping[str, object], *, snapshot_id: str | None
) -> str:
    _validate_schema(document, _JOINQUANT_SCHEMA, "joinquant manifest")
    object_identity = cast(Mapping[str, object], document["object"])
    object_kind = object_identity.get("kind")
    if object_kind not in {"backtest", "simulation"}:
        raise AnalysisManifestError("joinquant manifest is not an analysis source")
    if object_kind == "simulation" and snapshot_id is None:
        raise AnalysisManifestError("joinquant simulation requires an explicit snapshot_id")
    if object_kind == "backtest" and snapshot_id is not None:
        raise AnalysisManifestError("joinquant backtest cannot use snapshot_id")
    gate = cast(Mapping[str, object], document["gate"])
    gate_exceptions = cast(list[object], gate["exceptions"])
    allowed_exceptions = {"attribution_log:missing_at_source"}
    if object_kind == "simulation":
        allowed_exceptions.add("performance_profile:unsupported_api_version")
    if gate.get("status") != "pass" or any(
        item not in allowed_exceptions for item in gate_exceptions
    ):
        raise AnalysisManifestError("joinquant archive gate did not pass")
    datasets = cast(Mapping[str, object], document["datasets"])
    for name in CORE_DATASETS:
        entry = _object(datasets.get(name), f"joinquant datasets.{name}")
        if entry.get("required") is not True or entry.get("status") != "complete":
            raise AnalysisManifestError(f"joinquant datasets.{name} is incomplete")
        if "rows" not in entry:
            raise AnalysisManifestError(f"joinquant datasets.{name}.rows is missing")
    return str(object_kind)


def _joinquant_parquet_reference(
    entry: Mapping[str, object], name: str, data_prefix: str
) -> Mapping[str, object] | None:
    files = entry.get("files", [])
    if not isinstance(files, list):
        raise AnalysisManifestError(f"joinquant datasets.{name}.files is invalid")
    matches = [
        item
        for item in files
        if isinstance(item, Mapping)
        and item.get("path") == f"{data_prefix}/{name}.parquet"
        and item.get("format") == "parquet"
    ]
    if len(matches) > 1:
        raise AnalysisManifestError(
            f"joinquant datasets.{name} has duplicate Parquet files"
        )
    return None if not matches else matches[0]


def _validate_joinquant_files(
    root: Path, document: Mapping[str, object], *, data_prefix: str
) -> None:
    datasets = cast(Mapping[str, object], document["datasets"])
    for name in CORE_DATASETS:
        entry = _object(datasets[name], f"joinquant datasets.{name}")
        rows = int(entry["rows"])
        reference = _joinquant_parquet_reference(entry, name, data_prefix)
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


def _validate_simulation_snapshot(
    document: Mapping[str, object], snapshot_id: str
) -> str:
    streams = _object(document.get("streams"), "joinquant streams")
    snapshots = _object(streams.get("snapshots"), "joinquant streams.snapshots")
    data = _object(streams.get("data"), "joinquant streams.data")
    if snapshots.get("cursor") != snapshot_id or data.get("cursor") != snapshot_id:
        raise AnalysisManifestError("joinquant simulation registered snapshot is stale")
    prefix = f"snapshots/{snapshot_id}/"
    datasets = _object(document.get("datasets"), "joinquant datasets")
    for name in CORE_DATASETS:
        entry = _object(datasets.get(name), f"joinquant datasets.{name}")
        files = entry.get("files")
        if not isinstance(files, list):
            raise AnalysisManifestError(f"joinquant datasets.{name}.files is invalid")
        for reference in files:
            if not isinstance(reference, Mapping) or not isinstance(
                reference.get("path"), str
            ):
                raise AnalysisManifestError(
                    f"joinquant datasets.{name}.files is invalid"
                )
            if not str(reference["path"]).startswith(prefix):
                raise AnalysisManifestError(
                    "joinquant simulation data is outside the registered snapshot"
                )
    return f"{prefix}data"


def open_analysis_source(
    result_dir: Path, *, snapshot_id: str | None = None
) -> AnalysisSource:
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
        object_kind = _validate_joinquant_document(document, snapshot_id=snapshot_id)
        data_prefix = "data"
        if object_kind == "simulation":
            assert snapshot_id is not None
            data_prefix = _validate_simulation_snapshot(document, snapshot_id)
        _validate_joinquant_files(root, document, data_prefix=data_prefix)
        kind = f"joinquant_{object_kind}"
    elif version == "local-backtest/1":
        validate_local_manifest_document(document)
        if cast(Mapping[str, object], document["gate"])["status"] != "pass":
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
        data_prefix=data_prefix if version == 1 and not isinstance(version, bool) else "data",
        snapshot_id=snapshot_id if kind == "joinquant_simulation" else None,
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
