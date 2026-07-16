from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.research.market_data.candidate_screen import (
    CandidateScreenRule,
    screen_candidates,
    write_candidate_screen,
)


RULE = CandidateScreenRule(
    min_valid_days=750,
    money_lookback_days=20,
    min_median_money=100_000_000.0,
)


def _candidate_rows(
    *,
    valid_days: int = 750,
    median_money: float = 100_000_000.0,
    illegal_ohlc: bool = False,
) -> list[dict[str, object]]:
    dates = pd.bdate_range(end="2026-07-13", periods=valid_days)
    rows = []
    previous_close = 10.0
    for index, current_date in enumerate(dates):
        close = 10.0 + index / 10_000
        row = {
            "date": current_date.strftime("%Y-%m-%d"),
            "security": "159985.XSHE",
            "open": close,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "pre_close": previous_close,
            "volume": 10_000_000.0,
            "money": median_money,
            "factor": 1.0,
            "paused": False,
            "high_limit": close * 1.1,
            "low_limit": close * 0.9,
        }
        rows.append(row)
        previous_close = close
    if illegal_ohlc:
        rows[-1]["high"] = float(rows[-1]["low"]) - 1.0
    return rows


def _official_metadata(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {
        "159985.XSHE": {
            "security": "159985.XSHE",
            "official_start_date": rows[0]["date"],
            "first_market_date": rows[0]["date"],
            "last_market_date": rows[-1]["date"],
            "market_rows": len(rows),
        }
    }


def _screen(rows: list[dict[str, object]]):
    return screen_candidates(
        rows=rows,
        corporate_actions=(),
        official_security_metadata=_official_metadata(rows),
        requested_securities=("159985.XSHE",),
        as_of_date="2026-07-13",
        rule=RULE,
        instrument_risk_notes={
            "159985.XSHE": (
                "移仓与期限结构风险",
                "保证金外资产收益不等于现货涨跌",
            )
        },
    )


def test_screen_counts_only_valid_days_and_freezes_risk_disclosure() -> None:
    rows = _candidate_rows()

    result = _screen(rows)

    security = result.results[0]
    assert security.status == "pass"
    assert security.valid_days == 750
    assert security.median_money_20d == pytest.approx(100_000_000.0)
    assert security.instrument_risk_notes == (
        "移仓与期限结构风险",
        "保证金外资产收益不等于现货涨跌",
    )
    assert result.passed_securities == ("159985.XSHE",)


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        (_candidate_rows(valid_days=749), "valid_days_below_750"),
        (_candidate_rows(median_money=99_999_999.0), "median_money_below_100000000"),
        (_candidate_rows(illegal_ohlc=True), "illegal_ohlc"),
    ],
)
def test_screen_rejects_each_fixed_gate(
    rows: list[dict[str, object]], reason: str
) -> None:
    result = _screen(rows)

    assert reason in result.results[0].reason_codes
    assert result.passed_securities == ()


def test_screen_report_is_content_addressed_and_contains_no_backtest_metrics(
    tmp_path: Path,
) -> None:
    result = _screen(_candidate_rows())

    path = write_candidate_screen(result, root=tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))

    assert path == tmp_path / "screens" / f"{result.screen_id}.json"
    assert document["screen_id"] == result.screen_id
    forbidden = {"returns", "cagr", "calmar", "max_drawdown"}
    serialized = json.dumps(document, ensure_ascii=False).lower()
    assert all(f'"{field}"' not in serialized for field in forbidden)
    assert write_candidate_screen(result, root=tmp_path) == path


def test_invalid_official_coverage_is_a_screen_failure_not_an_exception() -> None:
    rows = _candidate_rows()
    metadata = _official_metadata(rows)
    metadata["159985.XSHE"]["market_rows"] = None

    result = screen_candidates(
        rows=rows,
        corporate_actions=(),
        official_security_metadata=metadata,
        requested_securities=("159985.XSHE",),
        as_of_date="2026-07-13",
        rule=RULE,
    )

    assert result.results[0].status == "fail"
    assert "official_coverage_mismatch" in result.results[0].reason_codes
