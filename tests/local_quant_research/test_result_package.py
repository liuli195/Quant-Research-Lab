from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pytest

from scripts.research.local_quant_research.contracts import (
    ExecutionBundle,
    ExecutionRun,
    ResultExtension,
)
from scripts.research.result_package import ResultContractError, ResultPackage, ResultPackageRequest, write_result_package


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


def _result_request(
    code: Path, output_dir: Path, counting_ledger: CountingLedger
) -> ResultPackageRequest:
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
        output_dir=output_dir,
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


@pytest.fixture
def in_memory_package_request(repo_root: Path, counting_ledger: CountingLedger) -> ResultPackageRequest:
    code = repo_root / "tests/local_quant_research/fixtures/minimal_strategy/strategy.py"
    return _result_request(code, Path("unused-publish/result"), counting_ledger)


def test_scenario_gate_uses_writer_duration_through_delayed_return(
    in_memory_package_request: ResultPackageRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import performance, scenario
    from scripts.research.local_quant_research.performance import (
        PerformanceEvidence,
        PerformanceGateError,
        PerformanceSample,
    )
    from scripts.research.result_package import ResultPackage

    code_path = next(iter(in_memory_package_request.code_files.values()))
    loaded_strategy = SimpleNamespace(
        root=code_path.parent,
        source_paths=(code_path,),
        descriptor=SimpleNamespace(strategy_id=in_memory_package_request.strategy_id),
    )
    warm = SimpleNamespace(
        execution=in_memory_package_request.execution,
        extensions=in_memory_package_request.extensions,
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
            path=in_memory_package_request.output_dir,
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
        scenario={"scenario_id": in_memory_package_request.scenario_id},
        project_document={},
        run_id=in_memory_package_request.run_id,
        output_dir=in_memory_package_request.output_dir,
        code_identity={},
        market_snapshot={},
        runtime_lock={},
        environment={},
    )

    with pytest.raises(PerformanceGateError) as caught:
        scenario.execute_scenario(request)

    assert caught.value.code == "cold_performance_limit"


def test_execution_report_contains_only_reproducible_package_facts(
    in_memory_package_request: ResultPackageRequest,
) -> None:
    from scripts.research import result_package as module
    request = in_memory_package_request
    facts = module._materialize_core(request.execution)
    report_facts = module._build_report_facts(config=request.config_documents, facts=facts,
        extension_tables={extension.name: extension.table for extension in request.extensions})
    performance = {**request.evidence_documents["performance.json"], "stages": {
        "core_facts": 0.0, "parquet_materialize": 0.0, "readback_validate": 0.0, "report_and_manifest": 0.0}}
    report_bytes, metrics_bytes = module._report_payloads(
        identity={"strategy_id": request.strategy_id, "scenario_id": request.scenario_id, "run_id": request.run_id},
        evidence={**request.evidence_documents, "performance.json": performance},
        report_facts=report_facts, package_sha256="a" * 64)
    report = report_bytes.decode("utf-8")
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

    metrics = json.loads(metrics_bytes)
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


def test_writer_reuses_equal_package_and_rejects_digest_conflict(
    in_memory_package_request: ResultPackageRequest, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research import result_package as module
    request = replace(in_memory_package_request, output_dir=repo_root)
    expected_request = replace(request, execution=_execution(CountingLedger()))
    inputs = module._freeze_request_inputs(expected_request)
    extensions = module._validate_extension_contracts(expected_request.extensions)
    facts = module._materialize_core(expected_request.execution)
    core_summaries = module._validate_common_facts(facts)
    _, extension_summaries = module._validate_extensions(expected_request.extensions)
    expected = module._canonical_digest(module._content_document(
        expected_request, inputs, extensions, core_summaries, extension_summaries))
    monkeypatch.setattr(module, "validate_result_package", lambda _root: {"package_sha256": expected})
    monkeypatch.setattr(module, "_read_json", lambda _path, _field: dict(request.evidence_documents["performance.json"]))
    same_ledger = CountingLedger()
    same = write_result_package(replace(request, execution=_execution(same_ledger)))
    assert same.package_sha256 == expected
    assert same.path == repo_root
    assert same_ledger.calls == {"orders": 1, "assets": 1, "cash": 1, "value": 1}
    with pytest.raises(ResultContractError, match="conflict"):
        write_result_package(replace(request, scenario_id="other", execution=_execution(CountingLedger())))


def test_writer_propagates_existing_package_validation_failure(
    in_memory_package_request: ResultPackageRequest, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research import result_package as module
    def invalid_existing_package(_root):
        raise ResultContractError("report content mismatch")
    monkeypatch.setattr(module, "validate_result_package", invalid_existing_package)
    with pytest.raises(ResultContractError, match="report"):
        write_result_package(replace(in_memory_package_request, output_dir=repo_root))


@pytest.mark.parametrize("name", ["", "BadName", "bad/name", "-leading"])
def test_writer_rejects_invalid_extension_name_before_reading_ledger(
    in_memory_package_request: ResultPackageRequest,
    counting_ledger: CountingLedger,
    name: str,
) -> None:
    invalid = replace(in_memory_package_request.extensions[0], name=name)
    with pytest.raises(ResultContractError, match="extension name"):
        write_result_package(replace(in_memory_package_request, extensions=(invalid,)))
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
    in_memory_package_request: ResultPackageRequest,
    counting_ledger: CountingLedger,
    run_id: str,
) -> None:
    output_dir = in_memory_package_request.output_dir

    with pytest.raises(ResultContractError, match="run_id"):
        write_result_package(
            replace(in_memory_package_request, run_id=run_id, output_dir=output_dir)
        )

    assert counting_ledger.calls == {"orders": 0, "assets": 0, "cash": 0, "value": 0}
    assert not output_dir.parent.exists()


def test_writer_rejects_blank_scenario_identity_before_reading_or_writing(
    in_memory_package_request: ResultPackageRequest,
    counting_ledger: CountingLedger,
) -> None:
    config = dict(in_memory_package_request.config_documents)
    config["scenario.json"] = {"scenario_id": "   "}

    with pytest.raises(ResultContractError, match="identity"):
        write_result_package(
            replace(
                in_memory_package_request,
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
    in_memory_package_request: ResultPackageRequest,
    counting_ledger: CountingLedger,
    field: str,
    unsafe_name: str,
) -> None:
    output_dir = in_memory_package_request.output_dir
    replacement: dict[str, object]
    if field == "code_files":
        replacement = {
            **in_memory_package_request.code_files,
            unsafe_name: next(iter(in_memory_package_request.code_files.values())),
        }
    elif field == "config_documents":
        replacement = {**in_memory_package_request.config_documents, unsafe_name: {}}
    else:
        replacement = {**in_memory_package_request.evidence_documents, unsafe_name: {}}

    with pytest.raises(ResultContractError, match="unsafe"):
        write_result_package(
            replace(in_memory_package_request, output_dir=output_dir, **{field: replacement})
        )

    assert counting_ledger.calls == {"orders": 0, "assets": 0, "cash": 0, "value": 0}
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
    in_memory_package_request: ResultPackageRequest,
    counting_ledger: CountingLedger,
    field: str,
    missing_name: str | None,
) -> None:
    output_dir = in_memory_package_request.output_dir
    replacement = (
        {}
        if field == "code_files"
        else {
            name: value
            for name, value in getattr(in_memory_package_request, field).items()
            if name != missing_name
        }
    )

    with pytest.raises(ResultContractError, match="archive-ready"):
        write_result_package(
            replace(in_memory_package_request, output_dir=output_dir, **{field: replacement})
        )

    assert counting_ledger.calls == {"orders": 0, "assets": 0, "cash": 0, "value": 0}
    assert not output_dir.parent.exists()


def test_writer_rejects_duplicate_extension_names_and_keys(
    in_memory_package_request: ResultPackageRequest,
    counting_ledger: CountingLedger,
) -> None:
    duplicate_name = replace(
        in_memory_package_request.extensions[0], schema_version="decision-log/2"
    )
    with pytest.raises(ResultContractError, match="unique"):
        write_result_package(
            replace(
                in_memory_package_request,
                extensions=(in_memory_package_request.extensions[0], duplicate_name),
            )
        )

    duplicate_key = replace(
        in_memory_package_request.extensions[0],
        table=pa.table(
            {
                "time": ["2026-01-05", "2026-01-06"],
                "event_id": ["same", "same"],
                "score": [0.1, 0.2],
            }
        ),
    )
    with pytest.raises(ResultContractError, match="unique key"):
        write_result_package(replace(in_memory_package_request, extensions=(duplicate_key,)))
    assert counting_ledger.calls == {"orders": 0, "assets": 0, "cash": 0, "value": 0}
