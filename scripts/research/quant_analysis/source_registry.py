from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

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


def _attribution_status(source: AnalysisSource) -> str:
    if source.kind == "local_research":
        extensions = source.manifest.get("extensions")
        return "declared" if isinstance(extensions, Mapping) and "attribution_log" in extensions else "missing_at_source"
    datasets = source.manifest.get("datasets")
    entry = datasets.get("attribution_log") if isinstance(datasets, Mapping) else None
    return "declared" if isinstance(entry, Mapping) and entry.get("status") == "complete" else "missing_at_source"


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
        "attribution": {"status": _attribution_status(source)},
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
