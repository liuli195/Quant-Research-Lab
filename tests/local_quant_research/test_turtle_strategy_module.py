from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import numpy as np

from scripts.research.local_quant_research.contracts import ExecutionBundle
from scripts.research.local_quant_research.vectorbt_runtime import run_vectorbt
from scripts.research.market_data.economic_returns import (
    canonical_corporate_actions_digest,
)
from scripts.research.market_data.query import SnapshotView
from scripts.research.local_quant_research.strategy_loader import (
    discover_strategy_sources,
)


RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))


def test_turtle_package_has_one_public_strategy_symbol() -> None:
    turtle_etf = importlib.import_module("turtle_etf")
    strategy = importlib.import_module("turtle_etf.strategy")
    module = strategy.MODULE

    assert module.descriptor.strategy_id == "strategy-003"
    assert module.descriptor.extension_names == ("turtle_etf",)
    assert set(turtle_etf.__all__) == {"MODULE"}
    assert turtle_etf.MODULE is module
    assert callable(module.prepare)
    assert callable(module.followup_program)
    assert callable(module.build_extensions)


def test_turtle_private_strategy_sources_do_not_import_vectorbt() -> None:
    root = RESEARCH_ROOT / "turtle_etf"
    for name in ("strategy.py", "_kernel.py", "_attribution.py", "_delayed.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports |= {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert all(not item.startswith("vectorbt") for item in imports)


def test_attribution_module_does_not_define_a_second_core_result_adapter() -> None:
    tree = ast.parse(
        (RESEARCH_ROOT / "turtle_etf/_attribution.py").read_text(encoding="utf-8")
    )
    definitions = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    }

    assert {
        "LocalExecutionFacts",
        "to_joinquant_facts",
        "_validate_common_facts",
        "validate_turtle_attribution",
    }.isdisjoint(definitions)


def test_turtle_source_identity_includes_private_implementation() -> None:
    sources = discover_strategy_sources(RESEARCH_ROOT, "turtle_etf.strategy")
    relative = {path.relative_to(RESEARCH_ROOT).as_posix() for path in sources}

    assert {
        "turtle_etf/strategy.py",
        "turtle_etf/_kernel.py",
        "turtle_etf/_attribution.py",
        "turtle_etf/_delayed.py",
    }.issubset(relative)
    strategy = importlib.import_module("turtle_etf.strategy")
    assert not hasattr(strategy.MODULE.descriptor, "source_files")


def _snapshot() -> SnapshotView:
    closes = (10.0, 12.0, 13.0, 14.0)
    rows = tuple(
        {
            "date": f"2026-01-{5 + index:02d}",
            "security": "ETF-A",
            "open": close + 0.25,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "pre_close": closes[max(index - 1, 0)],
            "volume": 1_000_000.0,
            "money": 10_000_000.0,
            "factor": 1.0,
            "paused": False,
            "high_limit": close + 2.0,
            "low_limit": close - 2.0,
        }
        for index, close in enumerate(closes)
    )
    return SnapshotView(
        snapshot_id="snapshot-test",
        fields=tuple(rows[0]),
        rows=rows,
        digest="1" * 64,
        corporate_action_fields=(),
        corporate_actions=(),
        corporate_actions_digest=canonical_corporate_actions_digest(()),
    )


def _config(*, delay_days: int = 0) -> dict[str, object]:
    return {
        "scenario_id": "module-test",
        "universe": [{"security": "ETF-A", "asset_group": "equity"}],
        "signal": {
            "entry_days": 1,
            "exit_days": 1,
            "n_days": 1,
            "add_step_n": 0.5,
            "stop_n": 2.0,
            "max_units": 4,
        },
        "risk": {
            "lot_size": 100,
            "unit_risk_per_n": 0.01,
            "asset_group_unit_cap": 6.0,
            "portfolio_unit_cap": 12.0,
        },
        "costs": {"commission_multiplier": 1.0, "one_way_slippage": 0.0},
        "research": {"initial_cash": 100_000.0},
        "execution": {"additional_delay_days": delay_days},
    }


def test_turtle_module_executes_immediate_orders_through_shared_runtime() -> None:
    module = importlib.import_module("turtle_etf.strategy").MODULE
    prepared = module.prepare(_snapshot(), _config())

    primary = run_vectorbt(prepared.ledger_input, prepared.primary_program)

    assert primary.trace["action_codes"][:, 0].tolist() == [0, 0, 3, 0]
    assert len(primary.ledger.orders) == 1
    assert primary.ledger.orders["filled"].tolist() == [300]
    assert primary.ledger.orders["time"].tolist() == ["2026-01-07T09:30:00"]
    assert primary.ledger.orders["security"].tolist() == ["ETF-A"]
    assert np.all(primary.ledger.orders["status"] == "done")


def test_turtle_module_replays_delayed_plan_through_same_runtime() -> None:
    module = importlib.import_module("turtle_etf.strategy").MODULE
    prepared = module.prepare(_snapshot(), _config(delay_days=1))
    primary = run_vectorbt(prepared.ledger_input, prepared.primary_program)

    followup = module.followup_program(prepared, primary)
    assert followup is not None
    final = run_vectorbt(prepared.ledger_input, followup)
    extension = module.build_extensions(
        prepared,
        ExecutionBundle(primary, final, ("primary", "followup")),
    )[0]

    assert final.trace["planned_row_indices"][:, 0].tolist() == [-1, -1, -1, 2]
    assert len(final.ledger.orders) == 1
    assert final.ledger.orders["filled"].tolist() == [300]
    assert final.ledger.orders["time"].tolist() == ["2026-01-08T09:30:00"]
    assert extension.name == "turtle_etf"
    assert extension.table["event_type"].to_pylist() == ["decision", "valuation"]


def test_strategy_trace_does_not_mirror_vectorbt_account_facts() -> None:
    module = importlib.import_module("turtle_etf.strategy").MODULE
    prepared = module.prepare(_snapshot(), _config(delay_days=1))
    primary = run_vectorbt(prepared.ledger_input, prepared.primary_program)
    followup = module.followup_program(prepared, primary)
    assert followup is not None
    final = run_vectorbt(prepared.ledger_input, followup)

    forbidden = {
        "filled_quantities",
        "fill_prices",
        "fees",
        "state_quantities",
        "day_equity",
    }
    assert forbidden.isdisjoint(primary.trace)
    assert forbidden.isdisjoint(final.trace)


def test_segment_runtime_uses_prepare_time_scratch_buffers() -> None:
    tree = ast.parse(
        (RESEARCH_ROOT / "turtle_etf/_kernel.py").read_text(encoding="utf-8")
    )
    runtime_functions = {
        "prepare_segment_nb",
        "_risk_scales_into_nb",
        "_targets_for_scale_into_nb",
        "_cash_feasible_targets_nb",
    }
    forbidden = {"zeros", "ones", "full", "array", "asarray", "copy"}
    calls: set[str] = set()
    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in runtime_functions
    ):
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if isinstance(call.func, ast.Attribute):
                calls.add(call.func.attr)

    assert forbidden.isdisjoint(calls)
