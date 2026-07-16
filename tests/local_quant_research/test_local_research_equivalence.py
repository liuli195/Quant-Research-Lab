from __future__ import annotations

import hashlib
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import numpy as np
import pyarrow as pa
import pytest


RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))

from scripts.research.market_data.query import open_snapshot  # noqa: E402
from turtle_etf.result_adapter import LocalExecutionFacts, to_joinquant_facts  # noqa: E402
from turtle_etf.vectorbt_cli import _market_frames  # noqa: E402
from turtle_etf.vectorbt_engine import run_vectorbt_simulation  # noqa: E402
from turtle_etf.vectorbt_inputs import prepare_simulation_inputs  # noqa: E402


SCENARIOS = (
    "immediate-11-etf",
    "immediate-17-etf",
    "delayed-11-etf-1d",
)

_SNAPSHOTS = {
    "immediate-11-etf": "e88238cca420a8ae66b90adb6cda4dd6c38a07390a13b8ac2f471e534742e33e",
    "immediate-17-etf": "27c0a452ad5cd8c7f865d2d8cd7555595df0b1fc7afecb86eace4b36336a7ddd",
    "delayed-11-etf-1d": "e88238cca420a8ae66b90adb6cda4dd6c38a07390a13b8ac2f471e534742e33e",
}
_EXPANDED_UNIVERSE = {
    "159980.XSHE": "commodity_futures",
    "159981.XSHE": "commodity_futures",
    "159985.XSHE": "commodity_futures",
    "511260.XSHG": "treasury_bond",
    "513030.XSHG": "developed_non_us_equity",
    "513800.XSHG": "developed_non_us_equity",
}
_LOGIC_DETAIL_KEYS = (
    "action",
    "delay_days",
    "execution_adjustment",
    "execution_date",
    "frozen_reason",
    "planned_date",
    "redistribution_state_changed",
    "source_reason",
    "state_changed",
)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _update_array_digest(
    digest: object,
    values: Iterable[object],
    *,
    floating: bool,
) -> None:
    items = list(values)
    valid = np.asarray([value is not None for value in items], dtype=np.uint8)
    if floating:
        array = np.asarray(
            [0.0 if value is None else float(value) for value in items],
            dtype="<f8",
        )
    else:
        array = np.asarray(
            [0 if value is None else int(value) for value in items],
            dtype="<i8",
        )
    digest.update(valid.tobytes())
    digest.update(array.tobytes())


def _table_digest(table: pa.Table, fields: tuple[str, ...]) -> dict[str, object]:
    digest = hashlib.sha256()
    digest.update(_canonical_json_bytes({"fields": fields, "rows": table.num_rows}))
    for name in fields:
        column = table[name].combine_chunks()
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        if pa.types.is_floating(column.type):
            _update_array_digest(digest, column.to_pylist(), floating=True)
        elif pa.types.is_integer(column.type):
            _update_array_digest(digest, column.to_pylist(), floating=False)
        else:
            digest.update(_canonical_json_bytes(column.to_pylist()))
    return {"rows": table.num_rows, "sha256": digest.hexdigest()}


def _state_digest(facts: LocalExecutionFacts) -> dict[str, object]:
    identities: list[dict[str, object]] = []
    unit_counts: list[object] = []
    common_stops: list[object] = []
    for row in facts.attribution.to_pylist():
        details = json.loads(str(row["details_json"]))
        if "unit_count_before" not in details:
            continue
        identities.append(
            {
                "reason_code": row["reason_code"],
                "security": row["security"],
                "time": row["time"],
            }
        )
        unit_counts.extend(
            (details["unit_count_before"], details["unit_count_after"])
        )
        common_stops.extend(
            (details["common_stop_before"], details["common_stop_after"])
        )
    digest = hashlib.sha256(_canonical_json_bytes(identities))
    _update_array_digest(digest, unit_counts, floating=False)
    _update_array_digest(digest, common_stops, floating=True)
    return {"events": len(identities), "sha256": digest.hexdigest()}


