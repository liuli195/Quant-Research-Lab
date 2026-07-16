from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import shutil
import sys
import time
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Mapping

import numpy as np

from .result_adapter import (
    LocalExecutionFacts,
    materialize_execution_facts,
    parameter_document_digest,
    to_joinquant_facts,
)
from .vectorbt_engine import run_vectorbt_simulation


class PerformanceGateError(RuntimeError):
    """Raised when a single local scenario misses its execution gate."""


@dataclass(frozen=True)
class BenchmarkResult:
    facts: LocalExecutionFacts
    performance: Mapping[str, object]


def _canonical_bytes(value: object) -> bytes:
    def default(item: object) -> object:
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        if isinstance(item, np.generic):
            return item.item()
        raise TypeError(f"unsupported prepared input identity: {type(item).__name__}")

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=default,
    ).encode("utf-8")


def _prepared_inputs_digest(prepared_inputs: object) -> str:
    digest = hashlib.sha256()
    try:
        fields = vars(prepared_inputs)
    except TypeError as exc:
        raise PerformanceGateError("prepared inputs do not expose an identity") from exc
    for name in sorted(fields):
        value = fields[name]
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        if isinstance(value, np.ndarray):
            contiguous = np.ascontiguousarray(value)
            digest.update(str(contiguous.dtype).encode("ascii"))
            digest.update(_canonical_bytes(list(contiguous.shape)))
            digest.update(contiguous.tobytes())
        else:
            digest.update(_canonical_bytes(value))
    return digest.hexdigest()


def _environment() -> dict[str, object]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": sys.platform,
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ("vectorbt", "numba", "numpy", "pandas", "pyarrow")
        },
    }


def _run_once(
    *,
    prepared_inputs: object,
    config: Mapping[str, object],
    scenario_id: str,
    data_dir: Path,
) -> tuple[LocalExecutionFacts, str, float]:
    started = time.perf_counter()
    simulation = run_vectorbt_simulation(prepared_inputs, config)
    facts = to_joinquant_facts(prepared_inputs, simulation, scenario_id)
    result_digest = materialize_execution_facts(data_dir, facts)
    elapsed = time.perf_counter() - started
    return facts, result_digest, elapsed


def benchmark_scenario(
    *,
    prepared_inputs: object,
    config: Mapping[str, object],
    scenario_id: str,
    work_dir: Path,
    code_sha256: str,
    config_sha256: str,
    limit_seconds: float = 180.0,
) -> BenchmarkResult:
    if not isinstance(scenario_id, str) or not scenario_id:
        raise PerformanceGateError("scenario identity is missing")
    for value in (code_sha256, config_sha256):
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise PerformanceGateError("execution identity is invalid")
    if not np.isfinite(limit_seconds) or limit_seconds <= 0.0:
        raise PerformanceGateError("performance limit is invalid")
    root = Path(work_dir)
    if root.exists():
        raise PerformanceGateError("benchmark work directory already exists")
    root.mkdir(parents=True)
    cold_dir = root / "cold-data"
    warm_dir = root / "warm-data"
    try:
        cold_facts, cold_digest, cold_seconds = _run_once(
            prepared_inputs=prepared_inputs,
            config=config,
            scenario_id=scenario_id,
            data_dir=cold_dir,
        )
        _, warm_digest, warm_seconds = _run_once(
            prepared_inputs=prepared_inputs,
            config=config,
            scenario_id=scenario_id,
            data_dir=warm_dir,
        )
        if cold_digest != warm_digest:
            raise PerformanceGateError("cold and warm results are not deterministic")
        if cold_seconds > limit_seconds or warm_seconds > limit_seconds:
            raise PerformanceGateError(
                f"single scenario execution exceeded {limit_seconds:g} seconds"
            )
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise

    shutil.rmtree(cold_dir)
    shutil.rmtree(warm_dir)
    root.rmdir()
    cleanup = {
        "cold_temporary_result_removed": not cold_dir.exists(),
        "warm_temporary_result_removed": not warm_dir.exists(),
        "work_directory_removed": not root.exists(),
        "verified": not root.exists() and not cold_dir.exists() and not warm_dir.exists(),
    }
    if not cleanup["verified"]:
        raise PerformanceGateError("benchmark temporary artifacts could not be cleaned")
    params_sha256 = parameter_document_digest(config)
    scenario_sha256 = hashlib.sha256(
        _canonical_bytes({"scenario_id": scenario_id, "params_sha256": params_sha256})
    ).hexdigest()
    performance = {
        "schema_version": "local-backtest-performance/1",
        "status": "pass",
        "limit_seconds": float(limit_seconds),
        "environment": _environment(),
        "prepared_inputs_sha256": _prepared_inputs_digest(prepared_inputs),
        "code_sha256": code_sha256,
        "config_sha256": config_sha256,
        "params_sha256": params_sha256,
        "scenario": {"scenario_id": scenario_id, "sha256": scenario_sha256},
        "cold_seconds": float(cold_seconds),
        "warm_seconds": float(warm_seconds),
        "cold_result_sha256": cold_digest,
        "warm_result_sha256": warm_digest,
        "result_match": True,
        "cleanup": cleanup,
    }
    return BenchmarkResult(facts=cold_facts, performance=performance)
