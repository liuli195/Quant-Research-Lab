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
from scripts.research.local_quant_research.contracts import ExecutionBundle  # noqa: E402
from scripts.research.local_quant_research.vectorbt_runtime import run_vectorbt  # noqa: E402
from turtle_etf.strategy import MODULE  # noqa: E402


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
_ANALYSIS_VIEWS = (
    "results",
    "balances",
    "positions",
    "orders",
    "risk",
    "period_risks",
    "strategy_daily_returns",
    "source_benchmark_returns",
)
_V1_FIXTURE = "tests/local_quant_research/fixtures/local-research-v1-baseline.json"
_MODULE_FIXTURE = (
    "tests/local_quant_research/fixtures/strategy-module-v1-baseline.json"
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


def _structured_array_digest(
    array: np.ndarray,
    fields: tuple[str, ...] | None = None,
) -> dict[str, object]:
    records = np.asarray(array)
    names = tuple(records.dtype.names or ())
    selected = names if fields is None else fields
    if not selected or not set(selected).issubset(names):
        raise AssertionError("structured public ledger array fields are incomplete")
    digest = hashlib.sha256()
    digest.update(_canonical_json_bytes({"fields": selected, "rows": len(records)}))
    for name in selected:
        values = records[name]
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        if np.issubdtype(values.dtype, np.floating):
            _update_array_digest(digest, values.tolist(), floating=True)
        elif np.issubdtype(values.dtype, np.integer):
            _update_array_digest(digest, values.tolist(), floating=False)
        else:
            digest.update(_canonical_json_bytes(values.tolist()))
    return {"rows": len(records), "sha256": digest.hexdigest()}


def _state_digest(attribution: pa.Table) -> dict[str, object]:
    identities: list[dict[str, object]] = []
    unit_counts: list[object] = []
    common_stops: list[object] = []
    for row in attribution.to_pylist():
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


def logic_digest(orders: np.ndarray, attribution: pa.Table) -> str:
    orders = [
        {
            key: row[key].item() if isinstance(row[key], np.generic) else row[key]
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
        for row in np.asarray(orders)
    ]
    attribution_rows = []
    for row in attribution.to_pylist():
        normalized = dict(row)
        normalized["details_json"] = json.loads(str(row["details_json"]))
        attribution_rows.append(normalized)
    return hashlib.sha256(
        _canonical_json_bytes(
            {"attribution": attribution_rows, "orders": orders}
        )
    ).hexdigest()


def _summarize_public_result(
    execution: ExecutionBundle,
    extension: object,
    scenario: str,
) -> dict[str, object]:
    ledger = execution.final.ledger
    attribution = extension.table
    return {
        "scenario": scenario,
        "orders": _structured_array_digest(ledger.orders),
        "fees": _structured_array_digest(
            ledger.orders, ("match_time", "security", "commission")
        ),
        "cash": _structured_array_digest(ledger.cash),
        "positions": _structured_array_digest(ledger.assets),
        "value": _structured_array_digest(ledger.value),
        "state": _state_digest(attribution),
        "logic": logic_digest(ledger.orders, attribution),
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


def _require_fixed_machine_snapshots(repo_root: Path) -> None:
    snapshot_root = repo_root / ".local/market-data/snapshots"
    missing = sorted(
        snapshot_id
        for snapshot_id in set(_SNAPSHOTS.values())
        if not (snapshot_root / f"{snapshot_id}.json").is_file()
    )
    if missing:
        pytest.skip(
            "fixed-machine recomputation requires private market-data snapshots: "
            + ", ".join(missing)
            + "; committed fixture structure and digest linkage remain verified"
        )


@lru_cache(maxsize=None)
def _actual_execution(
    repo_root_text: str, scenario: str
) -> tuple[ExecutionBundle, object, dict[str, object], str]:
    repo_root = Path(repo_root_text)
    _require_fixed_machine_snapshots(repo_root)
    config = _scenario_config(scenario)
    snapshot = open_snapshot(
        _SNAPSHOTS[scenario], root=repo_root / ".local/market-data"
    )
    prepared = MODULE.prepare(snapshot, config)
    primary = run_vectorbt(prepared.ledger_input, prepared.primary_program)
    followup = MODULE.followup_program(prepared, primary)
    if followup is None:
        execution = ExecutionBundle(primary, primary, ("primary",))
    else:
        final = run_vectorbt(prepared.ledger_input, followup)
        execution = ExecutionBundle(primary, final, ("primary", "followup"))
    extension = MODULE.build_extensions(prepared, execution)[0]
    return execution, extension, config, snapshot.corporate_actions_digest


def _actual_scenario(repo_root_text: str, scenario: str) -> dict[str, object]:
    execution, extension, _, _ = _actual_execution(repo_root_text, scenario)
    return _summarize_public_result(execution, extension, scenario)


def assert_equivalent(
    actual: dict[str, object], expected: dict[str, object]
) -> None:
    assert actual["scenario"] == expected["scenario"]
    for key in ("orders", "fees", "cash", "positions", "value", "state", "logic"):
        assert actual[key] == expected[key]


def test_historical_v1_fixture_keeps_materialized_contract_provenance(
    repo_root: Path,
) -> None:
    fixture = json.loads(
        (repo_root / _V1_FIXTURE).read_text(encoding="utf-8")
    )

    assert fixture["schema_version"] == 1
    assert set(fixture["materialized_contract"]) == {"analysis_data_views"}
    assert tuple(item["scenario"] for item in fixture["scenarios"]) == SCENARIOS
    assert all(
        set(item)
        == {
            "scenario",
            "orders",
            "fees",
            "cash",
            "positions",
            "value",
            "state",
            "logic",
            "materialized",
        }
        for item in fixture["scenarios"]
    )
    views_sha256 = hashlib.sha256(
        _canonical_json_bytes(
            fixture["materialized_contract"]["analysis_data_views"]
        )
    ).hexdigest()
    for item in fixture["scenarios"]:
        materialized = item["materialized"]
        assert set(materialized) == {
            "manifest",
            "config_identity",
            "code_identity",
            "analysis_data_views_sha256",
        }
        assert materialized["analysis_data_views_sha256"] == views_sha256
        assert materialized["manifest"]["schema_version"] == "local-backtest/1"
        assert len(materialized["manifest"]["sha256"]) == 64
        assert materialized["code_identity"]["path"] == "code.py"
        assert materialized["code_identity"]["bytes"] > 0
        assert len(materialized["code_identity"]["sha256"]) == 64
        current = materialized["config_identity"]["current"]
        version = materialized["config_identity"]["version"]
        assert current["path"] == "params.json"
        assert current["bytes"] == version["bytes"] > 0
        assert current["sha256"] == version["sha256"]
        assert version["path"] == (
            f"params_versions/{current['sha256']}.json"
        )


def test_strategy_module_fixture_covers_only_public_ledger_and_extension(
    repo_root: Path,
) -> None:
    fixture = json.loads(
        (repo_root / _MODULE_FIXTURE).read_text(encoding="utf-8")
    )
    v1_bytes = (repo_root / _V1_FIXTURE).read_bytes()

    assert fixture["schema_version"] == "strategy-module-baseline/1"
    assert fixture["public_contract"] == {
        "ledger": "ExecutionLedger",
        "extension": "ResultExtension:turtle_etf",
    }
    assert fixture["migrated_from"] == {
        "path": _V1_FIXTURE,
        "sha256": hashlib.sha256(v1_bytes).hexdigest(),
    }
    assert tuple(item["scenario"] for item in fixture["scenarios"]) == SCENARIOS
    assert all(
        set(item)
        == {"scenario", "orders", "fees", "cash", "positions", "value", "state", "logic"}
        for item in fixture["scenarios"]
    )


def test_logic_digest_covers_complete_attribution_rows_and_details() -> None:
    order = np.array(
        [
            (
                "open",
                100,
                "entry",
                100,
                "ETF-A",
                "long",
                "held",
                "2026-01-05",
                "market",
            )
        ],
        dtype=[
            ("action", "U16"),
            ("amount", "i8"),
            ("comment", "U64"),
            ("filled", "i8"),
            ("security", "U64"),
            ("side", "U16"),
            ("status", "U16"),
            ("time", "U32"),
            ("type", "U16"),
        ],
    )
    attribution = {
        "time": "2026-01-05",
        "event_id": "event-1",
        "scope": "security",
        "security": "ETF-A",
        "event_type": "decision",
        "reason_code": "signal_entry",
        "requested_amount": 100,
        "executed_amount": 100,
        "reference_price": 10.0,
        "risk_before": 0.0,
        "risk_after": 1.0,
        "details_json": json.dumps(
            {
                "candidate_units": 1,
                "planned_amount": 100,
                "position_before": 0,
                "position_after": 100,
                "stop_failure": None,
            },
            sort_keys=True,
        ),
    }

    def digest(**changes: object) -> str:
        row = {**attribution, **changes}
        return logic_digest(order, pa.Table.from_pylist([row]))

    assert len(
        {
            digest(),
            digest(event_id="event-2"),
            digest(
                details_json=json.dumps(
                    {
                        **json.loads(attribution["details_json"]),
                        "planned_amount": 200,
                    },
                    sort_keys=True,
                )
            ),
        }
    ) == 3


def test_missing_private_snapshots_are_an_explicit_fixed_machine_gate(
    tmp_path: Path,
) -> None:
    with pytest.raises(pytest.skip.Exception) as skipped:
        _actual_scenario(str(tmp_path), SCENARIOS[0])
    assert str(skipped.value) == (
        "fixed-machine recomputation requires private market-data snapshots: "
        + ", ".join(sorted(set(_SNAPSHOTS.values())))
        + "; committed fixture structure and digest linkage remain verified"
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_strategy_module_matches_frozen_equivalence_fixture(
    repo_root: Path,
    scenario: str,
) -> None:
    fixture = json.loads(
        (repo_root / _MODULE_FIXTURE).read_text(encoding="utf-8")
    )
    expected = next(
        item for item in fixture["scenarios"] if item["scenario"] == scenario
    )

    assert_equivalent(_actual_scenario(str(repo_root), scenario), expected)
