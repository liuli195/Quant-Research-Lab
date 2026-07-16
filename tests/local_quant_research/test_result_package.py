from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.research.local_quant_research.contracts import (
    ExecutionBundle,
    ExecutionRun,
    ResultExtension,
)
from scripts.research.local_quant_research.result_package import (
    ResultContractError,
    ResultPackageRequest,
    validate_result_package,
    write_result_package,
)


FORBIDDEN_REPORT_PHRASES = ("推荐", "稳健性通过", "适合实盘", "实盘准入")


def _values() -> np.ndarray:
    return np.array(
        [
            ("2026-01-05 16:00:00", 1_000.0, 0.0, np.nan),
            ("2026-01-06 16:00:00", 1_050.0, 0.05, np.nan),
        ],
        dtype=[
            ("time", "U19"),
            ("total_value", "f8"),
            ("returns", "f8"),
            ("benchmark_returns", "f8"),
        ],
    )


def _cash() -> np.ndarray:
    return np.array(
        [
            ("2026-01-05 16:00:00", 400.0),
            ("2026-01-06 16:00:00", 390.0),
        ],
        dtype=[("time", "U19"), ("cash", "f8")],
    )


def _assets() -> np.ndarray:
    dtype = [
        ("pindex", "i8"),
        ("avg_cost", "f8"),
        ("margin", "f8"),
        ("amount", "f8"),
        ("today_amount", "i8"),
        ("hold_cost", "f8"),
        ("side", "U8"),
        ("price", "f8"),
        ("gains", "f8"),
        ("daily_gains", "f8"),
        ("closeable_amount", "i8"),
        ("time", "U19"),
        ("security_name", "U16"),
        ("security", "U16"),
    ]
    return np.array(
        [
            (
                0,
                90.0,
                0.0,
                6.0,
                0,
                90.0,
                "long",
                100.0,
                60.0,
                0.0,
                6,
                "2026-01-05 16:00:00",
                "ETF-A",
                "ETF-A",
            ),
            (
                0,
                90.0,
                0.0,
                6.0,
                0,
                90.0,
                "long",
                110.0,
                120.0,
                60.0,
                6,
                "2026-01-06 16:00:00",
                "ETF-A",
                "ETF-A",
            ),
        ],
        dtype=dtype,
    )


def _orders() -> np.ndarray:
    dtype = [
        ("match_time", "O"),
        ("pindex", "i8"),
        ("cancel_time", "O"),
        ("action", "U8"),
        ("limit_price", "f8"),
        ("comment", "U16"),
        ("entrust_time", "U19"),
        ("finish_time", "O"),
        ("side", "U8"),
        ("price", "f8"),
        ("commission", "f8"),
        ("gains", "f8"),
        ("type", "U8"),
        ("time", "U19"),
        ("security_name", "U16"),
        ("security", "U16"),
        ("filled", "i8"),
        ("amount", "i8"),
        ("status", "U8"),
    ]
    return np.array(
        [
            (
                "2026-01-06 09:30:00",
                0,
                None,
                "open",
                0.0,
                "",
                "2026-01-06 09:30:00",
                "2026-01-06 09:30:00",
                "long",
                100.0,
                1.0,
                0.0,
                "market",
                "2026-01-06 09:30:00",
                "ETF-A",
                "ETF-A",
                1,
                1,
                "done",
            )
        ],
        dtype=dtype,
    )


class CountingLedger:
    def __init__(
        self,
        *,
        orders: np.ndarray | None = None,
        assets: np.ndarray | None = None,
        cash: np.ndarray | None = None,
        value: np.ndarray | None = None,
    ) -> None:
        self._orders = _orders() if orders is None else orders
        self._assets = _assets() if assets is None else assets
        self._cash = _cash() if cash is None else cash
        self._value = _values() if value is None else value
        self.calls = {"orders": 0, "assets": 0, "cash": 0, "value": 0}

    @property
    def orders(self) -> np.ndarray:
        self.calls["orders"] += 1
        return self._orders

    @property
    def assets(self) -> np.ndarray:
        self.calls["assets"] += 1
        return self._assets

    @property
    def cash(self) -> np.ndarray:
        self.calls["cash"] += 1
        return self._cash

    @property
    def value(self) -> np.ndarray:
        self.calls["value"] += 1
        return self._value

    @property
    def trades(self) -> np.ndarray:
        raise AssertionError("result writer must not read trades")

    @property
    def positions(self) -> np.ndarray:
        raise AssertionError("result writer must not read positions")

    @property
    def returns(self) -> np.ndarray:
        raise AssertionError("result writer must not read returns")


