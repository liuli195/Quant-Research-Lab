from __future__ import annotations

import dataclasses
import inspect
import shutil
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
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


def _write_strategy(root: Path, source_files: tuple[str, ...]) -> None:
    declared = ", ".join(f"Path({value!r})" for value in source_files)
    if len(source_files) == 1:
        declared += ","
    (root / "strategy.py").write_text(
        "\n".join(
            (
                "from pathlib import Path",
                "from scripts.research.local_quant_research.contracts import StrategyDescriptor",
                "class MinimalModule:",
                "    descriptor = StrategyDescriptor(",
                "        strategy_id='temporary-fixture',",
                "        contract_version='1',",
                f"        source_files=({declared}),",
                "        extension_names=(),",
                "        accounting={},",
                "    )",
                "    def prepare(self, snapshot, config): raise NotImplementedError",
                "    def followup_program(self, prepared, primary_run): return None",
                "    def build_extensions(self, prepared, execution): return ()",
                "MODULE = MinimalModule()",
                "",
            )
        ),
        encoding="utf-8",
    )


def _relative_to_repo(path: Path, repo_root: Path) -> str:
    return path.relative_to(repo_root).as_posix()


def _write_concurrent_strategy(root: Path, strategy_id: str) -> None:
    root.mkdir()
    (root / "strategy.py").write_text(
        "\n".join(
            (
                "import time",
                "from pathlib import Path",
                "from scripts.research.local_quant_research.contracts import StrategyDescriptor",
                "time.sleep(0.05)",
                "class ConcurrentModule:",
                "    descriptor = StrategyDescriptor(",
                f"        strategy_id={strategy_id!r},",
                "        contract_version='1',",
                "        source_files=(Path('strategy.py'),),",
                "        extension_names=(),",
                "        accounting={},",
                "    )",
                "    def prepare(self, snapshot, config): raise NotImplementedError",
                "    def followup_program(self, prepared, primary_run): return None",
                "    def build_extensions(self, prepared, execution): return ()",
                "MODULE = ConcurrentModule()",
                "",
            )
        ),
        encoding="utf-8",
    )


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
        source_files=(Path("strategy.py"),),
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
    assert second.source_paths == (second.root / "strategy.py",)


def test_loader_restores_sys_path_and_conflicting_module_cache(repo_root: Path) -> None:
    before_path = list(sys.path)
    sentinel = ModuleType("strategy")
    previous = sys.modules.get("strategy")
    sys.modules["strategy"] = sentinel
    try:
        loaded = load_strategy(repo_root, _fixture_config("minimal_strategy"))
        assert loaded.descriptor.strategy_id == "minimal-fixture"
        assert sys.path == before_path
        assert sys.modules["strategy"] is sentinel
    finally:
        if previous is None:
            sys.modules.pop("strategy", None)
        else:
            sys.modules["strategy"] = previous


def test_loader_supports_package_relative_imports(
    repo_root: Path,
    temporary_strategy_root: Path,
) -> None:
    package = temporary_strategy_root / "fixture_package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "identity.py").write_text(
        "STRATEGY_ID = 'relative-import-fixture'\n",
        encoding="utf-8",
    )
    (package / "strategy.py").write_text(
        "\n".join(
            (
                "from pathlib import Path",
                "from .identity import STRATEGY_ID",
                "from scripts.research.local_quant_research.contracts import StrategyDescriptor",
                "class RelativeImportModule:",
                "    descriptor = StrategyDescriptor(",
                "        strategy_id=STRATEGY_ID,",
                "        contract_version='1',",
                (
                    "        source_files=(Path('fixture_package/__init__.py'), "
                    "Path('fixture_package/identity.py'), "
                    "Path('fixture_package/strategy.py'))"
                    ","
                ),
                "        extension_names=(),",
                "        accounting={},",
                "    )",
                "    def prepare(self, snapshot, config): raise NotImplementedError",
                "    def followup_program(self, prepared, primary_run): return None",
                "    def build_extensions(self, prepared, execution): return ()",
                "MODULE = RelativeImportModule()",
                "",
            )
        ),
        encoding="utf-8",
    )

    loaded = load_strategy(
        repo_root,
        {
            "root": _relative_to_repo(temporary_strategy_root, repo_root),
            "module": "fixture_package.strategy",
            "symbol": "MODULE",
        },
    )

    assert loaded.descriptor.strategy_id == "relative-import-fixture"
    assert "fixture_package" not in sys.modules
    assert "fixture_package.strategy" not in sys.modules


