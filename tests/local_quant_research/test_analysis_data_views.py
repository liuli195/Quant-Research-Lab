from __future__ import annotations

import hashlib
import gzip
import json
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
from scripts.research.analysis_data.views import _SCHEMAS, open_analysis_database
from scripts.research.result_contract import (
    ExecutionBundle,
    ExecutionRun,
    ResultExtension,
)
from scripts.research.result_package import (
    ResultPackageRequest,
    write_result_package,
)


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


def _tiny_joinquant_source(root: Path, kind: str) -> tuple[Path, str | None]:
    snapshot_id = "tiny-snapshot" if kind == "simulation" else None
    prefix = f"snapshots/{snapshot_id}/" if snapshot_id else ""
    root.mkdir(parents=True)

    def write(relative: str, content: bytes, file_format: str):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return {**_file_ref(root, target), "format": file_format}

    code = write("code.py", b"# Synthetic reader fixture.\n", "python")
    arrow_types = {"DOUBLE": pa.float64(), "BIGINT": pa.int64(), "VARCHAR": pa.string()}
    times = ["2024-01-02 16:00:00", "2024-01-03 16:00:00"]
    datasets, payloads = {}, {}
    for name, fields in _SCHEMAS.items():
        count = {"results": 2, "balances": 2, "risk": 1}.get(name, 0)
        columns = {field: pa.array(times[:count] if field == "time" else
                                  ["" if sql_type == "VARCHAR" else 0] * count,
                                  type=arrow_types[sql_type]) for field, sql_type in fields}
        if name == "results":
            columns.update(returns=pa.array([0.0, 0.1]), benchmark_returns=pa.array([0.0, 0.01]))
        if name == "balances":
            columns.update({field: pa.array([100.0, 110.0]) for field in ("total_value", "cash", "aval_cash")})
            columns["net_value"] = pa.array([1.0, 1.1])
        if name == "risk" and snapshot_id:
            columns.update(intraday_return=pa.array([0.02]), monthly_return=pa.array([0.03]))
        table = pa.table(columns)
        target = root / f"{prefix}data/{name}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, target, compression="zstd")
        payloads[name] = table.to_pylist()
        datasets[name] = {"required": True, "status": "complete", "rows": count,
                          "verified_empty": count == 0, "files": [_file_ref(root, target, rows=count)]}
    raw = write(f"{prefix}raw/research-response.json.gz", gzip.compress(json.dumps(payloads).encode(), mtime=0), "json.gz")
    url = f"https://www.joinquant.com/algorithm/{'live/index' if snapshot_id else 'backtest/detail'}?backtestId=fixture"
    manifest = {"schema_version": 1, "object": {"kind": kind, "local_id": "tiny", "status": "active" if snapshot_id else "done"},
                "source": {"url": url, "aliases": [], "observed_at": "2024-01-03T16:00:00+08:00"},
                "fence": {"before_sha256": code["sha256"], "after_sha256": code["sha256"]},
                "collection_fence": {"collection_before_sha256": raw["sha256"], "collection_after_sha256": raw["sha256"]},
                "code": code, "research_response": raw, "research_lineage": [raw],
                "datasets": datasets, "gate": {"status": "pass", "exceptions": []}}
    if snapshot_id:
        manifest["streams"] = {name: {"cursor": snapshot_id, "sha256": raw["sha256"]} for name in ("snapshots", "data")}
    else:
        header = ["时间", "基准收益", "策略收益"]
        summary = write("data/official-summary.csv", (",".join(header) + "\n2024-01-02,0,0\n2024-01-03,0.01,0.1\n").encode("utf-8-sig"), "csv")
        datasets["official_summary"] = {"required": True, "status": "complete", "rows": 2, "files": [summary],
            "evidence": {"evidence_version": 1, "source": {"kind": "joinquant_backtest_detail_export", "url": url, "action": "export_csv"},
                         "encoding": "utf-8-sig", "header": header, "rows": 2, "related_datasets": ["results", "balances", "orders"]}}
    write("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(), "json")
    return root, snapshot_id


@pytest.mark.real
def test_joinquant_source_builds_six_read_only_logical_views(tmp_path: Path) -> None:
    root, _ = _tiny_joinquant_source(tmp_path / "backtest", "backtest")
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
        assert database.connection.sql("select count(*) from results").fetchone() == (2,)
        assert database.connection.sql("select count(*) from risk").fetchone() == (1,)
        columns = database.connection.sql("describe orders").fetchall()
        assert dict((name, kind) for name, kind, *_ in columns)["cancel_time"] == "VARCHAR"

    assert _tree_sha(root) == before


@pytest.mark.real
def test_joinquant_simulation_requires_and_pins_one_snapshot(tmp_path: Path) -> None:
    root, snapshot_id = _tiny_joinquant_source(tmp_path / "simulation", "simulation")
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
            "where trading_date = date '2024-01-03'"
        ).fetchall()
        assert final_day == [(date(2024, 1, 3), 0.1)]

    assert _tree_sha(root) == before