def _execution(ledger: CountingLedger) -> ExecutionBundle:
    run = ExecutionRun(ledger=ledger, trace={})
    return ExecutionBundle(primary=run, final=run, stages=("primary",))


@pytest.fixture
def counting_ledger() -> CountingLedger:
    return CountingLedger()


@pytest.fixture
def package_request(
    tmp_path: Path, counting_ledger: CountingLedger
) -> ResultPackageRequest:
    code = tmp_path / "strategy.py"
    code.write_text("VALUE = 1\n", encoding="utf-8")
    extension = ResultExtension(
        name="decision_log",
        schema_version="decision-log/1",
        table=pa.table(
            {
                "time": ["2026-01-06 09:30:00"],
                "event_id": ["event-1"],
                "score": pa.array([0.75], type=pa.float64()),
            }
        ),
        unique_key=("event_id",),
        evidence={"source": "strategy"},
    )
    return ResultPackageRequest(
        strategy_id="strategy-003",
        scenario_id="baseline",
        run_id="run-1",
        output_dir=tmp_path / "result",
        execution=_execution(counting_ledger),
        extensions=(extension,),
        code_files={"strategy.py": code},
        config_documents={
            "scenario": {"scenario_id": "baseline"},
            "project-run": {"schema_version": 2},
            "code-identity": {"digest": "a" * 64},
        },
        evidence_documents={
            "market-snapshot": {"snapshot_id": "b" * 64},
            "runtime-lock": {"python": "3.12"},
            "performance": {"status": "pass"},
            "environment": {"platform": "windows"},
        },
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _assert_snappy(path: Path) -> None:
    metadata = pq.ParquetFile(path).metadata
    if metadata.num_row_groups == 0:
        return
    for row_group in range(metadata.num_row_groups):
        for column in range(metadata.num_columns):
            assert metadata.row_group(row_group).column(column).compression == "SNAPPY"


def test_writer_materializes_one_package_without_recomputing_ledger(
    package_request: ResultPackageRequest,
    counting_ledger: CountingLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import result_package

    writes: list[tuple[str, str | None]] = []
    real_write = result_package.pq.write_table

    def recording_write(
        table: pa.Table, path: Path, *, compression: str | None = None
    ) -> None:
        writes.append((Path(path).name, compression))
        real_write(table, path, compression=compression)

    monkeypatch.setattr(result_package.pq, "write_table", recording_write)
    package = write_result_package(package_request)
    manifest = validate_result_package(package.path)

    assert set(manifest["datasets"]) == {"results", "balances", "positions", "orders"}
    assert counting_ledger.calls == {"orders": 1, "assets": 1, "cash": 1, "value": 1}
    assert writes == [
        ("results.parquet", "snappy"),
        ("balances.parquet", "snappy"),
        ("positions.parquet", "snappy"),
        ("orders.parquet", "snappy"),
        ("data.parquet", "snappy"),
    ]
    assert len(package.package_sha256) == 64
    for name in manifest["datasets"]:
        reference = manifest["datasets"][name]["files"][0]
        path = package.path / reference["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["sha256"]
        _assert_snappy(path)
    extension_ref = manifest["extensions"]["decision_log"]["files"][0]
    _assert_snappy(package.path / extension_ref["path"])
    report = (package.path / "report/execution-summary.md").read_text(
        encoding="utf-8"
    )
    assert not any(phrase in report for phrase in FORBIDDEN_REPORT_PHRASES)


def test_writer_cleans_only_its_staging_directory_when_readback_fails(
    package_request: ResultPackageRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.research.local_quant_research import result_package

    sibling = package_request.output_dir.parent / "keep-me"
    sibling.mkdir()

    def fail_readback(*_args: object, **_kwargs: object) -> pa.Table:
        raise OSError("injected readback failure")

    monkeypatch.setattr(result_package.pq, "read_table", fail_readback)
    with pytest.raises(ResultContractError, match="readback"):
        write_result_package(package_request)

    assert sibling.is_dir()
    assert not package_request.output_dir.exists()
    assert list(package_request.output_dir.parent.glob(".run-1.*.tmp")) == []


def test_writer_reuses_equal_package_and_rejects_digest_conflict(
    package_request: ResultPackageRequest,
) -> None:
    first = write_result_package(package_request)
    before = _tree_digest(first.path)

    same_ledger = CountingLedger()
    same = write_result_package(
        replace(package_request, execution=_execution(same_ledger))
    )
    assert same.package_sha256 == first.package_sha256
    assert _tree_digest(first.path) == before
    assert same_ledger.calls == {"orders": 1, "assets": 1, "cash": 1, "value": 1}

    conflicting_ledger = CountingLedger()
    with pytest.raises(ResultContractError, match="conflict"):
        write_result_package(
            replace(
                package_request,
                scenario_id="other",
                execution=_execution(conflicting_ledger),
            )
        )
    assert _tree_digest(first.path) == before


def test_writer_atomically_publishes_only_after_staging_is_complete(
    package_request: ResultPackageRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.research.local_quant_research import result_package

    observed: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def observing_replace(source: Path, target: Path) -> None:
        source_path = Path(source)
        target_path = Path(target)
        assert source_path.name.startswith(".run-1.")
        assert source_path.name.endswith(".tmp")
        assert (source_path / "manifest.json").is_file()
        assert (source_path / "report/execution-summary.md").is_file()
        assert set(path.name for path in (source_path / "data").iterdir()) == {
            "results.parquet",
            "balances.parquet",
            "positions.parquet",
            "orders.parquet",
        }
        assert not target_path.exists()
        observed.append((source_path, target_path))
        real_replace(source_path, target_path)

    monkeypatch.setattr(result_package.os, "replace", observing_replace)
    package = write_result_package(package_request)

    assert observed == [(observed[0][0], package.path)]
    assert package.path.is_dir()


@pytest.mark.parametrize("name", ["", "BadName", "bad/name", "-leading"])
def test_writer_rejects_invalid_extension_name_before_reading_ledger(
    package_request: ResultPackageRequest,
    counting_ledger: CountingLedger,
    name: str,
) -> None:
    invalid = replace(package_request.extensions[0], name=name)
    with pytest.raises(ResultContractError, match="extension name"):
        write_result_package(replace(package_request, extensions=(invalid,)))
    assert counting_ledger.calls == {"orders": 0, "assets": 0, "cash": 0, "value": 0}


def test_writer_rejects_duplicate_extension_names_and_keys(
    package_request: ResultPackageRequest,
    counting_ledger: CountingLedger,
) -> None:
    duplicate_name = replace(
        package_request.extensions[0], schema_version="decision-log/2"
    )
    with pytest.raises(ResultContractError, match="unique"):
        write_result_package(
            replace(
                package_request,
                extensions=(package_request.extensions[0], duplicate_name),
            )
        )

    duplicate_key = replace(
        package_request.extensions[0],
        table=pa.table(
            {
                "time": ["2026-01-05", "2026-01-06"],
                "event_id": ["same", "same"],
                "score": [0.1, 0.2],
            }
        ),
    )
    with pytest.raises(ResultContractError, match="unique key"):
        write_result_package(replace(package_request, extensions=(duplicate_key,)))
    assert counting_ledger.calls == {"orders": 0, "assets": 0, "cash": 0, "value": 0}


def test_writer_rejects_cross_table_reconciliation_and_cleans_staging(
    package_request: ResultPackageRequest,
) -> None:
    broken_assets = _assets().copy()
    broken_assets[1]["price"] = 109.0
    ledger = CountingLedger(assets=broken_assets)

    with pytest.raises(ResultContractError, match="reconcile"):
        write_result_package(
            replace(package_request, execution=_execution(ledger))
        )

    assert ledger.calls == {"orders": 1, "assets": 1, "cash": 1, "value": 1}
    assert not package_request.output_dir.exists()
    assert list(package_request.output_dir.parent.glob(".run-1.*.tmp")) == []


def test_validator_rejects_tampered_materialized_table(
    package_request: ResultPackageRequest,
) -> None:
    package = write_result_package(package_request)
    results = package.path / "data/results.parquet"
    results.write_bytes(results.read_bytes() + b"tampered")

    with pytest.raises(ResultContractError, match="digest"):
        validate_result_package(package.path)
