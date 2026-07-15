from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .analysis_plan import expand_analysis_plan
from .evidence import evidence_digest


_SHA256 = re.compile(r"[0-9a-f]{64}")
_FORMULA_VERSION = "strategy-analysis-preparation/1"


class AnalysisOrchestrationError(ValueError):
    """Raised when independent analysis inputs cannot be prepared safely."""


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisOrchestrationError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise AnalysisOrchestrationError(f"{label} must be a JSON object")
    return value


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def build_scenario_run_documents(
    expanded_plan: Mapping[str, object],
    run_template: Mapping[str, object],
    *,
    preparation_id: str,
) -> list[dict[str, object]]:
    if _SHA256.fullmatch(preparation_id) is None:
        raise AnalysisOrchestrationError("preparation_id must be a SHA256 digest")
    scenarios = expanded_plan.get("scenarios")
    if not isinstance(scenarios, Sequence) or isinstance(scenarios, (str, bytes)):
        raise AnalysisOrchestrationError("expanded plan scenarios are invalid")
    if run_template.get("project_id") != expanded_plan.get("strategy_id"):
        raise AnalysisOrchestrationError("run template project_id does not match strategy")

    documents: list[dict[str, object]] = []
    for raw in scenarios:
        if not isinstance(raw, Mapping):
            raise AnalysisOrchestrationError("expanded scenario is invalid")
        scenario_id = str(raw.get("scenario_id", ""))
        params = raw.get("params")
        if not scenario_id or not isinstance(params, Mapping):
            raise AnalysisOrchestrationError("expanded scenario identity is invalid")
        run_config = copy.deepcopy(dict(run_template))
        run_config["project_config"] = (
            f".local/strategy-analysis-preparations/{preparation_id}/scenario-configs/"
            f"{scenario_id}/params.json"
        )
        run_config["required_outputs"] = [
            {"path": f"backtests/local-{scenario_id}", "format": "directory"}
        ]
        documents.append(
            {
                "scenario_id": scenario_id,
                "dimension": str(raw.get("dimension", "")),
                "params_sha256": str(raw.get("params_sha256", "")),
                "params": copy.deepcopy(dict(params)),
                "run_config": run_config,
            }
        )
    if len({item["scenario_id"] for item in documents}) != len(documents):
        raise AnalysisOrchestrationError("scenario_id values must be unique")
    return documents


def prepare_analysis_workspace(
    repo_root: Path,
    *,
    plan_path: Path,
    run_template_path: Path,
    benchmark_set_id: str,
) -> dict[str, object]:
    root = Path(repo_root).resolve()
    if _SHA256.fullmatch(benchmark_set_id) is None:
        raise AnalysisOrchestrationError("benchmark_set_id is invalid")
    expanded = expand_analysis_plan(root, plan_path)
    template_path = (
        run_template_path.resolve()
        if run_template_path.is_absolute()
        else (root / run_template_path).resolve()
    )
    if not template_path.is_relative_to(root):
        raise AnalysisOrchestrationError("run template must stay inside the repository")
    template = _load_json(template_path, label="run template")
    template_sha256 = hashlib.sha256(template_path.read_bytes()).hexdigest()

    benchmark_root = root / ".local" / "market-data" / "benchmark-sets" / benchmark_set_id
    benchmark_manifest_path = benchmark_root / "manifest.json"
    benchmark_manifest = _load_json(benchmark_manifest_path, label="benchmark set manifest")
    if benchmark_manifest.get("benchmark_set_id") != benchmark_set_id:
        raise AnalysisOrchestrationError("benchmark set identity mismatch")
    data = benchmark_manifest.get("data")
    if not isinstance(data, Mapping):
        raise AnalysisOrchestrationError("benchmark set data declaration is invalid")
    data_relative = Path(str(data.get("path", "")))
    if data_relative.is_absolute() or ".." in data_relative.parts:
        raise AnalysisOrchestrationError("benchmark data path is unsafe")
    data_path = (benchmark_root / data_relative).resolve()
    if not data_path.is_file() or not data_path.is_relative_to(benchmark_root):
        raise AnalysisOrchestrationError("benchmark data is missing")
    data_sha256 = hashlib.sha256(data_path.read_bytes()).hexdigest()
    if data_sha256 != data.get("sha256"):
        raise AnalysisOrchestrationError("benchmark data digest mismatch")

    preparation_id = evidence_digest(
        {
            "formula_version": _FORMULA_VERSION,
            "analysis_plan_sha256": expanded["analysis_plan_sha256"],
            "baseline_config_sha256": expanded["baseline_config_sha256"],
            "run_template_sha256": template_sha256,
            "benchmark_set_id": benchmark_set_id,
            "benchmark_data_sha256": data_sha256,
        }
    )
    documents = build_scenario_run_documents(
        expanded,
        template,
        preparation_id=preparation_id,
    )
    workspace = (
        root / ".local" / "strategy-analysis-preparations" / preparation_id
    )
    _atomic_json(workspace / "analysis-scenarios.json", expanded)
    config_paths: list[str] = []
    for document in documents:
        scenario_id = str(document["scenario_id"])
        scenario_root = workspace / "scenario-configs" / scenario_id
        _atomic_json(scenario_root / "params.json", document["params"])
        _atomic_json(scenario_root / "run.json", document["run_config"])
        config_paths.append(
            (scenario_root / "run.json").relative_to(root).as_posix()
        )
    preparation = {
        "schema_version": "strategy-analysis-preparation/1",
        "preparation_id": preparation_id,
        "formula_version": _FORMULA_VERSION,
        "analysis_scenarios": (
            workspace / "analysis-scenarios.json"
        ).relative_to(root).as_posix(),
        "run_template": {
            "path": template_path.relative_to(root).as_posix(),
            "sha256": template_sha256,
        },
        "benchmark_set": {
            "benchmark_set_id": benchmark_set_id,
            "manifest": benchmark_manifest_path.relative_to(root).as_posix(),
            "data_sha256": data_sha256,
        },
        "scenario_run_configs": config_paths,
        "expected_scenario_runs": len(documents),
        "next_action": "invoke_each_scenario_once",
    }
    _atomic_json(workspace / "preparation.json", preparation)
    return preparation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare independent strategy analysis runs")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--run-template", type=Path, required=True)
    parser.add_argument("--benchmark-set-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = prepare_analysis_workspace(
        args.repo_root,
        plan_path=args.plan,
        run_template_path=args.run_template,
        benchmark_set_id=args.benchmark_set_id,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
