from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
import pandas as pd
import pyarrow.parquet as pq

from scripts.research.analysis_data.manifest import (
    AnalysisManifestError,
    AnalysisSource,
    open_analysis_source,
)


class SourceRegistryError(ValueError):
    """Raised when an analysis source registry cannot prove its identity."""


@dataclass(frozen=True)
class SourceRegistration:
    scenario_id: str
    source_type: str
    root: Path
    manifest_sha256: str
    snapshot_id: str | None


@dataclass(frozen=True)
class RegisteredSource:
    registration: SourceRegistration
    source: AnalysisSource
    capabilities: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True)
class SourceRegistry:
    path: Path
    sha256: str
    analysis_plan: Path
    benchmark_manifest: Path
    baseline_scenario_id: str
    sources: tuple[RegisteredSource, ...]


_SCHEMA_PATH = Path(__file__).with_name("schemas") / "source-registry.schema.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceRegistryError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise SourceRegistryError(f"{label} must be an object")
    return value


def _repository_path(root: Path, value: object, label: str, *, directory: bool) -> Path:
    if not isinstance(value, str) or not value or value == "latest":
        raise SourceRegistryError(f"{label} must be an explicit repository-relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise SourceRegistryError(f"{label} must be an explicit repository-relative path")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or (not path.is_dir() if directory else not path.is_file()):
        raise SourceRegistryError(f"{label} is missing or outside the repository")
    return path


def _validate_document(document: Mapping[str, object]) -> None:
    schema = _load_json(_SCHEMA_PATH, "source registry schema")
    try:
        Draft202012Validator(schema).validate(dict(document))
    except ValidationError as exc:
        raise SourceRegistryError(f"source registry schema validation failed: {exc.message}") from exc


def _evidence_insufficient(reason: str) -> Mapping[str, object]:
    return {"status": "evidence_insufficient", "reason": reason}


def _attribution_entry(source: AnalysisSource) -> Mapping[str, object] | None:
    if source.kind == "local_research":
        extensions = source.manifest.get("extensions")
        entry = extensions.get("attribution_log") if isinstance(extensions, Mapping) else None
        return entry if isinstance(entry, Mapping) else None
    datasets = source.manifest.get("datasets")
    entry = datasets.get("attribution_log") if isinstance(datasets, Mapping) else None
    return entry if isinstance(entry, Mapping) and entry.get("status") == "complete" else None


def _attribution_path(
    source: AnalysisSource, reference: Mapping[str, object]
) -> Path | None:
    value = reference.get("path")
    if not isinstance(value, str) or not value:
        return None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    path = (source.root / relative).resolve()
    if not path.is_relative_to(source.root):
        return None
    if source.kind.startswith("joinquant_"):
        data_root = (source.root / source.data_prefix).resolve()
        if not path.is_relative_to(data_root):
            return None
    return path


def _source_result_range(source: AnalysisSource) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    path = (source.root / source.data_prefix / "results.parquet").resolve()
    if not path.is_file():
        return None
    try:
        values = pd.to_datetime(
            pq.read_table(path, columns=["time"]).to_pandas()["time"],
            errors="coerce",
            format="mixed",
        )
    except Exception:
        return None
    if values.empty or values.isna().any():
        return None
    return pd.Timestamp(values.min()), pd.Timestamp(values.max())


def _attribution_capability(source: AnalysisSource) -> Mapping[str, object]:
    entry = _attribution_entry(source)
    if entry is None:
        return {"status": "missing_at_source"}
    files = entry.get("files")
    if not isinstance(files, list):
        return _evidence_insufficient("invalid_file_declaration")
    references = [
        item
        for item in files
        if isinstance(item, Mapping) and item.get("format") == "parquet"
    ]
    if len(references) != 1:
        return _evidence_insufficient("invalid_parquet_declaration")
    reference = references[0]
    path = _attribution_path(source, reference)
    if path is None or not path.is_file():
        return _evidence_insufficient("invalid_source_path")
    digest = reference.get("sha256")
    expected_bytes = reference.get("bytes")
    expected_rows = reference.get("rows")
    if (
        not isinstance(digest, str)
        or not isinstance(expected_bytes, int)
        or not isinstance(expected_rows, int)
        or path.stat().st_size != expected_bytes
        or _sha256(path) != digest
    ):
        return _evidence_insufficient("digest_or_size_mismatch")
    try:
        parquet = pq.ParquetFile(path)
        physical_rows = parquet.metadata.num_rows
        fields = tuple(parquet.schema_arrow.names)
    except Exception:
        return _evidence_insufficient("invalid_parquet")
    if physical_rows != expected_rows or physical_rows <= 0:
        return _evidence_insufficient("row_count_mismatch")
    local = source.kind == "local_research"
    time_field = "time" if local else "current_dt"
    event_field = "event_type" if local else "event"
    identity_fields = ("event_id",) if local else ("audit_token", "seq")
    if any(field not in fields for field in (time_field, event_field, *identity_fields)):
        return _evidence_insufficient("required_event_fields_missing")
    try:
        frame = pq.read_table(
            path, columns=[time_field, event_field, *identity_fields]
        ).to_pandas()
        timestamps = pd.to_datetime(frame[time_field], errors="coerce", format="mixed")
    except Exception:
        return _evidence_insufficient("invalid_event_time")
    if timestamps.isna().any() or frame[event_field].isna().any() or (
        frame[event_field].astype(str).str.len() == 0
    ).any():
        return _evidence_insufficient("invalid_event_time_or_identity")
    source_range = _source_result_range(source)
    event_start = pd.Timestamp(timestamps.min())
    event_end = pd.Timestamp(timestamps.max())
    if source_range is None or event_start > event_end:
        return _evidence_insufficient("event_time_range_mismatch")
    if local:
        event_ids = frame["event_id"].fillna("").astype(str)
        if event_ids.eq("").any() or event_ids.duplicated().any():
            return _evidence_insufficient("invalid_event_id")
        declared_range = entry.get("time_range")
        if not isinstance(declared_range, Mapping):
            return _evidence_insufficient("event_time_range_mismatch")
        try:
            declared_start = pd.Timestamp(str(declared_range["start"]))
            declared_end = pd.Timestamp(str(declared_range["end"]))
        except (KeyError, ValueError, TypeError):
            return _evidence_insufficient("event_time_range_mismatch")
        event_days = timestamps.dt.normalize()
        source_start, source_end = (item.normalize() for item in source_range)
        if (
            event_start.normalize() != declared_start.normalize()
            or event_end.normalize() != declared_end.normalize()
            or ((event_days < source_start) | (event_days > source_end)).any()
        ):
            return _evidence_insufficient("event_time_range_mismatch")
    else:
        evidence = entry.get("evidence")
        details = evidence.get("evidence") if isinstance(evidence, Mapping) else None
        if not isinstance(evidence, Mapping) or not isinstance(details, Mapping):
            return _evidence_insufficient("invalid_event_identity")
        tokens = frame["audit_token"].fillna("").astype(str)
        expected_token = evidence.get("token")
        if (
            not isinstance(expected_token, str)
            or not expected_token
            or details.get("expected_token") != expected_token
            or set(tokens) != {expected_token}
        ):
            return _evidence_insufficient("invalid_event_token")
        sequences = pd.to_numeric(frame["seq"], errors="coerce")
        first_seq = evidence.get("first_seq")
        last_seq = evidence.get("last_seq")
        if (
            sequences.isna().any()
            or not isinstance(first_seq, int)
            or not isinstance(last_seq, int)
            or sorted(sequences.astype(int).tolist())
            != list(range(first_seq, last_seq + 1))
        ):
            return _evidence_insufficient("invalid_event_sequence")
        event_types = frame[event_field].astype(str)
        if bool(event_types.eq("run_start").any()) != bool(evidence.get("run_start")) or bool(
            event_types.eq("run_end").any()
        ) != bool(evidence.get("run_end")):
            return _evidence_insufficient("invalid_run_boundary")
        expected_start = details.get("expected_start")
        expected_end = details.get("expected_end")
        try:
            if expected_start and event_start.normalize() != pd.Timestamp(
                str(expected_start)
            ).normalize():
                return _evidence_insufficient("event_time_range_mismatch")
            if expected_end and event_end.normalize() != pd.Timestamp(
                str(expected_end)
            ).normalize():
                return _evidence_insufficient("event_time_range_mismatch")
        except (ValueError, TypeError):
            return _evidence_insufficient("event_time_range_mismatch")
        business_events = timestamps.loc[
            ~event_types.isin(("run_start", "run_end"))
        ].dt.normalize()
        source_start, source_end = (item.normalize() for item in source_range)
        if business_events.empty or (
            (business_events < source_start) | (business_events > source_end)
        ).any():
            return _evidence_insufficient("event_time_range_mismatch")
    return {
        "status": "available",
        "path": str(reference["path"]),
        "sha256": digest,
        "bytes": expected_bytes,
        "rows": physical_rows,
        "time_field": time_field,
        "event_field": event_field,
        "identity_fields": list(identity_fields),
        "reason_field": (
            "reason_code"
            if source.kind == "local_research" and "reason_code" in fields
            else "reason"
            if source.kind.startswith("joinquant_") and "reason" in fields
            else None
        ),
        "security_field": (
            "security"
            if "security" in fields
            else "etf"
            if "etf" in fields
            else None
        ),
        "time_range": {
            "start": event_start.isoformat(),
            "end": event_end.isoformat(),
        },
    }


def _capabilities(source: AnalysisSource) -> Mapping[str, Mapping[str, object]]:
    official_risk: dict[str, object] = {
        "status": "missing_at_source" if source.kind == "local_research" else "available",
        "source_only_extra_fields": [],
    }
    if source.kind == "joinquant_simulation":
        official_risk["source_only_extra_fields"] = [
            "intraday_return",
            "monthly_return",
        ]
    return {
        "common_facts": {"status": "available"},
        "official_risk": official_risk,
        "attribution": _attribution_capability(source),
        "cost_execution": {"status": "missing_at_source"},
    }


def _registered_source(root: Path, entry: Mapping[str, object]) -> RegisteredSource:
    source_root = _repository_path(root, entry["path"], "source path", directory=True)
    manifest_path = source_root / "manifest.json"
    if not manifest_path.is_file():
        raise SourceRegistryError("source manifest is missing")
    manifest_sha256 = str(entry["manifest_sha256"])
    if _sha256(manifest_path) != manifest_sha256:
        raise SourceRegistryError("source manifest digest does not match registration")
    snapshot_id = entry.get("snapshot_id")
    try:
        source = open_analysis_source(
            source_root,
            snapshot_id=str(snapshot_id) if snapshot_id is not None else None,
        )
    except AnalysisManifestError as exc:
        raise SourceRegistryError(str(exc)) from exc
    source_type = str(entry["source_type"])
    if source.kind != source_type:
        raise SourceRegistryError("declared source_type does not match manifest")
    return RegisteredSource(
        registration=SourceRegistration(
            scenario_id=str(entry["scenario_id"]),
            source_type=source_type,
            root=source_root,
            manifest_sha256=manifest_sha256,
            snapshot_id=str(snapshot_id) if snapshot_id is not None else None,
        ),
        source=source,
        capabilities=_capabilities(source),
    )


def load_source_registry(repo_root: Path, registry_path: Path) -> SourceRegistry:
    root = Path(repo_root).resolve()
    requested = Path(registry_path)
    if requested.is_absolute():
        try:
            requested = requested.resolve().relative_to(root)
        except ValueError as exc:
            raise SourceRegistryError(
                "source registry is missing or outside the repository"
            ) from exc
    path = _repository_path(
        root, requested.as_posix(), "source registry", directory=False
    )
    document = _load_json(path, "source registry")
    _validate_document(document)
    raw_sources = document["sources"]
    assert isinstance(raw_sources, list)
    sources = tuple(_registered_source(root, item) for item in raw_sources if isinstance(item, Mapping))
    if len(sources) != len(raw_sources):
        raise SourceRegistryError("source registry entries are invalid")
    scenario_ids = [item.registration.scenario_id for item in sources]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise SourceRegistryError("scenario_id values must be unique")
    baseline_scenario_id = str(document["baseline_scenario_id"])
    if baseline_scenario_id not in scenario_ids:
        raise SourceRegistryError("baseline_scenario_id is not registered")
    return SourceRegistry(
        path=path,
        sha256=_sha256(path),
        analysis_plan=_repository_path(root, document["analysis_plan"], "analysis_plan", directory=False),
        benchmark_manifest=_repository_path(
            root, document["benchmark_manifest"], "benchmark_manifest", directory=False
        ),
        baseline_scenario_id=baseline_scenario_id,
        sources=sources,
    )
