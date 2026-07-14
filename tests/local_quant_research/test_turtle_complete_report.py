from __future__ import annotations

import sys
from pathlib import Path


RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))

from turtle_etf.cli import run_candidate_set  # noqa: E402


CANDIDATES = (
    {"id": "baseline", "overrides": {}},
    {"id": "entry-40", "overrides": {"signal.entry_days": 40}},
    {"id": "entry-60", "overrides": {"signal.entry_days": 60}},
    {"id": "stop-1.5n", "overrides": {"signal.stop_n": 1.5}},
    {"id": "stop-2.5n", "overrides": {"signal.stop_n": 2.5}},
    {
        "id": "covariance-120d",
        "overrides": {
            "risk.covariance": {"method": "sample", "window_days": 120}
        },
    },
    {
        "id": "covariance-ewma-30d",
        "overrides": {
            "risk.covariance": {"method": "ewma", "half_life_days": 30}
        },
    },
)


def test_all_seven_candidates_are_run_in_order_and_nested_overrides_are_merged() -> None:
    calls: list[dict[str, object]] = []
    baseline = {
        "signal": {"entry_days": 55, "stop_n": 2.0},
        "risk": {"covariance": {"method": "sample", "window_days": 60}},
    }

    def runner(config: dict[str, object]) -> dict[str, object]:
        calls.append(config)
        return config

    results = run_candidate_set(baseline, CANDIDATES, runner)

    assert [candidate_id for candidate_id, _ in results] == [
        item["id"] for item in CANDIDATES
    ]
    assert len(calls) == 7
    assert calls[1]["signal"] == {"entry_days": 40, "stop_n": 2.0}
    assert calls[5]["risk"]["covariance"] == {
        "method": "sample",
        "window_days": 120,
    }
    assert calls[6]["risk"]["covariance"] == {
        "method": "ewma",
        "window_days": 60,
        "half_life_days": 30,
    }


def test_candidate_set_rejects_missing_reordered_or_unplanned_candidates() -> None:
    invalid = (*CANDIDATES[1:], CANDIDATES[0])

    try:
        run_candidate_set({}, invalid, lambda config: config)
    except ValueError as exc:
        assert "frozen seven" in str(exc)
    else:
        raise AssertionError("invalid candidate order was accepted")
