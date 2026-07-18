from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from scripts.research.local_quant_research.contracts import ExecutionBundle
from scripts.research.local_quant_research.vectorbt_runtime import run_vectorbt
from scripts.research.market_data.contracts import corporate_actions_digest
from scripts.research.market_data.query import SnapshotView


RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))


ATTRIBUTION_FIELDS = (
    "time",
    "event_id",
    "scope",
    "security",
    "event_type",
    "reason_code",
    "requested_amount",
    "executed_amount",
    "reference_price",
    "risk_before",
    "risk_after",
    "details_json",
)


def _snapshot(closes: tuple[float, ...]) -> SnapshotView:
    rows = tuple(
        {
            "date": f"2026-01-{5 + index:02d}",
            "security": "ETF-A",
            "open": close + 0.25,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "pre_close": closes[max(index - 1, 0)],
            "volume": 1_000_000.0,
            "money": 10_000_000.0,
            "factor": 1.0,
            "paused": False,
            "high_limit": close + 2.0,
            "low_limit": close - 2.0,
        }
        for index, close in enumerate(closes)
    )
    return SnapshotView(
        snapshot_id="attribution-test",
        fields=tuple(rows[0]),
        rows=rows,
        digest="1" * 64,
        corporate_action_fields=(),
        corporate_actions=(),
        corporate_actions_digest=corporate_actions_digest(()),
    )


def _split_snapshot() -> SnapshotView:
    closes = (100.0, 102.0, 51.0, 52.0, 53.0)
    pre_closes = (100.0, 100.0, 51.0, 51.0, 52.0)
    rows = tuple(
        {
            "date": f"2026-01-{5 + index:02d}",
            "security": "ETF-A",
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "pre_close": pre_closes[index],
            "volume": 1_000_000.0,
            "money": 10_000_000.0,
            "factor": 1.0,
            "paused": False,
            "high_limit": close + 5.0,
            "low_limit": close - 5.0,
        }
        for index, close in enumerate(closes)
    )
    action = {
        "source_event_id": "FUND_DIVIDEND:101",
        "security": "ETF-A",
        "event_type": "split",
        "announcement_date": "2026-01-06",
        "record_date": "2026-01-06",
        "ex_date": "2026-01-07",
        "effective_date": "2026-01-07",
        "pay_date": None,
        "status": "active",
        "knowledge_cutoff_date": "2026-01-10",
        "split_ratio": 2.0,
        "cash_per_share": None,
        "source": "joinquant.finance.FUND_DIVIDEND",
        "source_record_sha256": "b" * 64,
    }
    actions = (action,)
    return SnapshotView(
        snapshot_id="corporate-action-test",
        fields=tuple(rows[0]),
        rows=rows,
        digest="2" * 64,
        corporate_action_fields=tuple(action),
        corporate_actions=actions,
        corporate_actions_digest=corporate_actions_digest(actions),
    )


def _config(delay_days: int) -> dict[str, object]:
    return {
        "scenario_id": f"attribution-{delay_days}",
        "universe": [{"security": "ETF-A", "asset_group": "equity"}],
        "signal": {
            "entry_days": 1,
            "exit_days": 1,
            "n_days": 1,
            "add_step_n": 0.5,
            "stop_n": 2.0,
            "max_units": 4,
        },
        "risk": {
            "lot_size": 100,
            "unit_risk_per_n": 0.01,
            "asset_group_unit_cap": 6.0,
            "portfolio_unit_cap": 12.0,
        },
        "costs": {"commission_multiplier": 1.0, "one_way_slippage": 0.0},
        "research": {"initial_cash": 100_000.0},
        "execution": {"additional_delay_days": delay_days},
    }


def _execute(closes: tuple[float, ...], delay_days: int):
    module = importlib.import_module("turtle_etf.strategy").MODULE
    prepared = module.prepare(_snapshot(closes), _config(delay_days))
    primary = run_vectorbt(prepared.ledger_input, prepared.primary_program)
    followup = module.followup_program(prepared, primary)
    if followup is None:
        execution = ExecutionBundle(primary, primary, ("primary",))
    else:
        final = run_vectorbt(prepared.ledger_input, followup)
        execution = ExecutionBundle(primary, final, ("primary", "followup"))
    extension = module.build_extensions(prepared, execution)[0]
    return execution, extension


