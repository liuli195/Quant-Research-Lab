from __future__ import annotations

import dataclasses
import inspect
import shutil
import sys
import uuid
from pathlib import Path
from typing import get_type_hints

import numpy as np
import pyarrow as pa
import pytest

from scripts.research.local_quant_research.contracts import (
    FILL_ACCEPTED,
    FILL_IGNORED,
    FILL_REJECTED,
    SIDE_BUY,
    SIDE_NONE,
    SIDE_SELL,
    ExecutionBundle,
    ExecutionLedger,
    ExecutionRun,
    FillEvent,
    LedgerInput,
    OrderBuffer,
    OrderProgram,
    PreparedStrategy,
    ResultExtension,
    SegmentView,
    StrategyDescriptor,
    StrategyEvidenceError,
    StrategyModule,
)
from scripts.research.local_quant_research.strategy_loader import (
    ConfigurationError,
    LoadedStrategy,
    load_strategy,
)


def _fixture_config(name: str) -> dict[str, str]:
    if name == "minimal_strategy_b":
        return {
            "root": "tests/local_quant_research/fixtures",
            "module": "minimal_strategy_b.strategy",
            "symbol": "MODULE",
        }
    return {
        "root": f"tests/local_quant_research/fixtures/{name}",
        "module": "strategy",
        "symbol": "MODULE",
    }