def logic_digest(facts: LocalExecutionFacts) -> str:
    orders = [
        {
            key: row[key]
            for key in (
                "action",
                "amount",
                "comment",
                "filled",
                "security",
                "side",
                "status",
                "time",
                "type",
            )
        }
        for row in facts.orders.to_pylist()
    ]
    attribution = []
    for row in facts.attribution.to_pylist():
        details = json.loads(str(row["details_json"]))
        attribution.append(
            {
                "details": {
                    key: details[key]
                    for key in _LOGIC_DETAIL_KEYS
                    if key in details
                },
                "event_type": row["event_type"],
                "reason_code": row["reason_code"],
                "scope": row["scope"],
                "security": row["security"],
                "time": row["time"],
            }
        )
    return hashlib.sha256(
        _canonical_json_bytes({"attribution": attribution, "orders": orders})
    ).hexdigest()


def _summarize_facts(
    facts: LocalExecutionFacts, scenario: str
) -> dict[str, object]:
    value_digest = hashlib.sha256()
    value_digest.update(
        _canonical_json_bytes(
            _table_digest(facts.balances, ("time", "total_value", "net_value"))
        )
    )
    value_digest.update(
        _canonical_json_bytes(_table_digest(facts.results, ("time", "returns")))
    )
    return {
        "schema_version": 1,
        "scenario": scenario,
        "orders": _table_digest(facts.orders, tuple(facts.orders.schema.names)),
        "fees": _table_digest(
            facts.orders, ("match_time", "security", "commission")
        ),
        "cash": _table_digest(facts.balances, ("time", "cash", "aval_cash")),
        "positions": _table_digest(
            facts.positions, tuple(facts.positions.schema.names)
        ),
        "value": {
            "rows": facts.results.num_rows,
            "sha256": value_digest.hexdigest(),
        },
        "state": _state_digest(facts),
        "logic": logic_digest(facts),
    }


def _scenario_config(scenario: str) -> dict[str, object]:
    config = json.loads((RESEARCH_ROOT / "baseline.json").read_text(encoding="utf-8"))
    config["scenario_id"] = scenario
    config["execution"]["additional_delay_days"] = (
        1 if scenario == "delayed-11-etf-1d" else 0
    )
    if scenario == "immediate-17-etf":
        config["universe"].extend(
            {"security": security, "asset_group": asset_group}
            for security, asset_group in sorted(_EXPANDED_UNIVERSE.items())
        )
    return config


@lru_cache(maxsize=None)
def _actual_scenario(repo_root_text: str, scenario: str) -> dict[str, object]:
    repo_root = Path(repo_root_text)
    config = _scenario_config(scenario)
    snapshot = open_snapshot(
        _SNAPSHOTS[scenario], root=repo_root / ".local/market-data"
    )
    prepared = prepare_simulation_inputs(
        _market_frames(snapshot.rows, config),
        config,
        corporate_actions=snapshot.corporate_actions,
        corporate_actions_digest=snapshot.corporate_actions_digest,
    )
    simulation = run_vectorbt_simulation(prepared, config)
    facts = to_joinquant_facts(prepared, simulation, scenario)
    return _summarize_facts(facts, scenario)


def assert_equivalent(
    actual: dict[str, object], expected: dict[str, object]
) -> None:
    assert actual["schema_version"] == 1
    assert actual["scenario"] == expected["scenario"]
    for key in ("orders", "fees", "cash", "positions", "value", "state", "logic"):
        assert actual[key] == expected[key]


def test_all_reference_scenarios_have_complete_equivalence_fixtures(
    repo_root: Path,
) -> None:
    fixture = json.loads(
        (
            repo_root
            / "tests/local_quant_research/fixtures/local-research-v1-baseline.json"
        ).read_text(encoding="utf-8")
    )

    assert fixture["schema_version"] == 1
    assert tuple(item["scenario"] for item in fixture["scenarios"]) == SCENARIOS
    assert all(
        set(item)
        == {"scenario", "orders", "fees", "cash", "positions", "value", "state", "logic"}
        for item in fixture["scenarios"]
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_old_vectorbt_path_matches_frozen_equivalence_fixture(
    repo_root: Path,
    scenario: str,
) -> None:
    fixture = json.loads(
        (
            repo_root
            / "tests/local_quant_research/fixtures/local-research-v1-baseline.json"
        ).read_text(encoding="utf-8")
    )
    expected = next(
        item for item in fixture["scenarios"] if item["scenario"] == scenario
    )

    assert_equivalent(_actual_scenario(str(repo_root), scenario), expected)