def test_attribution_extension_uses_exact_fields_and_covers_each_order() -> None:
    execution, extension = _execute((10.0, 12.0, 13.0, 14.0), 0)
    rows = extension.table.to_pylist()

    assert extension.name == "turtle_etf"
    assert extension.schema_version == "turtle-etf-attribution/2"
    assert tuple(extension.table.schema.names) == ATTRIBUTION_FIELDS
    assert extension.unique_key == ("event_id",)
    assert len({row["event_id"] for row in rows}) == len(rows)
    assert [row["reason_code"] for row in rows if row["event_type"] == "decision"] == [
        "signal_entry"
    ]
    assert sum(row["event_type"] == "decision" for row in rows) == len(
        execution.final.ledger.orders
    )
    assert all(isinstance(json.loads(row["details_json"]), dict) for row in rows)


def test_delayed_attribution_preserves_planned_and_execution_dates() -> None:
    _, extension = _execute((10.0, 12.0, 13.0, 14.0), 1)
    decision = next(
        row for row in extension.table.to_pylist() if row["event_type"] == "decision"
    )
    details = json.loads(decision["details_json"])

    assert decision["time"] == "2026-01-08 09:30:00"
    assert details["planned_date"] == "2026-01-07"
    assert details["execution_date"] == "2026-01-08"
    assert details["delay_days"] == 1
    assert details["frozen_target_amount"] == decision["requested_amount"]
    assert details["execution_adjustment"] == "none"


def test_horizon_expired_order_is_attribution_only() -> None:
    execution, extension = _execute((10.0, 12.0, 13.0), 1)
    rows = extension.table.to_pylist()

    assert len(execution.final.ledger.orders) == 0
    assert len(rows) == 1
    assert rows[0]["event_type"] == "decision"
    assert rows[0]["executed_amount"] == 0.0
    details = json.loads(rows[0]["details_json"])
    assert details["execution_date"] is None
    assert details["execution_adjustment"] == "horizon_expired"


def test_corporate_action_is_extension_evidence_without_synthetic_order() -> None:
    module = importlib.import_module("turtle_etf.strategy").MODULE
    prepared = module.prepare(_split_snapshot(), _config(0))
    primary = run_vectorbt(prepared.ledger_input, prepared.primary_program)
    execution = ExecutionBundle(primary, primary, ("primary",))
    rows = module.build_extensions(prepared, execution)[0].table.to_pylist()

    event = next(row for row in rows if row["event_type"] == "corporate_action")
    details = json.loads(event["details_json"])
    assert event["reason_code"] == "corporate_action_applied"
    assert event["requested_amount"] is None
    assert event["executed_amount"] is None
    assert details["source_event_id"] == "FUND_DIVIDEND:101"
    assert details["split_ratio"] == 2.0
    assert details["application_date"] == "2026-01-07"
    assert all(str(order["comment"]) != "corporate_action" for order in primary.ledger.orders)


def test_delayed_cash_truncation_is_reported_from_shared_ledger_execution() -> None:
    execution, extension = _execute((10.0, 12.0, 13.0, 1000.0), 1)
    decision = next(
        row for row in extension.table.to_pylist() if row["event_type"] == "decision"
    )
    details = json.loads(decision["details_json"])

    assert len(execution.final.ledger.orders) == 0
    assert decision["requested_amount"] == 300.0
    assert decision["executed_amount"] == 0.0
    assert details["execution_adjustment"] == "cash_truncated"
    assert details["state_changed"] is False


def test_valuation_reconciles_addition_and_full_exit_to_public_value() -> None:
    execution, extension = _execute(
        (10.0, 12.0, 13.0, 14.0, 15.0, 10.0, 9.0),
        0,
    )
    rows = extension.table.to_pylist()
    decisions = [
        (row["reason_code"], json.loads(row["details_json"])["action"])
        for row in rows
        if row["event_type"] == "decision"
    ]
    valuations = [
        json.loads(row["details_json"])
        for row in rows
        if row["event_type"] == "valuation"
    ]

    assert decisions == [
        ("signal_entry", "entry"),
        ("signal_add", "addition"),
        ("signal_exit", "full_exit"),
    ]
    assert [item["security_daily_pnl"] for item in valuations] == pytest.approx(
        [-80.0, 300.0, 300.0, -1630.0, -605.0]
    )
    assert all(
        item["security_daily_pnl"] == pytest.approx(item["portfolio_daily_pnl"])
        for item in valuations
    )
    assert valuations[-1]["position_after"] == 0
    assert execution.final.ledger.value[-1]["total_value"] == pytest.approx(
        98_285.0
    )
