from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Mapping

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.research.analysis_data.manifest import (
    AnalysisManifestError,
    open_analysis_source,
)
from scripts.research.analysis_data.views import open_analysis_database
from scripts.research.local_quant_research.contracts import (
    ExecutionBundle,
    ExecutionRun,
    ResultExtension,
)
from scripts.research.result_package import (
    ResultPackageRequest,
    write_result_package,
)


SHA_FIELDS = ("path", "sha256", "bytes")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_ref(root: Path, path: Path, *, rows: int | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha(path),
        "bytes": path.stat().st_size,
    }
    if rows is not None:
        result.update({"rows": rows, "format": "parquet", "compression": "zstd"})
    return result


def _write_local_result(root: Path) -> Path:
    data = root / "data"
    versions = root / "params_versions"
    data.mkdir(parents=True)
    versions.mkdir()
    code = root / "code.py"
    params = root / "params.json"
    performance = root / "performance.json"
    code.write_text("VALUE = 1\n", encoding="utf-8")
    params.write_text('{"scenario_id":"baseline"}\n', encoding="utf-8")
    params_sha = _sha(params)
    version = versions / f"{params_sha}.json"
    version.write_bytes(params.read_bytes())
    performance.write_text('{"status":"pass"}\n', encoding="utf-8")

    tables = {
        "results": pa.Table.from_arrays(
            [
                pa.array([None, None, None], type=pa.float64()),
                pa.array([0.0, 0.1, 0.21], type=pa.float64()),
                pa.array(
                    [
                        "2024-01-02 16:00:00",
                        "2024-01-03 16:00:00",
                        "2024-01-04 16:00:00",
                    ],
                    type=pa.string(),
                ),
            ],
            names=["benchmark_returns", "returns", "time"],
        ),
        "balances": pa.table(
            {
                "total_value": pa.array([100.0, 110.0, 121.0], type=pa.float64()),
                "net_value": pa.array([100.0, 110.0, 121.0], type=pa.float64()),
                "cash": pa.array([100.0, 110.0, 121.0], type=pa.float64()),
                "aval_cash": pa.array([100.0, 110.0, 121.0], type=pa.float64()),
                "time": pa.array(
                    [
                        "2024-01-02 16:00:00",
                        "2024-01-03 16:00:00",
                        "2024-01-04 16:00:00",
                    ]
                ),
            }
        ),
        "positions": pa.Table.from_pylist(
            [],
            schema=pa.schema(
                [
                    ("pindex", pa.int64()),
                    ("avg_cost", pa.float64()),
                    ("margin", pa.float64()),
                    ("amount", pa.float64()),
                    ("today_amount", pa.int64()),
                    ("hold_cost", pa.float64()),
                    ("side", pa.string()),
                    ("price", pa.float64()),
                    ("gains", pa.float64()),
                    ("daily_gains", pa.float64()),
                    ("closeable_amount", pa.int64()),
                    ("time", pa.string()),
                    ("security_name", pa.string()),
                    ("security", pa.string()),
                ]
            ),
        ),
        "orders": pa.Table.from_pylist(
            [],
            schema=pa.schema(
                [
                    ("match_time", pa.string()),
                    ("pindex", pa.int64()),
                    ("cancel_time", pa.string()),
                    ("action", pa.string()),
                    ("limit_price", pa.float64()),
                    ("comment", pa.string()),
                    ("entrust_time", pa.string()),
                    ("finish_time", pa.string()),
                    ("side", pa.string()),
                    ("price", pa.float64()),
                    ("commission", pa.float64()),
                    ("gains", pa.float64()),
                    ("type", pa.string()),
                    ("time", pa.string()),
                    ("security_name", pa.string()),
                    ("security", pa.string()),
                    ("filled", pa.int64()),
                    ("amount", pa.int64()),
                    ("status", pa.string()),
                ]
            ),
        ),
    }
    for name, table in tables.items():
        pq.write_table(table, data / f"{name}.parquet", compression="zstd")

    datasets: dict[str, object] = {}
    for name, table in tables.items():
        rows = table.num_rows
        datasets[name] = {
            "required": True,
            "status": "complete",
            "rows": rows,
            "verified_empty": rows == 0,
            "time_range": {
                "start": None if rows == 0 else "2024-01-02",
                "end": None if rows == 0 else "2024-01-04",
            },
            "files": [_file_ref(root, data / f"{name}.parquet", rows=rows)],
            "evidence": {
                "fields": table.schema.names,
                "unique_key": ["time"] if name != "orders" else ["time", "security"],
            },
        }
    for name in ("risk", "period_risks"):
        datasets[name] = {
            "required": False,
            "status": "missing_at_source",
            "reason": "computed_by_strategy_analysis",
            "rows": 0,
            "verified_empty": True,
            "files": [],
        }
    manifest = {
        "schema_version": "local-backtest/1",
        "object": {"kind": "local_backtest", "local_id": "local-1", "status": "complete"},
        "source": {
            "kind": "local_vectorbt",
            "engine": {
                "backend": "vectorbt.Portfolio.from_order_func",
                "adapter_version": "local-vectorbt-adapter/1",
                "vectorbt": "1.1.0",
                "numba": "0.66.0",
                "numpy": "2.4.6",
                "pandas": "3.0.3",
            },
            "accounting": {
                "version": "turtle-etf-corporate-actions/1",
                "corporate_action_mode": "point_in_time_total_return_approximation",
                "continuity_factor_basis": "raw_previous_close_over_current_pre_close",
                "corporate_action_metadata_timing": "audit_only_may_be_retrospective",
                "price_basis": "continuous_economic_price",
                "quantity_basis": "economic_units",
                "cash_dividend_mode": "implicit_reinvestment_on_ex_date",
                "pay_date_cash_supported": False,
                "exact_joinquant_reconciliation": False,
                "corporate_actions_sha256": "a" * 64,
            },
        },
        "authority": "local_research",
        "run": {"run_id": "run-1", "scenario_id": "baseline", "snapshot_id": "a" * 64},
        "code": _file_ref(root, code),
        "params": {"current": _file_ref(root, params), "version": _file_ref(root, version)},
        "performance": _file_ref(root, performance),
        "datasets": datasets,
        "source_benchmark_returns": {
            "status": "missing_at_source",
            "reason": "independent_benchmark_set",
            "null_rows": 3,
        },
        "gate": {"status": "pass", "exceptions": [], "checks": ["digests"]},
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    return root


def _tree_sha(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


class _AnalysisLedger:
    @property
    def orders(self) -> np.ndarray:
        return np.array(
            [],
            dtype=[
                ("match_time", "O"),
                ("pindex", "i8"),
                ("cancel_time", "O"),
                ("action", "U8"),
                ("limit_price", "f8"),
                ("comment", "U8"),
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
            ],
        )

    @property
    def assets(self) -> np.ndarray:
        return np.array(
            [],
            dtype=[
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
            ],
        )

    @property
    def cash(self) -> np.ndarray:
        return np.array(
            [("2024-01-02 16:00:00", 100.0), ("2024-01-03 16:00:00", 110.0)],
            dtype=[("time", "U19"), ("cash", "f8")],
        )

    @property
    def value(self) -> np.ndarray:
        return np.array(
            [
                ("2024-01-02 16:00:00", 100.0, 0.0, np.nan),
                ("2024-01-03 16:00:00", 110.0, 0.1, np.nan),
            ],
            dtype=[
                ("time", "U19"),
                ("total_value", "f8"),
                ("returns", "f8"),
                ("benchmark_returns", "f8"),
            ],
        )


def _write_result_package(
    root: Path,
    *,
    extensions: tuple[ResultExtension, ...] | None = None,
    strategy_id: str = "minimal",
    scenario: Mapping[str, object] | None = None,
) -> Path:
    code = root.parent / "strategy.py"
    code.write_text("VALUE = 1\n", encoding="utf-8")
    ledger = _AnalysisLedger()
    run = ExecutionRun(ledger=ledger, trace={})
    package = write_result_package(
        ResultPackageRequest(
            strategy_id=strategy_id,
            scenario_id=str((scenario or {}).get("scenario_id", "baseline")),
            run_id="run-analysis",
            output_dir=root,
            execution=ExecutionBundle(primary=run, final=run, stages=("primary",)),
            extensions=extensions
            if extensions is not None
            else (
                ResultExtension(
                    name="signals",
                    schema_version="signals/1",
                    table=pa.table({"event_id": ["signal-1"], "score": [0.5]}),
                    unique_key=("event_id",),
                    evidence={"status": "complete"},
                ),
            ),
            code_files={"strategy.py": code},
            config_documents={
                "scenario.json": dict(scenario or {"scenario_id": "baseline"}),
                "project-run.json": {"schema_version": 2},
                "code-identity.json": {"digest": "b" * 64},
            },
            evidence_documents={
                "market-snapshot.json": {"snapshot_id": "a" * 64},
                "runtime-lock.json": {"python": "3.12"},
                "performance.json": {"status": "pass"},
                "environment.json": {"platform": "windows"},
            },
        )
    )
    return package.path


def _sync_package_reference(
    root: Path, manifest: dict[str, object], dataset: str
) -> None:
    path = root / f"data/{dataset}.parquet"
    reference = manifest["datasets"][dataset]["files"][0]
    reference["sha256"] = _sha(path)
    reference["bytes"] = path.stat().st_size


def test_local_research_package_exposes_identity_core_and_named_extension(
    tmp_path: Path,
) -> None:
    root = _write_result_package(tmp_path / "result")
    source = open_analysis_source(root)

    assert source.kind == "local_research"
    assert source.authority == "local_research"
    assert source.backend == "vectorbt"
    assert source.formula_version == "unified-strategy-analysis/1"
    assert set(source.manifest["datasets"]) == {
        "results",
        "balances",
        "positions",
        "orders",
    }

    with open_analysis_database(root) as database:
        assert database.table_names == ("results", "balances", "positions", "orders")
        assert database.connection.sql("select count(*) from results").fetchone() == (2,)
        assert database.extension("signals").fetchall() == [("signal-1", 0.5)]
        assert database.reference_status("risk") == (
            "missing_at_source",
            "local-research-package/2",
        )
        assert database.reference_status("period_risks") == (
            "missing_at_source",
            "local-research-package/2",
        )
        with pytest.raises(KeyError, match="unknown"):
            database.extension("unknown")


def test_open_analysis_source_summarizes_each_core_table_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research import result_package

    root = _write_result_package(tmp_path / "result")
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

    open_analysis_source(root)

    assert summaries == Counter(
        {name: 1 for name in result_package.CORE_DATASETS}
    )


def test_local_research_rejects_noncanonical_core_path_before_query(
    tmp_path: Path,
) -> None:
    root = _write_result_package(tmp_path / "result")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reference = manifest["datasets"]["results"]["files"][0]
    source = root / str(reference["path"])
    replacement = root / "data/renamed-results.parquet"
    replacement.write_bytes(source.read_bytes())
    reference["path"] = replacement.relative_to(root).as_posix()
    reference["sha256"] = _sha(replacement)
    reference["bytes"] = replacement.stat().st_size
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(AnalysisManifestError, match="file identity"):
        open_analysis_database(root)


def test_local_research_extension_digest_is_validated_before_query(
    tmp_path: Path,
) -> None:
    root = _write_result_package(tmp_path / "result")
    extension = root / "extensions/signals/data.parquet"
    extension.write_bytes(extension.read_bytes() + b"tampered")

    with pytest.raises(AnalysisManifestError, match="digest"):
        open_analysis_database(root)


@pytest.mark.parametrize(
    "tampering",
    ["schema", "unique_key", "time_range", "reconciliation", "package_sha"],
)
def test_local_research_source_rejects_untruthful_complete_contract(
    tmp_path: Path,
    tampering: str,
) -> None:
    root = _write_result_package(tmp_path / "result")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if tampering == "schema":
        path = root / "data/results.parquet"
        table = pq.read_table(path)
        replacement = table.set_column(
            table.schema.get_field_index("returns"),
            pa.field("returns", pa.float32(), nullable=False),
            pa.array(table["returns"].to_pylist(), type=pa.float32()),
        )
        pq.write_table(replacement, path, compression="snappy")
        manifest["datasets"]["results"]["schema"] = [
            {"name": field.name, "type": str(field.type), "nullable": field.nullable}
            for field in replacement.schema
        ]
        _sync_package_reference(root, manifest, "results")
    elif tampering == "unique_key":
        path = root / "data/results.parquet"
        table = pq.read_table(path)
        repeated_time = table["time"][0].as_py()
        replacement = table.set_column(
            table.schema.get_field_index("time"),
            table.schema.field("time"),
            pa.array([repeated_time] * table.num_rows, type=pa.string()),
        )
        pq.write_table(replacement, path, compression="snappy")
        repeated_date = str(repeated_time)[:10]
        manifest["datasets"]["results"]["time_range"] = {
            "start": repeated_date,
            "end": repeated_date,
        }
        _sync_package_reference(root, manifest, "results")
    elif tampering == "time_range":
        manifest["datasets"]["results"]["time_range"] = {
            "start": "2020-01-01",
            "end": "2020-01-02",
        }
    elif tampering == "reconciliation":
        path = root / "data/balances.parquet"
        table = pq.read_table(path)
        replacement = table.set_column(
            table.schema.get_field_index("total_value"),
            table.schema.field("total_value"),
            pa.array([100.0, 999.0], type=pa.float64()),
        )
        pq.write_table(replacement, path, compression="snappy")
        _sync_package_reference(root, manifest, "balances")
    else:
        manifest["package_sha256"] = "f" * 64

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(AnalysisManifestError):
        open_analysis_source(root)


def test_joinquant_source_builds_six_read_only_logical_views(repo_root: Path) -> None:
    root = repo_root / "joinquant/strategies/strategy-001/backtests/111"
    before = _tree_sha(root)

    with open_analysis_database(root) as database:
        assert database.source.kind == "joinquant_backtest"
        assert database.table_names == (
            "results",
            "balances",
            "positions",
            "orders",
            "risk",
            "period_risks",
        )
        assert database.connection.sql("select count(*) from results").fetchone() == (1289,)
        assert database.connection.sql("select count(*) from risk").fetchone() == (1,)
        columns = database.connection.sql("describe orders").fetchall()
        assert dict((name, kind) for name, kind, *_ in columns)["cancel_time"] == "VARCHAR"

    assert _tree_sha(root) == before


def test_joinquant_simulation_requires_and_pins_one_snapshot(repo_root: Path) -> None:
    root = repo_root / "joinquant/strategies/strategy-001/simulations/simulation-001"
    snapshot_id = "5cc582a778eca2ddc481282b"
    before = _tree_sha(root)

    with pytest.raises(AnalysisManifestError, match="explicit snapshot_id"):
        open_analysis_source(root)

    with open_analysis_database(root, snapshot_id=snapshot_id) as database:
        assert database.source.kind == "joinquant_simulation"
        assert database.source.snapshot_id == snapshot_id
        assert database.connection.sql("select count(*) from results").fetchone()[0] > 0
        assert database.connection.sql("select count(*) from risk").fetchone() == (1,)
        risk_fields = [
            row[0] for row in database.connection.sql("describe risk").fetchall()
        ]
        assert "intraday_return" not in risk_fields
        assert "monthly_return" not in risk_fields
        assert "sharpe" in risk_fields
        final_day = database.connection.sql(
            "select trading_date, cumulative_returns from strategy_daily_returns "
            "where trading_date = date '2026-07-17'"
        ).fetchall()
        assert final_day == [(date(2026, 7, 17), -0.014533449871168003)]

    assert _tree_sha(root) == before


def test_joinquant_simulation_rejects_data_outside_registered_snapshot(
    repo_root: Path, tmp_path: Path
) -> None:
    source = (
        repo_root / "joinquant/strategies/strategy-001/simulations/simulation-001"
    )
    document = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    document["datasets"]["results"]["files"][1]["path"] = (
        "snapshots/other-snapshot/data/results.parquet"
    )
    root = tmp_path / "simulation"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(AnalysisManifestError, match="outside the registered snapshot"):
        open_analysis_source(root, snapshot_id="5cc582a778eca2ddc481282b")


def test_joinquant_legal_missing_attribution_exception_remains_read_only(
    repo_root: Path,
) -> None:
    root = repo_root / "joinquant/strategies/strategy-001/backtests/10"
    before = _tree_sha(root)

    with open_analysis_database(root) as database:
        assert database.source.kind == "joinquant_backtest"
        assert database.connection.sql("select count(*) from results").fetchone() == (
            57,
        )

    assert _tree_sha(root) == before


def test_joinquant_unknown_gate_exception_is_not_downgraded(
    repo_root: Path, tmp_path: Path
) -> None:
    source = repo_root / "joinquant/strategies/strategy-001/backtests/10/manifest.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    document["gate"]["exceptions"] = ["unknown:exception"]
    root = tmp_path / "joinquant-result"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(AnalysisManifestError, match="gate did not pass"):
        open_analysis_database(root)


def test_joinquant_column_order_variants_are_normalized_in_memory(
    repo_root: Path,
) -> None:
    root = repo_root / "joinquant/strategies/strategy-001/backtests/113"
    before = _tree_sha(root)

    with open_analysis_database(root) as database:
        columns = database.connection.sql("describe balances").fetchall()
        assert [row[0] for row in columns] == [
            "total_value",
            "net_value",
            "cash",
            "aval_cash",
            "time",
        ]
        assert database.connection.sql("select count(*) from balances").fetchone() == (
            1289,
        )

    assert _tree_sha(root) == before


def test_joinquant_legal_empty_tables_become_typed_memory_views(repo_root: Path) -> None:
    root = repo_root / "joinquant/strategies/strategy-001/backtests/109"
    with open_analysis_database(root) as database:
        assert database.connection.sql("select count(*) from positions").fetchone() == (0,)
        assert database.connection.sql("select count(*) from orders").fetchone() == (0,)
        assert [row[0] for row in database.connection.sql("describe positions").fetchall()] == [
            "pindex",
            "avg_cost",
            "margin",
            "amount",
            "today_amount",
            "hold_cost",
            "side",
            "price",
            "gains",
            "daily_gains",
            "closeable_amount",
            "time",
            "security_name",
            "security",
        ]


def test_local_source_exposes_missing_references_without_fake_files(tmp_path: Path) -> None:
    root = _write_local_result(tmp_path / "local")
    with open_analysis_database(root) as database:
        assert database.source.kind == "local_backtest"
        assert database.connection.sql("select count(*) from risk").fetchone() == (0,)
        assert database.connection.sql("select count(*) from period_risks").fetchone() == (0,)
        schema = database.connection.sql("describe results").fetchall()
        assert [(row[0], row[1]) for row in schema] == [
            ("benchmark_returns", "DOUBLE"),
            ("returns", "DOUBLE"),
            ("time", "VARCHAR"),
        ]
        assert database.connection.sql(
            "select count(benchmark_returns) from results"
        ).fetchone() == (0,)
        assert database.reference_status("risk") == (
            "missing_at_source",
            "computed_by_strategy_analysis",
        )


def test_daily_returns_are_derived_from_cumulative_values_at_query_time(
    tmp_path: Path,
) -> None:
    root = _write_local_result(tmp_path / "local")
    with open_analysis_database(root) as database:
        rows = database.connection.sql(
            "select trading_date, cumulative_returns, daily_returns, comparable "
            "from strategy_daily_returns order by trading_date"
        ).fetchall()

    assert rows[0][0].isoformat() == "2024-01-02"
    assert rows[0][1:] == (0.0, 0.0, True)
    assert rows[1][1] == 0.1
    assert abs(rows[1][2] - 0.1) < 1e-12
    assert rows[2][1] == 0.21
    assert abs(rows[2][2] - 0.1) < 1e-12


def test_nonzero_first_cumulative_return_is_not_compared_without_predecessor(
    tmp_path: Path,
) -> None:
    root = _write_local_result(tmp_path / "local")
    results = root / "data/results.parquet"
    table = pq.read_table(results)
    replacement = table.set_column(
        table.schema.get_field_index("returns"),
        "returns",
        pa.array([0.05, 0.1, 0.21], type=pa.float64()),
    )
    pq.write_table(replacement, results, compression="zstd")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["datasets"]["results"]["files"][0] = _file_ref(root, results, rows=3)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    with open_analysis_database(root) as database:
        first = database.connection.sql(
            "select daily_returns, comparable from strategy_daily_returns "
            "order by trading_date limit 1"
        ).fetchone()
    assert first == (None, False)
