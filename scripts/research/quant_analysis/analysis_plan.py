from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


SCHEMA_PATH = Path(__file__).with_name("schemas") / "analysis-plan.schema.json"


class AnalysisPlanError(ValueError):
    """Raised when an analysis plan cannot be safely and deterministically expanded."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisPlanError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise AnalysisPlanError(f"{label} must be a JSON object")
    return value


def _resolve_plan_path(repo_root: Path, path: str | Path) -> Path:
    candidate = Path(path)
    return candidate.resolve() if candidate.is_absolute() else (repo_root / candidate).resolve()


def _resolve_repo_file(repo_root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AnalysisPlanError(f"{label} must be a repository-relative path")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AnalysisPlanError(f"{label} must stay inside the repository")
    resolved = (repo_root / candidate).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise AnalysisPlanError(f"{label} must stay inside the repository") from exc
    if not resolved.is_file():
        raise AnalysisPlanError(f"{label} does not exist: {value}")
    return resolved


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _validate_plan(document: dict[str, Any]) -> None:
    schema = _load_json(SCHEMA_PATH, label="analysis plan schema")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        paths = [
            ".".join(str(part) for part in error.absolute_path) or "$"
            for error in errors
        ]
        raise AnalysisPlanError(f"analysis plan schema validation failed at {paths}")


def expand_analysis_plan(repo_root: str | Path, plan_path: str | Path) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    plan_file = _resolve_plan_path(root, plan_path)
    plan = _load_json(plan_file, label="analysis plan")
    _validate_plan(plan)

    baseline_file = _resolve_repo_file(
        root,
        plan["baseline_config"],
        label="baseline_config",
    )
    baseline = _load_json(baseline_file, label="baseline config")
    if baseline.get("project_id") != plan["strategy_id"]:
        raise AnalysisPlanError("baseline project_id must match strategy_id")

    expected_universe = {
        item.get("security"): item.get("asset_group")
        for item in baseline.get("universe", [])
        if isinstance(item, dict)
    }
    if expected_universe != plan["universe"]:
        raise AnalysisPlanError("analysis plan universe must match baseline universe")

    scenarios = plan["scenarios"]
    scenario_ids = [scenario["scenario_id"] for scenario in scenarios]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise AnalysisPlanError("scenario_id values must be unique")
    if scenarios[0]["scenario_id"] != "baseline" or scenarios[0]["overrides"]:
        raise AnalysisPlanError("baseline must be first and have empty overrides")
    if plan["expected"]["scenario_runs"] != len(scenarios):
        raise AnalysisPlanError("expected.scenario_runs must match scenarios")

    bootstrap = plan["analyses"]["bootstrap"]
    if plan["expected"]["bootstrap_paths"] != bootstrap.get("paths"):
        raise AnalysisPlanError("expected.bootstrap_paths must match analyses.bootstrap.paths")
    if plan["expected"]["seed"] != bootstrap.get("seed"):
        raise AnalysisPlanError("expected.seed must match analyses.bootstrap.seed")

    expanded_scenarios: list[dict[str, Any]] = []
    for scenario in scenarios:
        params = _deep_merge(baseline, scenario["overrides"])
        params["scenario_id"] = scenario["scenario_id"]
        expanded_scenarios.append(
            {
                "scenario_id": scenario["scenario_id"],
                "dimension": scenario["dimension"],
                "overrides": copy.deepcopy(scenario["overrides"]),
                "params": params,
                "params_sha256": _sha256(params),
            }
        )

    return {
        "schema_version": "analysis-scenarios/1",
        "strategy_id": plan["strategy_id"],
        "analysis_plan": plan_file.relative_to(root).as_posix()
        if plan_file.is_relative_to(root)
        else str(plan_file),
        "analysis_plan_sha256": _sha256(plan),
        "baseline_config": baseline_file.relative_to(root).as_posix(),
        "baseline_config_sha256": _sha256(baseline),
        "expected": copy.deepcopy(plan["expected"]),
        "universe": copy.deepcopy(plan["universe"]),
        "analyses": copy.deepcopy(plan["analyses"]),
        "thresholds": copy.deepcopy(plan["thresholds"]),
        "scenarios": expanded_scenarios,
    }


def write_analysis_scenarios(document: dict[str, Any], output_path: str | Path) -> None:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_bytes(document) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()
