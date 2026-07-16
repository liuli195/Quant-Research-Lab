from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from scripts.research.local_quant_research.contracts import ExecutionRun
from scripts.research.local_quant_research.strategy_loader import load_strategy
from scripts.research.market_data.query import SnapshotView


def _snapshot() -> SnapshotView:
    return SnapshotView(
        snapshot_id="a" * 64,
        fields=(),
        rows=(),
        digest="b" * 64,
        corporate_action_fields=(),
        corporate_actions=(),
        corporate_actions_digest="c" * 64,
    )


@pytest.mark.parametrize(
    "strategy_root",
    (
        "tests/local_quant_research/fixtures/minimal_strategy",
        "tests/local_quant_research/fixtures/minimal_strategy_b",
    ),
)
def test_minimal_no_order_strategy_runs_through_shared_vectorbt_runtime(
    strategy_root: str,
    repo_root: Path,
) -> None:
    try:
        runtime = importlib.import_module(
            "scripts.research.local_quant_research.vectorbt_runtime"
        )
    except ModuleNotFoundError:
        pytest.fail("shared vectorbt runtime is missing")
    loaded = load_strategy(
        repo_root,
        {"root": strategy_root, "module": "strategy", "symbol": "MODULE"},
    )
    prepared = loaded.module.prepare(_snapshot(), {})

    result = runtime.run_vectorbt(
        prepared.ledger_input,
        prepared.primary_program,
    )

    assert isinstance(result, ExecutionRun)
    value = result.ledger.value
    assert value.dtype.names == (
        "time",
        "total_value",
        "returns",
        "benchmark_returns",
    )
    assert value.shape == (2,)
    assert value.flags.writeable is False
    assert result.ledger.value is value
    assert np.array_equal(value["total_value"], np.array([100_000.0, 100_000.0]))


def test_ledger_and_trace_public_arrays_are_cached_readonly_and_hide_portfolio(
    repo_root: Path,
) -> None:
    runtime = importlib.import_module(
        "scripts.research.local_quant_research.vectorbt_runtime"
    )
    loaded = load_strategy(
        repo_root,
        {
            "root": "tests/local_quant_research/fixtures/minimal_strategy",
            "module": "strategy",
            "symbol": "MODULE",
        },
    )
    prepared = loaded.module.prepare(_snapshot(), {})
    program = replace(
        prepared.primary_program,
        trace={"state": np.array([1.0, 2.0])},
    )

    result = runtime.run_vectorbt(prepared.ledger_input, program)

    public_arrays = (
        "orders",
        "assets",
        "cash",
        "value",
        "trades",
        "positions",
        "returns",
    )
    for name in public_arrays:
        first = getattr(result.ledger, name)
        assert first.flags.writeable is False
        assert getattr(result.ledger, name) is first
    trace = result.trace["state"]
    assert trace.flags.writeable is False
    assert result.trace["state"] is trace
    assert {
        name for name in dir(result.ledger) if not name.startswith("_")
    } == set(public_arrays)
    assert not hasattr(result.ledger, "portfolio")
