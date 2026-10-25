from __future__ import annotations

import gc
import importlib
import weakref
from pathlib import Path

import pytest


LEGACY_RUN_FIELDS = (
    "command",
    "project_entry",
    "code_identity",
    "required_outputs",
    "output_root",
    "stop_states",
)


def test_parent_generates_one_fixed_private_execute_command() -> None:
    tmp_path = Path("unused-execution-root")
    from scripts.research.local_quant_research import runner

    execution_root = tmp_path / "inputs"
    staging = tmp_path / "staging"

    command = runner._execute_command(
        repo_root=tmp_path,
        execution_root=execution_root,
        staging=staging,
    )

    assert command == (
        tmp_path / ".venv/Scripts/python.exe",
        tmp_path / "scripts/research/local_quant_research/cli.py",
        "_execute",
        "--frozen-inputs",
        execution_root / "request.json",
        "--staging",
        staging,
    )


def _performance_module():
    try:
        return importlib.import_module(
            "scripts.research.local_quant_research.performance"
        )
    except ModuleNotFoundError:
        pytest.fail("shared performance module is missing")


def test_daily_performance_runs_exactly_one_cold_and_one_warm() -> None:
    performance = _performance_module()
    calls: list[int] = []

    def operation() -> dict[str, object]:
        calls.append(len(calls))
        return {"execution": "same"}

    outcome, evidence = performance.run_cold_warm(
        operation,
        digest=lambda value: value["execution"],
    )

    assert outcome == {"execution": "same"}
    assert calls == [0, 1]
    assert evidence.cold.digest == evidence.warm.digest == "same"
    assert evidence.cold.seconds < 180
    assert evidence.warm.seconds < 180


def test_daily_performance_releases_cold_outcome_before_warm() -> None:
    performance = _performance_module()
    references: list[weakref.ReferenceType[object]] = []

    class Outcome:
        pass

    def operation() -> Outcome:
        if references:
            gc.collect()
            assert references[0]() is None
        outcome = Outcome()
        references.append(weakref.ref(outcome))
        return outcome

    performance.run_cold_warm(
        operation,
        digest=lambda _value: "same",
    )


def test_daily_performance_rejects_full_execution_digest_mismatch() -> None:
    performance = _performance_module()
    calls = 0

    def operation() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"execution": calls}

    with pytest.raises(performance.PerformanceGateError) as caught:
        performance.run_cold_warm(
            operation,
            digest=lambda value: str(value["execution"]),
        )

    assert caught.value.code == "execution_digest_mismatch"
    assert calls == 2


def test_daily_performance_counts_digest_time_in_each_180_second_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    performance = _performance_module()
    clock = 0.0

    def now() -> float:
        return clock

    def slow_digest(value: str) -> str:
        nonlocal clock
        clock = 180.0
        return value

    monkeypatch.setattr(performance.time, "perf_counter", now)

    with pytest.raises(performance.PerformanceGateError) as caught:
        performance.run_cold_warm(lambda: "same", digest=slow_digest)

    assert caught.value.code == "cold_performance_limit"


@pytest.mark.parametrize(
    ("clock", "reason"),
    (
        ([0.0, 180.0, 180.0, 181.0], "cold_performance_limit"),
        ([0.0, 1.0, 1.0, 181.0], "warm_performance_limit"),
    ),
)
def test_daily_performance_rejects_each_180_second_limit(
    clock: list[float],
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    performance = _performance_module()
    values = iter(clock)
    monkeypatch.setattr(performance.time, "perf_counter", lambda: next(values))

    with pytest.raises(performance.PerformanceGateError) as caught:
        performance.run_cold_warm(lambda: "same", digest=lambda value: value)

    assert caught.value.code == reason


def test_daily_performance_adds_single_writer_duration_to_both_limits() -> None:
    performance = _performance_module()
    _, evidence = performance.run_cold_warm(
        lambda: "same",
        digest=lambda value: value,
    )

    with pytest.raises(performance.PerformanceGateError) as caught:
        performance.include_shared_work(evidence, 180.0)

    assert caught.value.code == "cold_performance_limit"


def test_public_cli_omits_release_performance_workflow() -> None:
    from scripts.research.local_quant_research import cli

    assert "performance" not in cli._parser().format_help()