def test_loader_rejects_external_parent_package_before_execution(
    repo_root: Path,
    temporary_strategy_root: Path,
) -> None:
    package = temporary_strategy_root / "external_parent"
    package.mkdir()
    marker = temporary_strategy_root / "external-parent-executed"
    external_init = temporary_strategy_root.parent / f"{uuid.uuid4().hex}.py"
    external_init.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (package / "__init__.py").symlink_to(external_init)
    (package / "strategy.py").write_text(
        "\n".join(
            (
                "from pathlib import Path",
                "from scripts.research.local_quant_research.contracts import StrategyDescriptor",
                "class ExternalParentModule:",
                "    descriptor = StrategyDescriptor(",
                "        strategy_id='external-parent-fixture',",
                "        contract_version='1',",
                "        source_files=(Path('external_parent/strategy.py'),),",
                "        extension_names=(),",
                "        accounting={},",
                "    )",
                "    def prepare(self, snapshot, config): raise NotImplementedError",
                "    def followup_program(self, prepared, primary_run): return None",
                "    def build_extensions(self, prepared, execution): return ()",
                "MODULE = ExternalParentModule()",
                "",
            )
        ),
        encoding="utf-8",
    )
    try:
        with pytest.raises(ConfigurationError, match="package file"):
            load_strategy(
                repo_root,
                {
                    "root": _relative_to_repo(temporary_strategy_root, repo_root),
                    "module": "external_parent.strategy",
                    "symbol": "MODULE",
                },
            )
        assert not marker.exists()
    finally:
        external_init.unlink(missing_ok=True)


def test_loaded_strategy_supports_delayed_relative_imports(
    repo_root: Path,
    temporary_strategy_root: Path,
) -> None:
    package = temporary_strategy_root / "delayed_package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "identity.py").write_text(
        "STRATEGY_ID = 'delayed-relative-import-fixture'\n",
        encoding="utf-8",
    )
    (package / "strategy.py").write_text(
        "\n".join(
            (
                "from pathlib import Path",
                "from scripts.research.local_quant_research.contracts import StrategyDescriptor",
                "class DelayedImportModule:",
                "    descriptor = StrategyDescriptor(",
                "        strategy_id='delayed-relative-import-fixture',",
                "        contract_version='1',",
                "        source_files=(Path('delayed_package/__init__.py'), Path('delayed_package/identity.py'), Path('delayed_package/strategy.py')),",
                "        extension_names=(),",
                "        accounting={},",
                "    )",
                "    def prepare(self, snapshot, config):",
                "        from .identity import STRATEGY_ID",
                "        return STRATEGY_ID",
                "    def followup_program(self, prepared, primary_run): return None",
                "    def build_extensions(self, prepared, execution): return ()",
                "MODULE = DelayedImportModule()",
                "",
            )
        ),
        encoding="utf-8",
    )

    loaded = load_strategy(
        repo_root,
        {
            "root": _relative_to_repo(temporary_strategy_root, repo_root),
            "module": "delayed_package.strategy",
            "symbol": "MODULE",
        },
    )

    assert loaded.module.prepare(object(), {}) == "delayed-relative-import-fixture"
    assert "delayed_package" not in sys.modules
    assert "delayed_package.strategy" not in sys.modules


def test_loader_concurrently_isolates_same_named_modules(
    repo_root: Path,
    temporary_strategy_root: Path,
) -> None:
    first_root = temporary_strategy_root / "first"
    second_root = temporary_strategy_root / "second"
    _write_concurrent_strategy(first_root, "concurrent-first")
    _write_concurrent_strategy(second_root, "concurrent-second")
    before_path = list(sys.path)
    missing = object()
    before_module = sys.modules.get("strategy", missing)
    barrier = threading.Barrier(2)

    def load(root: Path) -> LoadedStrategy:
        barrier.wait(timeout=5)
        return load_strategy(
            repo_root,
            {
                "root": _relative_to_repo(root, repo_root),
                "module": "strategy",
                "symbol": "MODULE",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(load, first_root)
        second_future = executor.submit(load, second_root)
        first = first_future.result(timeout=10)
        second = second_future.result(timeout=10)

    assert (first.descriptor.strategy_id, second.descriptor.strategy_id) == (
        "concurrent-first",
        "concurrent-second",
    )
    assert first.source_paths == (first_root.resolve() / "strategy.py",)
    assert second.source_paths == (second_root.resolve() / "strategy.py",)
    assert sys.path == before_path
    assert sys.modules.get("strategy", missing) is before_module


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


def test_loader_does_not_execute_module_file_outside_strategy_root(
    repo_root: Path,
    temporary_strategy_root: Path,
) -> None:
    module_name = f"strategy_contract_external_{uuid.uuid4().hex}"
    external_module = repo_root / f"{module_name}.py"
    marker = temporary_strategy_root / "external-module-executed"
    external_module.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(ConfigurationError, match="module file"):
            load_strategy(
                repo_root,
                {
                    "root": _relative_to_repo(temporary_strategy_root, repo_root),
                    "module": module_name,
                    "symbol": "MODULE",
                },
            )
        assert not marker.exists()
    finally:
        external_module.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("source_files", "message"),
    (
        (("C:/outside.py",), "source_files"),
        (("../outside.py",), "source_files"),
        (("strategy.py", "./strategy.py"), "duplicate"),
        (("missing.py",), "missing"),
        (("source_dir",), "file"),
    ),
)
def test_loader_rejects_unsafe_or_invalid_source_files(
    repo_root: Path,
    temporary_strategy_root: Path,
    source_files: tuple[str, ...],
    message: str,
) -> None:
    (temporary_strategy_root / "source_dir").mkdir()
    _write_strategy(temporary_strategy_root, source_files)

    with pytest.raises(ConfigurationError, match=message):
        load_strategy(
            repo_root,
            {
                "root": _relative_to_repo(temporary_strategy_root, repo_root),
                "module": "strategy",
                "symbol": "MODULE",
            },
        )


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