@pytest.fixture
def temporary_strategy_root(repo_root: Path):
    root = repo_root / ".local" / "strategy-contract-tests" / uuid.uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
        for parent in (root.parent, root.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                pass


def test_contract_constants_and_namedtuple_fields_are_stable() -> None:
    assert (SIDE_NONE, SIDE_BUY, SIDE_SELL) == (0, 1, -1)
    assert (FILL_IGNORED, FILL_ACCEPTED, FILL_REJECTED) == (0, 1, 2)
    assert SegmentView._fields == (
        "row",
        "group",
        "from_col",
        "to_col",
        "cash",
        "value",
        "positions",
        "valuation_prices",
    )
    assert FillEvent._fields == (
        "row",
        "column",
        "status",
        "side",
        "size",
        "price",
        "fees",
        "cash_after",
        "position_after",
    )


def test_new_contract_dataclasses_are_frozen_and_slotted() -> None:
    descriptor = StrategyDescriptor(
        strategy_id="example",
        contract_version="1",
        extension_names=(),
        accounting={"basis": "cash"},
    )

    assert not hasattr(descriptor, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        descriptor.strategy_id = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        descriptor.accounting["basis"] = "other"  # type: ignore[index]


def test_order_buffer_requires_equal_array_lengths_and_remains_writable() -> None:
    arrays = [np.zeros(2) for _ in range(8)]
    buffer = OrderBuffer(*arrays)

    buffer.enabled[0] = 1
    assert buffer.enabled.flags.writeable
    assert buffer.enabled[0] == 1

    arrays[-1] = np.zeros(1)
    with pytest.raises(ValueError, match="same length"):
        OrderBuffer(*arrays)


def test_strategy_evidence_error_preserves_stable_code() -> None:
    error = StrategyEvidenceError("missing_declared_input", "input is missing")

    assert error.code == "missing_declared_input"
    assert str(error) == "input is missing"


def test_contract_shapes_and_strategy_protocol_are_exact() -> None:
    assert [field.name for field in dataclasses.fields(StrategyDescriptor)] == [
        "strategy_id",
        "contract_version",
        "extension_names",
        "accounting",
    ]
    assert [field.name for field in dataclasses.fields(LedgerInput)] == [
        "dates",
        "symbols",
        "close",
        "initial_cash",
        "group_ids",
        "cash_sharing",
        "frequency",
    ]
    assert [field.name for field in dataclasses.fields(OrderProgram)] == [
        "program_id",
        "prepare_segment_nb",
        "after_fill_nb",
        "after_segment_nb",
        "inputs",
        "params",
        "state",
        "trace",
        "orders",
    ]
    assert [field.name for field in dataclasses.fields(PreparedStrategy)] == [
        "ledger_input",
        "primary_program",
        "context",
    ]
    assert [field.name for field in dataclasses.fields(ExecutionRun)] == [
        "ledger",
        "trace",
    ]
    assert [field.name for field in dataclasses.fields(ExecutionBundle)] == [
        "primary",
        "final",
        "stages",
    ]
    assert [field.name for field in dataclasses.fields(ResultExtension)] == [
        "name",
        "schema_version",
        "table",
        "unique_key",
        "evidence",
    ]
    assert set(StrategyModule.__protocol_attrs__) == {
        "descriptor",
        "prepare",
        "followup_program",
        "build_extensions",
    }
    assert list(inspect.signature(StrategyModule.prepare).parameters) == [
        "self",
        "snapshot",
        "config",
    ]
    assert get_type_hints(ResultExtension)["table"] is pa.Table


def test_execution_ledger_protocol_exposes_read_only_properties() -> None:
    for name in ("orders", "assets", "cash", "value", "trades", "positions", "returns"):
        member = inspect.getattr_static(ExecutionLedger, name)
        assert isinstance(member, property)
        assert member.fset is None


@pytest.mark.parametrize("invalid", ("C:/outside", "../outside", "/outside"))
def test_loader_rejects_strategy_root_escape(repo_root: Path, invalid: str) -> None:
    with pytest.raises(ConfigurationError, match="strategy_root"):
        load_strategy(
            repo_root,
            {"root": invalid, "module": "strategy", "symbol": "MODULE"},
        )


@pytest.mark.parametrize("unknown", ("command", "project_entry"))
def test_loader_rejects_unknown_or_legacy_config_fields(
    repo_root: Path,
    unknown: str,
) -> None:
    config = {**_fixture_config("minimal_strategy"), unknown: "forbidden"}

    with pytest.raises(ConfigurationError, match="fields"):
        load_strategy(repo_root, config)


def test_shared_loader_accepts_two_strategy_modules(repo_root: Path) -> None:
    first = load_strategy(repo_root, _fixture_config("minimal_strategy"))
    second = load_strategy(repo_root, _fixture_config("minimal_strategy_b"))

    assert isinstance(first, LoadedStrategy)
    assert (first.descriptor.strategy_id, second.descriptor.strategy_id) == (
        "minimal-fixture",
        "minimal-fixture-b",
    )
    assert first.root != second.root
    assert first.source_paths == (first.root / "strategy.py",)
    assert tuple(path.relative_to(second.root).as_posix() for path in second.source_paths) == (
        "minimal_strategy_b/__init__.py",
        "minimal_strategy_b/strategy.py",
    )


def test_loader_uses_standard_import_and_restores_sys_path(repo_root: Path) -> None:
    before_path = list(sys.path)
    loaded = load_strategy(repo_root, _fixture_config("minimal_strategy"))

    assert loaded.descriptor.strategy_id == "minimal-fixture"
    assert loaded.module.__class__.__module__ == "strategy"
    assert sys.path == before_path
    assert Path(sys.modules["strategy"].__file__).resolve() == loaded.source_paths[0]


def test_loader_rejects_unknown_symbol(repo_root: Path) -> None:
    config = {**_fixture_config("minimal_strategy"), "symbol": "UNKNOWN"}

    with pytest.raises(ConfigurationError, match="symbol"):
        load_strategy(repo_root, config)


def test_loader_rejects_module_file_outside_strategy_root(repo_root: Path) -> None:
    config = {
        **_fixture_config("minimal_strategy"),
        "module": "scripts.research.local_quant_research.contracts",
        "symbol": "StrategyDescriptor",
    }

    with pytest.raises(ConfigurationError, match="module file"):
        load_strategy(repo_root, config)


@pytest.mark.parametrize("fixture_name", ("minimal_strategy", "minimal_strategy_b"))
def test_minimal_strategy_prepares_two_days_one_column_without_orders(
    repo_root: Path,
    fixture_name: str,
) -> None:
    loaded = load_strategy(repo_root, _fixture_config(fixture_name))

    prepared = loaded.module.prepare(object(), {})

    assert prepared.ledger_input.close.shape == (2, 1)
    assert prepared.ledger_input.symbols == ("TEST",)
    assert prepared.primary_program.orders.enabled.shape == (1,)
    assert not prepared.primary_program.orders.enabled.any()
    assert loaded.module.followup_program(prepared, object()) is None
    assert loaded.module.build_extensions(prepared, object()) == ()
