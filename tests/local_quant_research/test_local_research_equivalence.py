from __future__ import annotations

import hashlib
import json
from pathlib import Path


SCENARIOS = (
    "immediate-11-etf",
    "immediate-17-etf",
    "delayed-11-etf-1d",
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
