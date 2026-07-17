from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from scripts.research.market_data.query import SnapshotView

from .contracts import ExecutionBundle, ResultExtension
from .evidence import execution_digest
from .performance import PerformanceEvidence, include_shared_work, run_cold_warm
from .result_package import ResultPackage, ResultPackageRequest, write_result_package
from .strategy_loader import LoadedStrategy
from .vectorbt_runtime import run_vectorbt


SCENARIO_STAGES = (
    "strategy_load",
    "strategy_prepare",
    "primary_vectorbt",
    "followup_prepare",
    "followup_vectorbt",
    "core_facts",
    "strategy_extensions",
    "parquet_materialize",
    "readback_validate",
    "report_and_manifest",
)


@dataclass(frozen=True, slots=True)
class ScenarioRequest:
    loaded_strategy: LoadedStrategy
    snapshot: SnapshotView
    scenario: Mapping[str, object]
    project_document: Mapping[str, object]
    run_id: str
    output_dir: Path
    code_identity: Mapping[str, object]
    market_snapshot: Mapping[str, object]
    runtime_lock: Mapping[str, object]
    environment: Mapping[str, object]
    strategy_load_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    package: ResultPackage
    performance: PerformanceEvidence
    stages: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class _EngineOutcome:
    execution: ExecutionBundle
    extensions: tuple[ResultExtension, ...]
    stages: Mapping[str, float]


def _timed(operation):
    started = time.perf_counter()
    value = operation()
    return value, time.perf_counter() - started


def _engine_once(request: ScenarioRequest) -> _EngineOutcome:
    module = request.loaded_strategy.module
    prepared, prepare_seconds = _timed(
        lambda: module.prepare(request.snapshot, request.scenario)
    )
    primary, primary_seconds = _timed(
        lambda: run_vectorbt(prepared.ledger_input, prepared.primary_program)
    )
    followup, followup_prepare_seconds = _timed(
        lambda: module.followup_program(prepared, primary)
    )
    followup_seconds = 0.0
    if followup is None:
        execution = ExecutionBundle(primary, primary, ("primary",))
    else:
        final, followup_seconds = _timed(
            lambda: run_vectorbt(prepared.ledger_input, followup)
        )
        execution = ExecutionBundle(primary, final, ("primary", "followup"))
    extensions, extension_seconds = _timed(
        lambda: module.build_extensions(prepared, execution)
    )
    return _EngineOutcome(
        execution=execution,
        extensions=extensions,
        stages={
            "strategy_prepare": prepare_seconds,
            "primary_vectorbt": primary_seconds,
            "followup_prepare": followup_prepare_seconds,
            "followup_vectorbt": followup_seconds,
            "strategy_extensions": extension_seconds,
        },
    )


def execute_scenario(request: ScenarioRequest) -> ScenarioOutcome:
    warm, performance = run_cold_warm(
        lambda: _engine_once(request),
        digest=lambda outcome: execution_digest(
            outcome.execution,
            outcome.extensions,
        ),
    )
    scenario_id = request.scenario.get("scenario_id")
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        raise ValueError("scenario_id is missing or invalid")
    stages = {name: 0.0 for name in SCENARIO_STAGES}
    stages["strategy_load"] = request.strategy_load_seconds
    stages.update(warm.stages)
    performance_document = performance.to_document()
    performance_document["stages"] = stages
    code_files = {
        source.relative_to(request.loaded_strategy.root).as_posix(): source
        for source in request.loaded_strategy.source_paths
    }
    package_request = ResultPackageRequest(
        strategy_id=request.loaded_strategy.descriptor.strategy_id,
        scenario_id=scenario_id,
        run_id=request.run_id,
        output_dir=request.output_dir,
        execution=warm.execution,
        extensions=warm.extensions,
        code_files=code_files,
        config_documents={
            "scenario.json": dict(request.scenario),
            "project-run.json": dict(request.project_document),
            "code-identity.json": dict(request.code_identity),
        },
        evidence_documents={
            "market-snapshot.json": dict(request.market_snapshot),
            "runtime-lock.json": dict(request.runtime_lock),
            "performance.json": performance_document,
            "environment.json": dict(request.environment),
        },
        performance_finalizer=lambda writer_stages, writer_measurement: {
            **include_shared_work(
                performance,
                request.strategy_load_seconds
                + writer_measurement["gate_measured_seconds"],
            ).to_document(),
            "stages": {**stages, **writer_stages},
        },
        atomic_publish=False,
    )
    package = write_result_package(package_request)
    stages.update(package.writer_stages)
    complete_performance = include_shared_work(
        performance,
        request.strategy_load_seconds + package.writer_seconds,
    )
    return ScenarioOutcome(package, complete_performance, stages)
