from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

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
            "scenario.json": {
                "scenario_id": "baseline",
                "parameters": {"lookback": 20},
            },
            "project-run.json": {"schema_version": 2},
            "code-identity.json": {"digest": "a" * 64},
        },
        evidence_documents={
            "market-snapshot.json": {"snapshot_id": "b" * 64},
            "runtime-lock.json": {"python": "3.12"},
            "performance.json": {"status": "pass", "elapsed_seconds": 0.25},
            "environment.json": {"platform": "windows"},
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


def _write_manifest(path: Path, document: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def _point_reference_at(
    root: Path,
    reference: dict[str, object],
    source: Path,
    relative: str,
) -> None:
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    reference["path"] = relative
    reference["sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
    reference["bytes"] = destination.stat().st_size


def _replace_persisted_extension_table(package: Path, table: pa.Table) -> None:
    """Keep the disk declaration self-consistent up to logical package identity."""
    from scripts.research.local_quant_research import result_package

    data_path = package / "extensions/decision_log/data.parquet"
    pq.write_table(table, data_path, compression="snappy")
    persisted = pq.read_table(data_path)
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["extensions"]["decision_log"]
    reference = entry["files"][0]
    reference["sha256"] = hashlib.sha256(data_path.read_bytes()).hexdigest()
    reference["bytes"] = data_path.stat().st_size
    reference["rows"] = table.num_rows
    entry["rows"] = table.num_rows
    entry["verified_empty"] = table.num_rows == 0
    entry["schema"] = result_package._schema_document(persisted.schema)
    entry["time_range"] = result_package._time_range(persisted)
    entry["evidence"]["fields"] = persisted.schema.names
    _write_manifest(manifest_path, manifest)


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
    assert set(package.writer_stages) == {
        "core_facts",
        "parquet_materialize",
        "readback_validate",
        "report_and_manifest",
    }
    assert all(seconds > 0 for seconds in package.writer_stages.values())
    performance = json.loads(
        (package.path / "evidence/performance.json").read_text(encoding="utf-8")
    )
    for name in ("core_facts", "parquet_materialize", "readback_validate"):
        assert performance["stages"][name] == package.writer_stages[name]
    assert (
        performance["stages"]["report_and_manifest"]
        <= package.writer_stages["report_and_manifest"]
    )
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


def test_writer_freezes_code_before_digest_and_copy(
    package_request: ResultPackageRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = next(iter(package_request.code_files.values())).resolve()
    original = source.read_bytes()
    replacement = b"VALUE = 2\n"
    real_read_bytes = Path.read_bytes
    source_reads = 0

    def changing_read_bytes(path: Path) -> bytes:
        nonlocal source_reads
        payload = real_read_bytes(path)
        if path.resolve() == source:
            source_reads += 1
            if source_reads == 1:
                path.write_bytes(replacement)
        return payload

    monkeypatch.setattr(Path, "read_bytes", changing_read_bytes)

    package = write_result_package(package_request)

    assert source_reads == 1
    assert source.read_bytes() == replacement
    assert (package.path / "code/strategy.py").read_bytes() == original
    assert validate_result_package(package.path)["package_sha256"] == package.package_sha256


def test_writer_rejects_forbidden_report_before_publish(
    package_request: ResultPackageRequest,
) -> None:
    config = dict(package_request.config_documents)
    config["scenario.json"] = {
        "scenario_id": "baseline",
        "parameters": {"conclusion": "推荐"},
    }

    with pytest.raises(ResultContractError, match="forbidden"):
        write_result_package(replace(package_request, config_documents=config))

    assert not package_request.output_dir.exists()
    assert list(package_request.output_dir.parent.glob(".run-1.*.tmp")) == []


def test_writer_reads_each_materialized_parquet_table_only_once(
    package_request: ResultPackageRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import result_package

    reads: Counter[Path] = Counter()
    real_read = result_package.pq.read_table

    def recording_read(path: Path, *args: object, **kwargs: object) -> pa.Table:
        reads[Path(path).resolve()] += 1
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(result_package.pq, "read_table", recording_read)
    write_result_package(package_request)

    read_ids = Counter(
        (
            "extensions/decision_log/data.parquet"
            if path.name == "data.parquet"
            else f"data/{path.name}"
        )
        for path, count in reads.items()
        for _ in range(count)
    )
    assert read_ids == Counter(
        {
            "data/results.parquet": 1,
            "data/balances.parquet": 1,
            "data/positions.parquet": 1,
            "data/orders.parquet": 1,
            "extensions/decision_log/data.parquet": 1,
        }
    )


def test_writer_uses_its_single_readback_without_calling_disk_validator(
    package_request: ResultPackageRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import result_package

    def fail_validator(*_args: object, **_kwargs: object) -> None:
        pytest.fail("writer must not call the public disk validator")

    monkeypatch.setattr(result_package, "validate_result_package", fail_validator)
    monkeypatch.setattr(result_package, "_validate_result_package_document", fail_validator)

    write_result_package(package_request)


def test_writer_distinguishes_persisted_prefinalization_from_returned_full_duration(
    package_request: ResultPackageRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import result_package

    delay_seconds = 0.05
    real_write_documents = result_package._write_documents

    def delayed_write_documents(
        root: Path,
        directory: str,
        documents: Mapping[str, object],
    ) -> dict[str, dict[str, object]]:
        if directory == "evidence":
            time.sleep(delay_seconds)
        return real_write_documents(root, directory, documents)

    monkeypatch.setattr(result_package, "_write_documents", delayed_write_documents)
    package = write_result_package(package_request)
    performance = json.loads(
        (package.path / "evidence/performance.json").read_text(encoding="utf-8")
    )

    writer = performance["writer"]
    assert set(writer) == {"prefinalization_seconds"}
    assert writer["prefinalization_seconds"] > 0
    assert package.writer_seconds - writer["prefinalization_seconds"] >= (
        delay_seconds * 0.8
    )
    assert package.writer_stages["report_and_manifest"] >= delay_seconds * 0.8
    assert performance["measurement_scope"] == {
        "actual_gate_basis": "returned_writer_seconds_through_writer_return",
        "persisted_writer_measurement": "writer_start_through_prefinalization_before_final_evidence_report_manifest_write",
        "persisted_measurement_excludes": "final_evidence_report_manifest_write_and_atomic_publish",
    }


def test_scenario_gate_uses_writer_duration_through_delayed_return(
    package_request: ResultPackageRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import performance, scenario
    from scripts.research.local_quant_research.performance import (
        PerformanceEvidence,
        PerformanceGateError,
        PerformanceSample,
    )
    from scripts.research.local_quant_research.result_package import ResultPackage

    code_path = next(iter(package_request.code_files.values()))
    loaded_strategy = SimpleNamespace(
        root=code_path.parent,
        source_paths=(code_path,),
        descriptor=SimpleNamespace(strategy_id=package_request.strategy_id),
    )
    warm = SimpleNamespace(
        execution=package_request.execution,
        extensions=package_request.extensions,
        stages={},
    )
    samples = PerformanceEvidence(
        cold=PerformanceSample("cold", 0.0, "same"),
        warm=PerformanceSample("warm", 0.0, "same"),
    )
    monkeypatch.setattr(
        scenario,
        "run_cold_warm",
        lambda _operation, *, digest: (warm, samples),
    )

    def delayed_writer(_request: ResultPackageRequest) -> ResultPackage:
        started = time.perf_counter()
        time.sleep(0.05)
        return ResultPackage(
            path=package_request.output_dir,
            manifest={},
            package_sha256="a" * 64,
            writer_stages={},
            writer_seconds=time.perf_counter() - started,
        )

    monkeypatch.setattr(scenario, "write_result_package", delayed_writer)
    monkeypatch.setattr(performance, "PERFORMANCE_LIMIT_SECONDS", 0.04)
    request = scenario.ScenarioRequest(
        loaded_strategy=loaded_strategy,
        snapshot=SimpleNamespace(),
        scenario={"scenario_id": package_request.scenario_id},
        project_document={},
        run_id=package_request.run_id,
        output_dir=package_request.output_dir,
        code_identity={},
        market_snapshot={},
        runtime_lock={},
        environment={},
    )

    with pytest.raises(PerformanceGateError) as caught:
        scenario.execute_scenario(request)

    assert caught.value.code == "cold_performance_limit"


def test_validator_summarizes_each_core_table_once(
    package_request: ResultPackageRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import result_package

    package = write_result_package(package_request)
    names_by_fields = {
        tuple(schema.names): name for name, schema in result_package._SCHEMAS.items()
    }
    summaries: Counter[str] = Counter()
    reads: Counter[Path] = Counter()
    real_summary = result_package._table_summary
    real_read = result_package.pq.read_table

    def counting_summary(table: pa.Table) -> dict[str, object]:
        name = names_by_fields.get(tuple(table.schema.names))
        if name is not None:
            summaries[name] += 1
        return real_summary(table)

    def counting_read(path: Path, *args: object, **kwargs: object) -> pa.Table:
        reads[Path(path).resolve()] += 1
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(result_package, "_table_summary", counting_summary)
    monkeypatch.setattr(result_package.pq, "read_table", counting_read)

    validate_result_package(package.path)

    assert summaries == Counter({name: 1 for name in result_package.CORE_DATASETS})
    assert reads == Counter(
        {
            (package.path / f"data/{name}.parquet").resolve(): 1
            for name in result_package.CORE_DATASETS
        }
        | {
            (package.path / "extensions/decision_log/data.parquet").resolve(): 1
        }
    )


def test_writer_reuse_summarizes_new_and_existing_core_once_each(
    package_request: ResultPackageRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import result_package

    write_result_package(package_request)
    names_by_fields = {
        tuple(schema.names): name for name, schema in result_package._SCHEMAS.items()
    }
    summaries: Counter[str] = Counter()
    real_summary = result_package._table_summary

    def counting_summary(table: pa.Table) -> dict[str, object]:
        name = names_by_fields.get(tuple(table.schema.names))
        if name is not None:
            summaries[name] += 1
        return real_summary(table)

    monkeypatch.setattr(result_package, "_table_summary", counting_summary)

    reused = write_result_package(
        replace(package_request, execution=_execution(CountingLedger()))
    )

    assert reused.path == package_request.output_dir.resolve()
    assert summaries == Counter({name: 2 for name in result_package.CORE_DATASETS})


def test_execution_report_contains_only_reproducible_package_facts(
    package_request: ResultPackageRequest,
) -> None:
    package = write_result_package(package_request)
    report = (package.path / "report/execution-summary.md").read_text(
        encoding="utf-8"
    )
    for heading in (
        "## 参数与配置",
        "## 时间范围",
        "## 成交摘要",
        "## 持仓摘要",
        "## 净值摘要",
        "## 性能",
        "## 完整性门禁",
    ):
        assert heading in report
    assert '"lookback": 20' in report
    assert "2026-01-05" in report and "2026-01-06" in report
    assert "订单记录：1" in report
    assert "成交数量：1" in report
    assert "最新持仓数量：1" in report
    assert "最新持仓市值：660.000000" in report
    assert "起始总资产：1000.000000" in report
    assert "结束总资产：1050.000000" in report
    assert '"elapsed_seconds": 0.25' in report
    assert "状态：`pass`" in report
    assert not any(phrase in report for phrase in FORBIDDEN_REPORT_PHRASES)

    metrics = json.loads(
        (package.path / "report/metrics.json").read_text(encoding="utf-8")
    )
    assert metrics["parameters"] == {"lookback": 20}
    assert metrics["time_range"] == {"start": "2026-01-05", "end": "2026-01-06"}
    assert metrics["orders"] == {
        "records": 1,
        "requested_amount": 1,
        "filled_amount": 1,
        "commission": 1.0,
    }
    assert metrics["positions"]["latest_records"] == 1
    assert metrics["positions"]["latest_market_value"] == 660.0
    assert metrics["net_value"]["start_total_value"] == 1000.0
    assert metrics["net_value"]["end_total_value"] == 1050.0
    assert metrics["performance"]["status"] == "pass"
    assert metrics["performance"]["elapsed_seconds"] == 0.25
    assert set(metrics["performance"]["stages"]) == {
        "core_facts",
        "parquet_materialize",
        "readback_validate",
        "report_and_manifest",
    }
    assert metrics["integrity_gate"]["status"] == "pass"


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


def test_validator_rejects_manifest_scenario_different_from_frozen_config(
    package_request: ResultPackageRequest,
) -> None:
    mismatched = replace(
        package_request,
        scenario_id="different-scenario",
        output_dir=package_request.output_dir.parent / "scenario-mismatch",
    )

    with pytest.raises(ResultContractError, match="scenario identity"):
        write_result_package(mismatched)

    assert not mismatched.output_dir.exists()


def test_writer_refuses_reuse_when_report_and_manifest_reference_are_tampered(
    package_request: ResultPackageRequest,
) -> None:
    package = write_result_package(package_request)
    manifest_path = package.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = package.path / "report/execution-summary.md"
    metrics = package.path / "report/metrics.json"
    summary.write_text(
        summary.read_text(encoding="utf-8") + "\n外部替换内容\n",
        encoding="utf-8",
    )
    metrics_document = json.loads(metrics.read_text(encoding="utf-8"))
    metrics_document["external"] = True
    metrics.write_text(
        json.dumps(metrics_document, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    for name, path in (("execution-summary", summary), ("metrics", metrics)):
        reference = manifest["reports"][name]
        reference["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        reference["bytes"] = path.stat().st_size
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(ResultContractError, match="report"):
        write_result_package(
            replace(package_request, execution=_execution(CountingLedger()))
        )


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


@pytest.mark.parametrize(
    "run_id",
    [
        "C:foo",
        "/absolute",
        "\\anchored",
        "../escape",
        "nested/run",
        "nested\\run",
        "run:ads",
        "CON",
        "trailing.",
        "trailing ",
    ],
)
def test_writer_rejects_unsafe_run_id_before_reading_or_writing(
    package_request: ResultPackageRequest,
    counting_ledger: CountingLedger,
    tmp_path: Path,
    run_id: str,
) -> None:
    marker = tmp_path / "outside-marker.txt"
    marker.write_text("unchanged", encoding="utf-8")
    output_dir = tmp_path / "publish" / "result"

    with pytest.raises(ResultContractError, match="run_id"):
        write_result_package(
            replace(package_request, run_id=run_id, output_dir=output_dir)
        )

    assert counting_ledger.calls == {"orders": 0, "assets": 0, "cash": 0, "value": 0}
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert not output_dir.parent.exists()


def test_writer_rejects_blank_scenario_identity_before_reading_or_writing(
    package_request: ResultPackageRequest,
    counting_ledger: CountingLedger,
) -> None:
    config = dict(package_request.config_documents)
    config["scenario.json"] = {"scenario_id": "   "}

    with pytest.raises(ResultContractError, match="identity"):
        write_result_package(
            replace(
                package_request,
                scenario_id="   ",
                config_documents=config,
            )
        )

    assert counting_ledger.calls == {"orders": 0, "assets": 0, "cash": 0, "value": 0}


@pytest.mark.parametrize(
    ("field", "unsafe_name"),
    [
        ("code_files", "C:escape.py"),
        ("code_files", "/escape.py"),
        ("code_files", "../escape.py"),
        ("code_files", "strategy.py:ads"),
        ("code_files", "CON"),
        ("config_documents", "C:scenario"),
        ("config_documents", "scenario:ads"),
        ("config_documents", "AUX"),
        ("evidence_documents", "C:environment"),
        ("evidence_documents", "environment:ads"),
        ("evidence_documents", "NUL"),
    ],
)
def test_writer_rejects_unsafe_package_paths_before_reading_or_writing(
    package_request: ResultPackageRequest,
    counting_ledger: CountingLedger,
    tmp_path: Path,
    field: str,
    unsafe_name: str,
) -> None:
    marker = tmp_path / "outside-marker.txt"
    marker.write_text("unchanged", encoding="utf-8")
    output_dir = tmp_path / "publish" / "result"
    replacement: dict[str, object]
    if field == "code_files":
        replacement = {
            **package_request.code_files,
            unsafe_name: next(iter(package_request.code_files.values())),
        }
    elif field == "config_documents":
        replacement = {**package_request.config_documents, unsafe_name: {}}
    else:
        replacement = {**package_request.evidence_documents, unsafe_name: {}}

    with pytest.raises(ResultContractError, match="unsafe"):
        write_result_package(
            replace(package_request, output_dir=output_dir, **{field: replacement})
        )

    assert counting_ledger.calls == {"orders": 0, "assets": 0, "cash": 0, "value": 0}
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert not output_dir.parent.exists()


@pytest.mark.parametrize(
    ("field", "missing_name"),
    [
        ("code_files", None),
        ("config_documents", "scenario.json"),
        ("config_documents", "project-run.json"),
        ("config_documents", "code-identity.json"),
        ("evidence_documents", "market-snapshot.json"),
        ("evidence_documents", "runtime-lock.json"),
        ("evidence_documents", "performance.json"),
        ("evidence_documents", "environment.json"),
    ],
)
def test_writer_requires_archive_ready_inputs_before_reading_or_writing(
    package_request: ResultPackageRequest,
    counting_ledger: CountingLedger,
    tmp_path: Path,
    field: str,
    missing_name: str | None,
) -> None:
    output_dir = tmp_path / "publish" / "result"
    replacement = (
        {}
        if field == "code_files"
        else {
            name: value
            for name, value in getattr(package_request, field).items()
            if name != missing_name
        }
    )

    with pytest.raises(ResultContractError, match="archive-ready"):
        write_result_package(
            replace(package_request, output_dir=output_dir, **{field: replacement})
        )

    assert counting_ledger.calls == {"orders": 0, "assets": 0, "cash": 0, "value": 0}
    assert not output_dir.parent.exists()


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


@pytest.mark.parametrize(
    "invalid_value",
    (
        pa.array([["nested"]], type=pa.list_(pa.string())),
        pa.array([1], type=pa.int32()),
    ),
)
def test_disk_validator_rejects_extension_types_outside_flat_contract(
    package_request: ResultPackageRequest,
    invalid_value: pa.Array,
) -> None:
    package = write_result_package(package_request)
    _replace_persisted_extension_table(
        package.path,
        pa.table({"event_id": ["event-1"], "value": invalid_value}),
    )

    with pytest.raises(ResultContractError, match="flat string/bool/int64/float64"):
        validate_result_package(package.path)


def test_validator_rejects_non_snappy_physical_compression_with_synced_digest(
    package_request: ResultPackageRequest,
) -> None:
    package = write_result_package(package_request)
    results = package.path / "data/results.parquet"
    table = pq.read_table(results)
    pq.write_table(table, results, compression="zstd")
    manifest_path = package.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reference = manifest["datasets"]["results"]["files"][0]
    reference["sha256"] = hashlib.sha256(results.read_bytes()).hexdigest()
    reference["bytes"] = results.stat().st_size
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(ResultContractError, match="physical compression"):
        validate_result_package(package.path)


@pytest.mark.parametrize(
    "relative",
    [
        "C:scenario.json",
        "/absolute/scenario.json",
        "../escape/scenario.json",
        "config/scenario.json:ads",
    ],
)
def test_validator_rejects_unsafe_declared_paths(
    package_request: ResultPackageRequest,
    relative: str,
) -> None:
    package = write_result_package(package_request)
    manifest_path = package.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reference = manifest["config"]["scenario.json"]
    if relative.endswith(":ads"):
        source = package.path / str(reference["path"])
        _point_reference_at(package.path, reference, source, relative)
    else:
        reference["path"] = relative
    _write_manifest(manifest_path, manifest)

    with pytest.raises(ResultContractError, match="unsafe"):
        validate_result_package(package.path)


@pytest.mark.parametrize(
    ("section", "name"),
    [
        ("code", "strategy.py"),
        ("config", "scenario.json"),
        ("config", "project-run.json"),
        ("config", "code-identity.json"),
        ("evidence", "market-snapshot.json"),
        ("evidence", "runtime-lock.json"),
        ("evidence", "performance.json"),
        ("evidence", "environment.json"),
    ],
)
def test_validator_requires_archive_ready_file_sets(
    package_request: ResultPackageRequest,
    section: str,
    name: str,
) -> None:
    package = write_result_package(package_request)
    manifest_path = package.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest[section][name]
    _write_manifest(manifest_path, manifest)

    with pytest.raises(ResultContractError, match="archive-ready"):
        validate_result_package(package.path)


@pytest.mark.parametrize(
    "declaration",
    ["code", "config", "evidence", "dataset", "extension"],
)
def test_validator_requires_fixed_declared_file_paths(
    package_request: ResultPackageRequest,
    declaration: str,
) -> None:
    package = write_result_package(package_request)
    manifest_path = package.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if declaration == "code":
        reference = manifest["code"]["strategy.py"]
        relative = "code/renamed-strategy.py"
    elif declaration == "config":
        reference = manifest["config"]["scenario.json"]
        relative = "config/renamed-scenario.json"
    elif declaration == "evidence":
        reference = manifest["evidence"]["performance.json"]
        relative = "evidence/renamed-performance.json"
    elif declaration == "dataset":
        reference = manifest["datasets"]["results"]["files"][0]
        relative = "data/renamed-results.parquet"
    else:
        reference = manifest["extensions"]["decision_log"]["files"][0]
        relative = "extensions/decision_log/renamed-data.parquet"
    source = package.path / str(reference["path"])
    _point_reference_at(package.path, reference, source, relative)
    _write_manifest(manifest_path, manifest)

    with pytest.raises(ResultContractError, match="file identity"):
        validate_result_package(package.path)


@pytest.mark.parametrize("strategy_evidence", [None, [], "unverified"])
def test_validator_requires_strategy_evidence_object(
    package_request: ResultPackageRequest,
    strategy_evidence: object,
) -> None:
    package = write_result_package(package_request)
    manifest_path = package.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    extension = manifest["extensions"]["decision_log"]
    if strategy_evidence is None:
        del extension["strategy_evidence"]
    else:
        extension["strategy_evidence"] = strategy_evidence
    _write_manifest(manifest_path, manifest)

    with pytest.raises(ResultContractError, match="strategy_evidence"):
        validate_result_package(package.path)
