from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from scripts.research.market_data.benchmark_sets import (
    BENCHMARK_IDS,
    BenchmarkLevel,
    BenchmarkSetError,
    SourcePayload,
    build_benchmark_rows,
    open_benchmark_set,
    write_benchmark_set,
)


def _payload(name: str, filename: str) -> SourcePayload:
    identities = {
        "csi300_total_return": (
            "China Securities Index Co., Ltd.",
            "H00300",
            "https://www.csindex.com.cn/",
        ),
        "nasdaq100_total_return": (
            "Nasdaq, Inc.",
            "XNDX",
            "https://indexes.nasdaqomx.com/",
        ),
        "usd_cny": (
            "Board of Governors of the Federal Reserve System",
            "DEXCHUS",
            "https://www.federalreserve.gov/",
        ),
    }
    provider, source_id, url = identities[name]
    return SourcePayload(
        name=name,
        filename=filename,
        provider=provider,
        source_id=source_id,
        url=url + filename,
        content_type="application/octet-stream",
        data=f"source:{name}".encode(),
    )


def _sources() -> tuple[SourcePayload, ...]:
    return (
        _payload("csi300_total_return", "csi300-total-return.xlsx"),
        _payload("nasdaq100_total_return", "nasdaq100-total-return.xlsx"),
        _payload("usd_cny", "usd-cny.html"),
    )


def test_benchmark_rows_use_total_return_and_currency_formula_without_fill() -> None:
    csi = (
        BenchmarkLevel(date(2024, 1, 1), 100.0),
        BenchmarkLevel(date(2024, 1, 2), 110.0),
        BenchmarkLevel(date(2024, 1, 3), 121.0),
    )
    nasdaq = (
        BenchmarkLevel(date(2024, 1, 1), 100.0),
        BenchmarkLevel(date(2024, 1, 2), 110.0),
        BenchmarkLevel(date(2024, 1, 3), 121.0),
    )
    fx = (
        BenchmarkLevel(date(2024, 1, 1), 7.0),
        BenchmarkLevel(date(2024, 1, 2), 7.07),
    )

    rows = build_benchmark_rows(
        csi_levels=csi,
        nasdaq_levels=nasdaq,
        usd_cny_levels=fx,
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
    )

    keyed = {(row["time"], row["benchmark_id"]): row["returns"] for row in rows}
    assert keyed[("2024-01-02", "CSI300_CNY_TOTAL_RETURN")] == pytest.approx(0.1)
    assert keyed[("2024-01-03", "CSI300_CNY_TOTAL_RETURN")] == pytest.approx(0.1)
    assert keyed[("2024-01-02", "NASDAQ100_CNY_TOTAL_RETURN")] == pytest.approx(0.111)
    assert ("2024-01-03", "NASDAQ100_CNY_TOTAL_RETURN") not in keyed


def test_benchmark_set_is_immutable_columnar_and_has_exact_two_identities(
    tmp_path: Path,
) -> None:
    rows = [
        {"time": "2024-01-02", "benchmark_id": BENCHMARK_IDS[0], "returns": 0.01},
        {"time": "2024-01-02", "benchmark_id": BENCHMARK_IDS[1], "returns": 0.02},
    ]
    created = write_benchmark_set(
        market_data_root=tmp_path,
        rows=rows,
        sources=_sources(),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 2),
    )
    opened = open_benchmark_set(created.root)

    assert created.root == opened.root
    assert created.root.parent.name == "benchmark-sets"
    assert created.root.name == created.benchmark_set_id
    assert set(created.manifest["benchmarks"]) == set(BENCHMARK_IDS)
    table = pq.read_table(created.root / "benchmark-returns.parquet")
    assert table.schema.names == ["time", "benchmark_id", "returns"]
    assert str(table.schema.field("returns").type) == "double"
    assert table.num_rows == 2
    for source in created.manifest["sources"]:
        path = created.root / source["path"]
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"]

    reused = write_benchmark_set(
        market_data_root=tmp_path,
        rows=rows,
        sources=_sources(),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 2),
    )
    assert reused.benchmark_set_id == created.benchmark_set_id


def test_benchmark_set_rejects_proxy_missing_or_unknown_identity(tmp_path: Path) -> None:
    valid = [
        {"time": "2024-01-02", "benchmark_id": BENCHMARK_IDS[0], "returns": 0.01},
        {"time": "2024-01-02", "benchmark_id": BENCHMARK_IDS[1], "returns": 0.02},
    ]
    for rows in (
        valid[:1],
        [*valid, {"time": "2024-01-02", "benchmark_id": "ETF_PROXY", "returns": 0.0}],
    ):
        with pytest.raises(BenchmarkSetError):
            write_benchmark_set(
                market_data_root=tmp_path,
                rows=rows,
                sources=_sources(),
                start_date=date(2024, 1, 2),
                end_date=date(2024, 1, 2),
            )


def test_benchmark_set_detects_manifest_or_parquet_tampering(tmp_path: Path) -> None:
    created = write_benchmark_set(
        market_data_root=tmp_path,
        rows=[
            {"time": "2024-01-02", "benchmark_id": BENCHMARK_IDS[0], "returns": 0.01},
            {"time": "2024-01-02", "benchmark_id": BENCHMARK_IDS[1], "returns": 0.02},
        ],
        sources=_sources(),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 2),
    )
    manifest_path = created.root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["data"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BenchmarkSetError, match="digest"):
        open_benchmark_set(created.root)
