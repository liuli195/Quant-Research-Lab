from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from scripts.research.market_data.benchmark_sets import BENCHMARK_IDS, BenchmarkLevel, BenchmarkSetError, SourcePayload, build_benchmark_rows, write_benchmark_set


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


def test_benchmark_set_rejects_proxy_missing_or_unknown_identity() -> None:
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
                market_data_root=Path("unused-benchmark-store"),
                rows=rows,
                sources=_sources(),
                start_date=date(2024, 1, 2),
                end_date=date(2024, 1, 2),
            )
