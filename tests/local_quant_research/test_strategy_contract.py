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

from scripts.research.local_quant_research import strategy_loader
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


def _relative_to_repo(path: Path, repo_root: Path) -> str:
    return path.relative_to(repo_root).as_posix()


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


def test_static_source_discovery_does_not_execute_strategy_and_captures_all_python(
    repo_root: Path,
    temporary_strategy_root: Path,
) -> None:
    package = temporary_strategy_root / "fixture_package"
    package.mkdir()
    marker = package / "executed"
    (package / "__init__.py").write_text("", encoding="utf-8")
    helper = package / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    strategy = package / "strategy.py"
    strategy.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    adjacent = temporary_strategy_root / "adjacent"
    adjacent.mkdir()
    (adjacent / "ignored.py").write_text("VALUE = 2\n", encoding="utf-8")
    archive = package / "research" / "archives"
    archive.mkdir(parents=True)
    (archive / "ignored.py").write_text("VALUE = 3\n", encoding="utf-8")

    discovered = strategy_loader.discover_strategy_sources(
        temporary_strategy_root,
        "fixture_package.strategy",
    )

    assert discovered == (
        (package / "__init__.py").resolve(),
        helper.resolve(),
        strategy.resolve(),
    )
    assert not marker.exists()


def test_static_source_discovery_rejects_linked_python_source(
    repo_root: Path,
    temporary_strategy_root: Path,
) -> None:
    (temporary_strategy_root / "strategy.py").write_text("MODULE = object()\n", encoding="utf-8")
    external = temporary_strategy_root.parent / f"{uuid.uuid4().hex}.py"
    external.write_text("VALUE = 1\n", encoding="utf-8")
    linked = temporary_strategy_root / "linked.py"
    try:
        linked.symlink_to(external)
    except OSError as exc:
        external.unlink(missing_ok=True)
        pytest.skip(f"file links are unavailable: {exc}")
    try:
        with pytest.raises(ConfigurationError) as caught:
            strategy_loader.discover_strategy_sources(
                temporary_strategy_root,
                "strategy",
            )
        assert caught.value.code == "unsafe_strategy_source_tree"
    finally:
        linked.unlink(missing_ok=True)
        external.unlink(missing_ok=True)


def test_static_source_discovery_rejects_archive_as_strategy_root(
    temporary_strategy_root: Path,
) -> None:
    archive_code = temporary_strategy_root / "research" / "archives" / "saved" / "code"
    archive_code.mkdir(parents=True)
    (archive_code / "strategy.py").write_text("MODULE = object()\n", encoding="utf-8")

    with pytest.raises(ConfigurationError) as caught:
        strategy_loader.discover_strategy_sources(archive_code, "strategy")

    assert caught.value.code == "unsafe_strategy_source_tree"


def test_loader_uses_standard_import_and_restores_sys_path(repo_root: Path) -> None:
    before_path = list(sys.path)
    loaded = load_strategy(repo_root, _fixture_config("minimal_strategy"))

    assert loaded.descriptor.strategy_id == "minimal-fixture"
    assert loaded.module.__class__.__module__ == "strategy"
    assert sys.path == before_path
    assert Path(sys.modules["strategy"].__file__).resolve() == loaded.source_paths[0]


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
                "from .identity import STRATEGY_ID",
                "from scripts.research.local_quant_research.contracts import StrategyDescriptor",
                "class RelativeImportModule:",
                "    descriptor = StrategyDescriptor(",
                "        strategy_id=STRATEGY_ID,",
                "        contract_version='1',",
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
    assert "fixture_package" in sys.modules
    assert "fixture_package.strategy" in sys.modules


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
                "from scripts.research.local_quant_research.contracts import StrategyDescriptor",
                "class ExternalParentModule:",
                "    descriptor = StrategyDescriptor(",
                "        strategy_id='external-parent-fixture',",
                "        contract_version='1',",
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
                "from scripts.research.local_quant_research.contracts import StrategyDescriptor",
                "class DelayedImportModule:",
                "    descriptor = StrategyDescriptor(",
                "        strategy_id='delayed-relative-import-fixture',",
                "        contract_version='1',",
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
    assert "delayed_package" in sys.modules
    assert "delayed_package.strategy" in sys.modules


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
