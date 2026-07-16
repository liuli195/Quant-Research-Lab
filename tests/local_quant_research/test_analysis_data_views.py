from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.research.analysis_data.manifest import AnalysisManifestError
from scripts.research.analysis_data.views import open_analysis_database


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
