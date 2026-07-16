from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .result_adapter import LocalResultPackage, write_local_result
from .vectorbt_benchmark import benchmark_scenario


_FORBIDDEN_FLOW_FIELDS = {"candidates", "scenarios", "analysis_plan"}
_SCENARIO_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class SingleScenarioError(ValueError):
    """Raised when a project request attempts more than one local scenario."""


@dataclass(frozen=True)
class SingleScenarioOutcome:
    scenario_id: str
    local_backtest_id: str
    result_path: Path
    next_action: str = "return_to_caller"


def validate_single_scenario_config(config: Mapping[str, object]) -> str:
    if not isinstance(config, Mapping):
        raise SingleScenarioError("single scenario config must be an object")
    if set(config) & _FORBIDDEN_FLOW_FIELDS:
        raise SingleScenarioError("single scenario config cannot contain batch or analysis inputs")
    scenario_id = config.get("scenario_id")
    if not isinstance(scenario_id, str) or _SCENARIO_ID.fullmatch(scenario_id) is None:
        raise SingleScenarioError("single scenario config requires scenario_id")
    if config.get("project_id") != "strategy-003" or config.get("schema_version") != 1:
        raise SingleScenarioError("single scenario project identity is invalid")
    return scenario_id


def execute_prepared_scenario(
    *,
    prepared_inputs: object,
    config: Mapping[str, object],
    output_dir: Path,
    run_id: str,
    snapshot_id: str,
    code_sha256: str,
    config_sha256: str,
    code_path: Path,
) -> SingleScenarioOutcome:
    scenario_id = validate_single_scenario_config(config)
    if len(config_sha256) != 64:
        raise SingleScenarioError("config_sha256 is invalid")
    local_backtest_id = f"local-{scenario_id}"
    output_root = Path(output_dir)
    benchmark = benchmark_scenario(
        prepared_inputs=prepared_inputs,
        config=config,
        scenario_id=scenario_id,
        work_dir=output_root / ".benchmark-work",
        code_sha256=code_sha256,
        config_sha256=config_sha256,
    )
    target = Path(output_dir) / "backtests" / local_backtest_id
    package: LocalResultPackage = write_local_result(
        target,
        facts=benchmark.facts,
        run_id=run_id,
        local_backtest_id=local_backtest_id,
        scenario_id=scenario_id,
        snapshot_id=snapshot_id,
        corporate_actions_sha256=str(
            getattr(prepared_inputs, "corporate_actions_digest", "")
        ),
        code_path=code_path,
        params=config,
        performance=benchmark.performance,
    )
    return SingleScenarioOutcome(
        scenario_id=scenario_id,
        local_backtest_id=local_backtest_id,
        result_path=package.root,
    )


def write_project_status(
    output_dir: Path,
    *,
    status: str,
    reason_codes: Sequence[str],
    next_action: str | None = None,
) -> Path:
    if status not in {"complete", "evidence_insufficient", "failed"}:
        raise SingleScenarioError("project status is invalid")
    if status == "complete" and reason_codes:
        raise SingleScenarioError("complete project status cannot contain reasons")
    if next_action is not None and (
        status != "complete" or next_action != "return_to_caller"
    ):
        raise SingleScenarioError("single scenario next action is invalid")
    document: dict[str, object] = {
        "schema_version": 1,
        "status": status,
        "reason_codes": list(reason_codes),
    }
    if next_action is not None:
        document["next_action"] = next_action
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "project-status.json"
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path
