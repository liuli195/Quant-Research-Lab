from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd
import pyarrow.parquet as pq

from scripts.research.result_package import ResultContractError, validate_result_package


class PackageSourceError(ValueError):
    """Raised when a result package cannot prove its analysis identity."""


@dataclass(frozen=True)
class PackageSource:
    root: Path
    manifest_sha256: str
    content_sha256: str
    strategy_id: str
    scenario_id: str
    params: Mapping[str, object]
    capabilities: Mapping[str, Mapping[str, object]]


_ATTRIBUTION_FIELDS = {"time", "event_id", "event_type"}
_OPTIONAL_ATTRIBUTION_FIELDS = (
    "scope",
    "security",
    "reason_code",
    "requested_amount",
    "executed_amount",
    "reference_price",
    "risk_before",
    "risk_after",
    "details_json",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageSourceError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise PackageSourceError(f"{label} must be an object")
    return value


def _source_result_range(root: Path) -> tuple[pd.Timestamp, pd.Timestamp]:
    path = root / "data" / "results.parquet"
    values = pd.to_datetime(
        pq.read_table(path, columns=["time"]).to_pandas()["time"],
        errors="coerce",
        format="mixed",
    )
    if values.empty or values.isna().any():
        raise PackageSourceError("result package has invalid result times")
    return pd.Timestamp(values.min()).normalize(), pd.Timestamp(values.max()).normalize()


def _attribution_capability(
    root: Path, manifest: Mapping[str, object]
) -> Mapping[str, object]:
    extensions = manifest.get("extensions")
    if not isinstance(extensions, Mapping):
        return {"status": "missing_at_source"}
    candidates: list[tuple[Mapping[str, object], set[str]]] = []
    for entry in extensions.values():
        if not isinstance(entry, Mapping) or entry.get("status") != "complete":
            continue
        schema = entry.get("schema")
        if not isinstance(schema, list):
            continue
        fields = {
            str(field.get("name"))
            for field in schema
            if isinstance(field, Mapping) and isinstance(field.get("name"), str)
        }
        if _ATTRIBUTION_FIELDS.issubset(fields):
            candidates.append((entry, fields))
    if not candidates:
        return {"status": "missing_at_source"}
    if len(candidates) != 1:
        return {"status": "evidence_insufficient", "reason": "ambiguous_attribution_extension"}
    entry, fields = candidates[0]
    files = entry.get("files")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], Mapping):
        return {"status": "evidence_insufficient", "reason": "invalid_file_declaration"}
    reference = files[0]
    relative = reference.get("path")
    if not isinstance(relative, str):
        return {"status": "evidence_insufficient", "reason": "invalid_source_path"}
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return {"status": "evidence_insufficient", "reason": "invalid_source_path"}
    try:
        frame = pq.read_table(path, columns=["time", "event_id", "event_type"]).to_pandas()
        timestamps = pd.to_datetime(frame["time"], errors="coerce", format="mixed")
    except Exception:
        return {"status": "evidence_insufficient", "reason": "invalid_parquet"}
    event_ids = frame["event_id"].fillna("").astype(str)
    event_types = frame["event_type"].fillna("").astype(str)
    if (
        frame.empty
        or timestamps.isna().any()
        or event_ids.eq("").any()
        or event_ids.duplicated().any()
        or event_types.eq("").any()
    ):
        return {"status": "evidence_insufficient", "reason": "invalid_event_identity"}
    source_start, source_end = _source_result_range(root)
    event_days = timestamps.dt.normalize()
    if ((event_days < source_start) | (event_days > source_end)).any():
        return {"status": "evidence_insufficient", "reason": "event_time_range_mismatch"}
    return {
        "status": "available",
        "path": relative,
        "sha256": str(reference["sha256"]),
        "rows": len(frame),
        "detail_fields": [name for name in _OPTIONAL_ATTRIBUTION_FIELDS if name in fields],
        "time_range": {
            "start": pd.Timestamp(timestamps.min()).isoformat(),
            "end": pd.Timestamp(timestamps.max()).isoformat(),
        },
    }


def _capabilities(
    root: Path, manifest: Mapping[str, object]
) -> Mapping[str, Mapping[str, object]]:
    return {
        "common_facts": {"status": "available"},
        "official_risk": {"status": "missing_at_source"},
        "attribution": _attribution_capability(root, manifest),
    }


def open_package_source(path: Path) -> PackageSource:
    root = Path(path).resolve()
    try:
        manifest = validate_result_package(root)
    except ResultContractError as exc:
        raise PackageSourceError(str(exc)) from exc
    identity = manifest.get("object")
    content_sha256 = manifest.get("package_sha256")
    if (
        not isinstance(identity, Mapping)
        or identity.get("status") != "complete"
        or not isinstance(identity.get("strategy_id"), str)
        or not isinstance(identity.get("scenario_id"), str)
        or not isinstance(content_sha256, str)
    ):
        raise PackageSourceError("source is not a self-identifying standard result package")
    params = _load_json(root / "config" / "scenario.json", "package scenario")
    if params.get("scenario_id") != identity["scenario_id"]:
        raise PackageSourceError("package scenario identity does not match its frozen config")
    return PackageSource(
        root=root,
        manifest_sha256=_sha256(root / "manifest.json"),
        content_sha256=content_sha256,
        strategy_id=str(identity["strategy_id"]),
        scenario_id=str(identity["scenario_id"]),
        params=params,
        capabilities=_capabilities(root, manifest),
    )


def open_package_sources(paths: Sequence[Path]) -> tuple[PackageSource, ...]:
    if not paths:
        raise PackageSourceError("at least one explicit result package is required")
    packages = tuple(open_package_source(path) for path in paths)
    scenario_ids = [package.scenario_id for package in packages]
    content_ids = [package.content_sha256 for package in packages]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise PackageSourceError("result package scenario identities must be unique")
    if len(content_ids) != len(set(content_ids)):
        raise PackageSourceError("the same result package cannot be analyzed twice")
    return packages
