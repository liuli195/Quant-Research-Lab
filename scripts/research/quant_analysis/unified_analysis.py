from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from scripts.research.analysis_data.manifest import open_analysis_source
from scripts.research.analysis_data.views import open_analysis_database
from scripts.research.market_data.query import open_snapshot

from .benchmarks import calculate_benchmark_statistics
from .cvar import calculate_cvar, rolling_compound_returns
from .evidence import ScenarioResult, build_evidence_matrix, evidence_digest
from .robustness import block_bootstrap, summarize_bootstrap
from .source_registry import RegisteredSource


BENCHMARK_IDS = (
    "CSI300_CNY_TOTAL_RETURN",
    "NASDAQ100_CNY_TOTAL_RETURN",
)
_FORMULA_VERSION = "unified-strategy-analysis/1"


class UnifiedAnalysisError(ValueError):
    """Raised when standard result packages cannot support deterministic analysis."""


def deterministic_next_action() -> str:
    return "generate_deterministic_local_report"


def _with_analysis_seconds(
    summary: Mapping[str, object],
    measured_seconds: float,
    output_path: Path,
) -> dict[str, object]:
    measured = float(measured_seconds)
    if not math.isfinite(measured) or measured < 0:
        raise UnifiedAnalysisError("analysis_seconds must be finite and non-negative")
    seconds = measured
    path = Path(output_path)
    if path.is_file():
        existing = _load_json(path, label="deterministic analysis")
        if existing.get("analysis_id") != summary.get("analysis_id"):
            raise UnifiedAnalysisError("existing deterministic analysis identity mismatch")
        prior = existing.get("analysis_seconds")
        if prior is not None:
            prior_seconds = float(prior)
            if not math.isfinite(prior_seconds) or prior_seconds < 0:
                raise UnifiedAnalysisError(
                    "existing analysis_seconds must be finite and non-negative"
                )
            seconds = prior_seconds
    result = dict(summary)
    result["analysis_seconds"] = seconds
    return result


@dataclass(frozen=True)
class ScenarioInput:
    scenario_id: str
    run_id: str
    result_dir: Path
    returns: pd.Series
    balances: pd.DataFrame
    positions: pd.DataFrame
    orders: pd.DataFrame
    events: pd.DataFrame
    params: Mapping[str, object]
    performance: Mapping[str, object]
    source_type: str = "local_research"
    source_manifest_sha256: str = ""
    capabilities: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    attribution_status: str = "available"


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnifiedAnalysisError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise UnifiedAnalysisError(f"{label} must be a JSON object")
    return value


def _safe_number(value: object) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, (np.integer, int)):
        return int(value)
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _numeric_metrics(values: Mapping[str, object]) -> dict[str, float | int | None]:
    return {str(key): _safe_number(value) for key, value in values.items()}


def calculate_return_metrics(returns: pd.Series, *, annualization: int = 252) -> dict[str, float | int | None]:
    values = np.asarray(returns, dtype=np.float64)
    if annualization <= 0 or values.ndim != 1 or values.size == 0:
        raise UnifiedAnalysisError("return series must be non-empty")
    if not np.isfinite(values).all() or np.any(values <= -1.0):
        raise UnifiedAnalysisError("return series contains invalid values")
    wealth = np.cumprod(1.0 + values)
    cumulative_return = float(wealth[-1] - 1.0)
    cagr = float((1.0 + cumulative_return) ** (annualization / values.size) - 1.0)
    volatility = float(np.std(values, ddof=1) * math.sqrt(annualization)) if values.size > 1 else 0.0
    mean_annual = float(np.mean(values) * annualization)
    downside_values = np.minimum(values, 0.0)
    downside = float(math.sqrt(float(np.mean(downside_values**2))) * math.sqrt(annualization))
    peaks = np.maximum(1.0, np.maximum.accumulate(wealth))
    drawdowns = wealth / peaks - 1.0
    maximum_drawdown = float(np.min(drawdowns))
    underwater = drawdowns < 0.0
    maximum_duration = 0
    current_duration = 0
    for value in underwater:
        current_duration = current_duration + 1 if value else 0
        maximum_duration = max(maximum_duration, current_duration)
    return {
        "observations": int(values.size),
        "cumulative_return": cumulative_return,
        "cagr": cagr,
        "annualized_volatility": volatility,
        "sharpe": None if volatility == 0.0 else mean_annual / volatility,
        "sortino": None if downside == 0.0 else mean_annual / downside,
        "max_drawdown": maximum_drawdown,
        "max_drawdown_duration": maximum_duration,
        "calmar": None if maximum_drawdown == 0.0 else cagr / abs(maximum_drawdown),
        "daily_hit_rate": float(np.mean(values[values != 0.0] > 0.0)) if np.any(values != 0.0) else None,
    }


def evaluate_metrics(
    metrics: Mapping[str, object], thresholds: Mapping[str, object]
) -> tuple[str, list[str]]:
    checks = (
        ("cagr_min_exclusive", "cagr", lambda value, limit: value > limit),
        (
            "max_drawdown_abs_max",
            "max_drawdown",
            lambda value, limit: abs(value) <= limit,
        ),
        ("calmar_min", "calmar", lambda value, limit: value >= limit),
    )
    reasons: list[str] = []
    for threshold_name, metric_name, predicate in checks:
        value = metrics.get(metric_name)
        if value is None or not predicate(float(value), float(thresholds[threshold_name])):
            reasons.append(threshold_name)
    return ("pass" if not reasons else "fail", reasons)


def align_three_way_benchmarks(
    strategy: pd.Series,
    benchmarks: Mapping[str, pd.Series],
) -> tuple[pd.DataFrame, dict[str, object]]:
    if tuple(benchmarks) != BENCHMARK_IDS:
        raise UnifiedAnalysisError("benchmark set must contain the two canonical benchmarks")
    common = set(pd.DatetimeIndex(strategy.index).normalize())
    for benchmark_id in BENCHMARK_IDS:
        common &= set(pd.DatetimeIndex(benchmarks[benchmark_id].index).normalize())
    common_index = pd.DatetimeIndex(sorted(common))
    if common_index.empty:
        raise UnifiedAnalysisError("strategy and benchmarks have no shared dates")
    aligned = pd.DataFrame(index=common_index)
    aligned["strategy"] = strategy.groupby(pd.DatetimeIndex(strategy.index).normalize()).last().reindex(common_index)
    for benchmark_id in BENCHMARK_IDS:
        normalized = benchmarks[benchmark_id].groupby(
            pd.DatetimeIndex(benchmarks[benchmark_id].index).normalize()
        ).last()
        aligned[benchmark_id] = normalized.reindex(common_index)
    if aligned.isna().any().any():
        raise UnifiedAnalysisError("benchmark alignment produced missing values")
    return aligned, {
        "alignment": "three-way exact-date inner join",
        "common_samples": len(aligned),
        "strategy_samples": len(strategy),
        "strategy_excluded_dates": len(strategy) - len(aligned),
        "benchmark_samples": {
            benchmark_id: len(benchmarks[benchmark_id]) for benchmark_id in BENCHMARK_IDS
        },
        "benchmark_excluded_dates": {
            benchmark_id: len(benchmarks[benchmark_id]) - len(aligned)
            for benchmark_id in BENCHMARK_IDS
        },
    }


def _find_attribution_file(result_dir: Path, manifest: Mapping[str, object]) -> Path:
    extensions = manifest.get("extensions")
    if not isinstance(extensions, Mapping):
        raise UnifiedAnalysisError("result package has no attribution extension")
    matches: list[Mapping[str, object]] = []
    for extension in extensions.values():
        if isinstance(extension, Mapping):
            entry = extension.get("attribution_log")
            if isinstance(entry, Mapping):
                matches.append(entry)
    if len(matches) != 1:
        raise UnifiedAnalysisError("result package must expose one attribution log")
    files = matches[0].get("files")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], Mapping):
        raise UnifiedAnalysisError("attribution log file declaration is invalid")
    relative = Path(str(files[0].get("path", "")))
    path = (result_dir / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(result_dir):
        raise UnifiedAnalysisError("attribution log path is unsafe")
    if not path.is_file() or _sha256(path) != files[0].get("sha256"):
        raise UnifiedAnalysisError("attribution log digest mismatch")
    return path


def _load_common_facts(
    result_dir: Path, *, snapshot_id: str | None = None
) -> tuple[pd.Series, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    with open_analysis_database(result_dir, snapshot_id=snapshot_id) as database:
        returns_frame = database.connection.sql(
            "select trading_date, daily_returns from strategy_daily_returns "
            "where comparable order by trading_date"
        ).fetchdf()
        balances = database.connection.sql(
            "select time, total_value, net_value, cash, aval_cash from balances order by time"
        ).fetchdf()
        positions = database.connection.sql("select * from positions order by time, security").fetchdf()
        orders = database.connection.sql("select * from orders order by time, security").fetchdf()
    returns = pd.Series(
        returns_frame["daily_returns"].astype(float).to_numpy(),
        index=pd.to_datetime(returns_frame["trading_date"]).dt.normalize(),
        name="return",
    )
    for frame in (balances, positions, orders):
        if "time" in frame:
            frame["date"] = pd.to_datetime(frame["time"]).dt.normalize()
    return returns, balances, positions, orders


def _load_scenario(result_dir: Path) -> ScenarioInput:
    result_dir = Path(result_dir).resolve()
    source = open_analysis_source(result_dir)
    manifest = dict(source.manifest)
    run = manifest.get("run")
    if not isinstance(run, Mapping):
        raise UnifiedAnalysisError("result run identity is missing")
    returns, balances, positions, orders = _load_common_facts(result_dir)
    events = pq.read_table(_find_attribution_file(result_dir, manifest)).to_pandas()
    events["event_time"] = pd.to_datetime(events["time"])
    events["date"] = events["event_time"].dt.normalize()
    params = _load_json(result_dir / "params.json", label="scenario params")
    performance = _load_json(result_dir / "performance.json", label="performance evidence")
    return ScenarioInput(
        scenario_id=str(run["scenario_id"]),
        run_id=str(run["run_id"]),
        result_dir=result_dir,
        returns=returns,
        balances=balances,
        positions=positions,
        orders=orders,
        events=events,
        params=params,
        performance=performance,
    )


def _registered_run_id(registered: RegisteredSource) -> str:
    identity = registered.source.manifest.get("object")
    if not isinstance(identity, Mapping):
        raise UnifiedAnalysisError("registered source identity is missing")
    key = "run_id" if registered.source.kind == "local_research" else "local_id"
    value = identity.get(key)
    if not isinstance(value, str) or not value:
        raise UnifiedAnalysisError("registered source run identity is missing")
    return value


def _registered_attribution_events(registered: RegisteredSource) -> pd.DataFrame:
    capability = registered.capabilities["attribution"]
    if capability.get("status") != "available":
        return pd.DataFrame()
    relative = Path(str(capability["path"]))
    path = (registered.registration.root / relative).resolve()
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not path.is_relative_to(registered.registration.root)
        or not path.is_file()
        or _sha256(path) != capability["sha256"]
    ):
        raise UnifiedAnalysisError("registered attribution evidence changed")
    time_field = str(capability["time_field"])
    event_field = str(capability["event_field"])
    reason_field = capability.get("reason_field")
    security_field = capability.get("security_field")
    columns = [
        time_field,
        event_field,
        *([str(reason_field)] if isinstance(reason_field, str) else []),
        *([str(security_field)] if isinstance(security_field, str) else []),
    ]
    frame = pq.read_table(path, columns=list(dict.fromkeys(columns))).to_pandas()
    event_time = pd.to_datetime(frame[time_field], errors="coerce", format="mixed")
    if event_time.isna().any():
        raise UnifiedAnalysisError("registered attribution evidence has invalid event times")
    events = pd.DataFrame(
        {
            "time": frame[time_field],
            "event_time": event_time,
            "date": event_time.dt.normalize(),
            "event_type": frame[event_field].fillna("").astype(str),
            "reason_code": (
                frame[str(reason_field)].fillna("").astype(str)
                if isinstance(reason_field, str)
                else ""
            ),
            "security": (
                frame[str(security_field)].fillna("").astype(str)
                if isinstance(security_field, str)
                else ""
            ),
        }
    )
    if events["event_type"].eq("").any():
        raise UnifiedAnalysisError("registered attribution evidence has invalid event identities")
    return events


def load_registered_scenario(
    registered: RegisteredSource, *, analysis_params: Mapping[str, object]
) -> ScenarioInput:
    registration = registered.registration
    returns, balances, positions, orders = _load_common_facts(
        registration.root, snapshot_id=registration.snapshot_id
    )
    attribution_status = str(registered.capabilities["attribution"]["status"])
    return ScenarioInput(
        scenario_id=registration.scenario_id,
        run_id=_registered_run_id(registered),
        result_dir=registration.root,
        returns=returns,
        balances=balances,
        positions=positions,
        orders=orders,
        events=_registered_attribution_events(registered),
        params=dict(analysis_params),
        performance={},
        source_type=registration.source_type,
        source_manifest_sha256=registration.manifest_sha256,
        capabilities=registered.capabilities,
        attribution_status=attribution_status,
    )


def _immutable_json(path: Path, value: object) -> None:
    if path.exists():
        existing = _load_json(path, label=path.name)
        if existing != value:
            raise UnifiedAnalysisError(f"immutable analysis identity collision: {path}")
        return
    _atomic_json(path, value)


def _register_source_results(
    repo_root: Path,
    preparation_workspace: Path,
    source_registry: Mapping[str, str],
) -> dict[str, object]:
    preparation_root = Path(preparation_workspace).resolve()
    expected_preparation_root = repo_root / ".local" / "strategy-analysis-preparations"
    if not preparation_root.is_relative_to(expected_preparation_root):
        raise UnifiedAnalysisError(
            "preparation workspace is outside .local/strategy-analysis-preparations"
        )
    preparation = _load_json(
        preparation_root / "preparation.json", label="analysis preparation"
    )
    preparation_id = str(preparation.get("preparation_id", ""))
    if not preparation_id or preparation_id != preparation_root.name:
        raise UnifiedAnalysisError("analysis preparation identity mismatch")
    scenarios = _load_json(
        preparation_root / "analysis-scenarios.json", label="analysis scenarios"
    )
    strategy_id = str(scenarios["strategy_id"])
    run_root = repo_root / ".local" / "quant-research" / strategy_id
    planned_scenarios = scenarios.get("scenarios")
    if not isinstance(planned_scenarios, Sequence) or isinstance(
        planned_scenarios, (str, bytes)
    ):
        raise UnifiedAnalysisError("analysis scenarios are invalid")
    if len(planned_scenarios) < 2:
        raise UnifiedAnalysisError(
            "source registry requires at least two planned scenarios"
        )
    scenario_ids = [str(scenario["scenario_id"]) for scenario in planned_scenarios]
    registered_ids = {str(key) for key in source_registry}
    if registered_ids != set(scenario_ids):
        raise UnifiedAnalysisError(
            "source registry must explicitly contain every planned scenario_id exactly once"
        )
    run_ids = [str(source_registry[scenario_id]) for scenario_id in scenario_ids]
    if any(not run_id or "/" in run_id or "\\" in run_id for run_id in run_ids):
        raise UnifiedAnalysisError("source registry contains an unsafe run_id")
    if len(run_ids) != len(set(run_ids)):
        raise UnifiedAnalysisError("source registry run_id values must be unique")

    sources: list[dict[str, object]] = []
    identities: list[dict[str, object]] = []
    for scenario in planned_scenarios:
        scenario_id = str(scenario["scenario_id"])
        run_id = str(source_registry[scenario_id])
        params_path = preparation_root / "scenario-configs" / scenario_id / "params.json"
        params_sha256 = _sha256(params_path)
        source_root = (run_root / run_id).resolve()
        if not source_root.is_relative_to(run_root):
            raise UnifiedAnalysisError("source registry run_id escapes the strategy run root")
        run_manifest_path = source_root / "run-manifest.json"
        run_manifest = _load_json(run_manifest_path, label="run manifest")
        inputs = run_manifest.get("inputs")
        snapshot = run_manifest.get("snapshot")
        if (
            run_manifest.get("status") != "complete"
            or run_manifest.get("project_id") != strategy_id
            or run_manifest.get("run_id") != run_id
            or not isinstance(inputs, Mapping)
            or inputs.get("project_config_sha256") != params_sha256
            or not isinstance(snapshot, Mapping)
        ):
            raise UnifiedAnalysisError(
                f"scenario {scenario_id} does not match its explicitly registered run"
            )
        code_identity = inputs.get("code_identity")
        execution = (
            code_identity.get("execution")
            if isinstance(code_identity, Mapping)
            else None
        )
        identity = {
            "snapshot_id": str(snapshot.get("snapshot_id", "")),
            "code_identity_sha256": str(inputs.get("code_identity_sha256", "")),
            "code_sha256": str(inputs.get("code_sha256", "")),
            "execution_backend": dict(execution) if isinstance(execution, Mapping) else {},
        }
        execution_dependencies = (
            execution.get("dependencies") if isinstance(execution, Mapping) else None
        )
        if (
            not identity["snapshot_id"]
            or not identity["code_identity_sha256"]
            or not identity["code_sha256"]
            or not identity["execution_backend"]
            or not isinstance(execution_dependencies, Mapping)
            or not execution_dependencies
        ):
            raise UnifiedAnalysisError(f"scenario {scenario_id} execution identity is incomplete")
        identities.append(identity)

        result_dir = source_root / "backtests" / f"local-{scenario_id}"
        local_manifest = _load_json(
            result_dir / "manifest.json", label="local result manifest"
        )
        local_run = local_manifest.get("run")
        local_source = local_manifest.get("source")
        local_engine = (
            local_source.get("engine")
            if isinstance(local_source, Mapping)
            else None
        )
        if (
            not isinstance(local_run, Mapping)
            or local_run.get("scenario_id") != scenario_id
            or local_run.get("run_id") != run_id
            or local_run.get("snapshot_id") != identity["snapshot_id"]
        ):
            raise UnifiedAnalysisError(
                f"scenario {scenario_id} local result identity mismatch"
            )
        if (
            not isinstance(local_engine, Mapping)
            or local_engine.get("backend") != execution.get("backend")
            or local_engine.get("adapter_version") != execution.get("adapter_version")
            or any(
                local_engine.get(str(name)) != version
                for name, version in execution_dependencies.items()
            )
        ):
            raise UnifiedAnalysisError(
                f"scenario {scenario_id} local result execution backend identity mismatch"
            )
        performance_path = result_dir / "performance.json"
        performance = _load_json(performance_path, label="performance evidence")
        cleanup = performance.get("cleanup")
        if (
            performance.get("status") != "pass"
            or performance.get("result_match") is not True
            or float(performance.get("cold_seconds", math.inf)) > 180.0
            or float(performance.get("warm_seconds", math.inf)) > 180.0
            or not isinstance(cleanup, Mapping)
            or not cleanup
            or not all(value is True for value in cleanup.values())
        ):
            raise UnifiedAnalysisError(f"scenario {scenario_id} failed its performance gate")
        sources.append(
            {
                "scenario_id": scenario_id,
                "dimension": str(scenario["dimension"]),
                "run_id": run_id,
                "run_manifest": run_manifest_path.relative_to(repo_root).as_posix(),
                "run_manifest_sha256": _sha256(run_manifest_path),
                "result_dir": result_dir.relative_to(repo_root).as_posix(),
                "result_manifest_sha256": _sha256(result_dir / "manifest.json"),
                "params_sha256": params_sha256,
                "output_set_sha256": str(run_manifest["output_set_sha256"]),
                "performance": {
                    "cold_seconds": float(performance["cold_seconds"]),
                    "warm_seconds": float(performance["warm_seconds"]),
                    "result_match": True,
                    "cleanup": dict(cleanup),
                },
            }
        )

    identity_names = (
        "snapshot_id",
        "code_identity_sha256",
        "code_sha256",
        "execution_backend",
    )
    for identity_name in identity_names:
        if any(
            identity[identity_name] != identities[0][identity_name]
            for identity in identities[1:]
        ):
            label = (
                "execution backend identity"
                if identity_name == "execution_backend"
                else identity_name
            )
            raise UnifiedAnalysisError(f"registered sources do not share one {label}")
    shared_identity = dict(identities[0])
    shared_identity["execution_backend_sha256"] = evidence_digest(
        shared_identity["execution_backend"]
    )
    source_registry_sha256 = evidence_digest(
        [
            {
                "scenario_id": source["scenario_id"],
                "run_id": source["run_id"],
                "run_manifest_sha256": source["run_manifest_sha256"],
                "result_manifest_sha256": source["result_manifest_sha256"],
                "output_set_sha256": source["output_set_sha256"],
            }
            for source in sources
        ]
    )
    analysis_id = evidence_digest(
        {
            "formula_version": _FORMULA_VERSION,
            "preparation_id": preparation_id,
            "source_registry_sha256": source_registry_sha256,
            "shared_identity": shared_identity,
        }
    )
    document = {
        "schema_version": "strategy-analysis-source-results/1",
        "strategy_id": strategy_id,
        "analysis_id": analysis_id,
        "preparation_id": preparation_id,
        "preparation_sha256": _sha256(preparation_root / "preparation.json"),
        "analysis_scenarios_sha256": _sha256(
            preparation_root / "analysis-scenarios.json"
        ),
        "source_registry": {
            "explicit": True,
            "scenario_count": len(scenario_ids),
            "run_id_count": len(run_ids),
            "sha256": source_registry_sha256,
        },
        "shared_identity": shared_identity,
        "sources": sources,
        "expected": len(planned_scenarios),
        "actual": len(sources),
        "source_mutation": "forbidden",
    }
    analysis_root = repo_root / ".local" / "strategy-analysis" / analysis_id
    _immutable_json(analysis_root / "preparation.json", preparation)
    _immutable_json(analysis_root / "analysis-scenarios.json", scenarios)
    _immutable_json(analysis_root / "source-results.json", document)
    return document


def _valuation_facts(scenario: ScenarioInput) -> pd.DataFrame:
    columns = [
        "date",
        "security",
        "reason_code",
        "source_reason",
        "security_daily_pnl",
        "common_stop_after",
        "stop_failure_loss",
    ]
    if scenario.events.empty:
        return pd.DataFrame(columns=columns)
    if "event_type" not in scenario.events or "details_json" not in scenario.events:
        raise UnifiedAnalysisError("attribution log does not expose valuation facts")
    events = scenario.events.loc[
        scenario.events["event_type"] == "valuation"
    ].copy()
    if events.empty:
        return pd.DataFrame(columns=columns)
    if "date" not in events:
        time_column = "event_time" if "event_time" in events else "time"
        events["date"] = pd.to_datetime(events[time_column]).dt.normalize()
    records: list[dict[str, object]] = []
    for event in events.to_dict("records"):
        try:
            details = json.loads(str(event["details_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise UnifiedAnalysisError("valuation details_json is invalid") from exc
        if not isinstance(details, Mapping):
            raise UnifiedAnalysisError("valuation details_json must be an object")
        try:
            security_pnl = float(details["security_daily_pnl"])
        except (KeyError, TypeError, ValueError) as exc:
            raise UnifiedAnalysisError(
                "valuation event has no security_daily_pnl"
            ) from exc
        if not np.isfinite(security_pnl):
            raise UnifiedAnalysisError("valuation security_daily_pnl is invalid")
        records.append(
            {
                "date": pd.Timestamp(event["date"]).normalize(),
                "security": str(event["security"]),
                "reason_code": str(event["reason_code"]),
                "source_reason": str(
                    details.get("source_reason", event["reason_code"])
                ),
                "security_daily_pnl": security_pnl,
                "common_stop_after": _safe_number(
                    details.get("common_stop_after")
                ),
                "stop_failure_loss": _safe_number(
                    details.get("stop_failure_loss")
                ),
            }
        )
    facts = pd.DataFrame.from_records(records, columns=columns)
    if facts.duplicated(["date", "security"]).any():
        raise UnifiedAnalysisError(
            "valuation facts contain duplicate security dates"
        )
    return facts.sort_values(["date", "security"]).reset_index(drop=True)


def _position_facts(
    scenario: ScenarioInput, universe: Mapping[str, str]
) -> pd.DataFrame:
    positions = scenario.positions.copy()
    if positions.empty:
        return positions
    positions["asset_group"] = positions["security"].map(universe)
    if positions["asset_group"].isna().any():
        raise UnifiedAnalysisError("position security is absent from analysis universe")
    equity = scenario.balances.set_index("date")["total_value"].astype(float)
    positions["equity"] = positions["date"].map(equity)
    positions["market_value"] = positions["amount"].astype(float) * positions["price"].astype(float)
    positions["weight"] = positions["market_value"] / positions["equity"]
    valuations = _valuation_facts(scenario).rename(
        columns={
            "source_reason": "attribution_reason",
            "common_stop_after": "common_stop",
        }
    )
    if valuations.empty:
        positions["attribution_reason"] = "unclassified"
        positions["common_stop"] = np.nan
        positions["stop_failure_loss"] = np.nan
        return positions
    annotated = positions.merge(
        valuations[
            [
                "date",
                "security",
                "attribution_reason",
                "common_stop",
                "stop_failure_loss",
            ]
        ],
        on=["date", "security"],
        how="left",
        validate="one_to_one",
    )
    annotated["attribution_reason"] = annotated["attribution_reason"].fillna(
        "unclassified"
    )
    return annotated


def _security_pnl_facts(
    scenario: ScenarioInput, universe: Mapping[str, str]
) -> pd.DataFrame:
    facts = _valuation_facts(scenario)
    if facts.empty:
        return pd.DataFrame(
            columns=[
                *facts.columns,
                "asset_group",
                "previous_equity",
                "return_contribution",
                "attribution_reason",
            ]
        )
    facts["asset_group"] = facts["security"].map(universe)
    if facts["asset_group"].isna().any():
        raise UnifiedAnalysisError(
            "valuation security is absent from analysis universe"
        )
    balances = scenario.balances.set_index("date")["total_value"].astype(float)
    previous_equity = balances.shift(1)
    first_date = balances.index.min()
    first_day_pnl = float(
        facts.loc[facts["date"] == first_date, "security_daily_pnl"].sum()
    )
    previous_equity.loc[first_date] = float(balances.loc[first_date]) - first_day_pnl
    facts["previous_equity"] = facts["date"].map(previous_equity)
    if facts["previous_equity"].isna().any() or (
        facts["previous_equity"] <= 0.0
    ).any():
        raise UnifiedAnalysisError(
            "valuation facts cannot resolve previous equity"
        )
    facts["return_contribution"] = (
        facts["security_daily_pnl"].astype(float)
        / facts["previous_equity"].astype(float)
    )
    facts["attribution_reason"] = facts["source_reason"]
    return facts


def _risk_metrics(scenario: ScenarioInput, positions: pd.DataFrame) -> dict[str, object]:
    balances = scenario.balances.copy()
    balances["invested_ratio"] = (
        (balances["total_value"].astype(float) - balances["cash"].astype(float))
        / balances["total_value"].astype(float)
    )
    risk = scenario.params.get("risk", {})
    if not isinstance(risk, Mapping):
        raise UnifiedAnalysisError("scenario risk config is invalid")
    if positions.empty:
        max_security_weight = max_group_weight = 0.0
        planned_coverage = 0.0
        max_planned_loss_ratio = None
    else:
        max_security_weight = float(positions["weight"].max())
        group_weights = positions.groupby(["date", "asset_group"])["weight"].sum()
        max_group_weight = float(group_weights.max())
        planned = positions.loc[positions["common_stop"].notna()].copy()
        planned_coverage = float(len(planned) / len(positions))
        if planned.empty:
            max_planned_loss_ratio = None
        else:
            planned["planned_loss"] = (
                (
                    planned["avg_cost"].astype(float)
                    - planned["common_stop"].astype(float)
                )
                .clip(lower=0.0)
                * planned["amount"].astype(float)
            )
            daily_risk = planned.groupby("date")["planned_loss"].sum()
            equity = balances.set_index("date")["total_value"].astype(float)
            loss_ratio = daily_risk / equity.reindex(daily_risk.index)
            max_planned_loss_ratio = float(loss_ratio.max())
    rolling_vol = scenario.returns.rolling(60, min_periods=60).std(ddof=1) * math.sqrt(252)
    filled_orders = scenario.orders.loc[
        (scenario.orders["status"] == "done") & (scenario.orders["filled"].astype(float) > 0)
    ]
    closed = filled_orders.loc[filled_orders["action"] == "close"]
    average_equity = float(balances["total_value"].mean())
    filled_notional = float(
        (filled_orders["filled"].astype(float) * filled_orders["price"].astype(float)).sum()
    )
    decision_events = (
        scenario.events.loc[scenario.events["event_type"] == "decision"]
        if "event_type" in scenario.events
        else scenario.events.iloc[0:0]
    )
    event_reason_codes = (
        decision_events["reason_code"]
        if "reason_code" in decision_events
        else pd.Series(dtype="object")
    )
    maximum_effective_risk_units: float | None = None
    maximum_portfolio_unit_utilization: float | None = None
    if not decision_events.empty and "details_json" in decision_events:
        for raw_details in decision_events["details_json"]:
            try:
                details = json.loads(str(raw_details))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(details, Mapping):
                continue
            effective = _safe_number(details.get("effective_risk_units"))
            if effective is None:
                continue
            effective_value = float(effective)
            maximum_effective_risk_units = (
                effective_value
                if maximum_effective_risk_units is None
                else max(maximum_effective_risk_units, effective_value)
            )
            cap = _safe_number(
                details.get(
                    "portfolio_unit_cap", risk.get("portfolio_unit_cap")
                )
            )
            if cap is not None and float(cap) > 0.0:
                utilization = effective_value / float(cap)
                maximum_portfolio_unit_utilization = (
                    utilization
                    if maximum_portfolio_unit_utilization is None
                    else max(
                        maximum_portfolio_unit_utilization, utilization
                    )
                )
    return {
        "average_invested_ratio": float(balances["invested_ratio"].mean()),
        "median_invested_ratio": float(balances["invested_ratio"].median()),
        "below_half_ratio": float((balances["invested_ratio"] < 0.5).mean()),
        "near_full_ratio": float((balances["invested_ratio"] >= 0.9).mean()),
        "average_cash_ratio": float((1.0 - balances["invested_ratio"]).mean()),
        "maximum_invested_ratio": float(balances["invested_ratio"].max()),
        "maximum_security_weight": max_security_weight,
        "maximum_asset_group_weight": max_group_weight,
        "planned_risk_coverage": planned_coverage,
        "maximum_planned_loss_ratio": max_planned_loss_ratio,
        "maximum_effective_risk_units": maximum_effective_risk_units,
        "maximum_portfolio_unit_utilization": (
            maximum_portfolio_unit_utilization
        ),
        "maximum_realized_60d_volatility": _safe_number(rolling_vol.max()),
        "filled_order_count": int(len(filled_orders)),
        "rejected_order_count": int((scenario.orders["status"] != "done").sum()),
        "turnover": None if average_equity == 0.0 else filled_notional / average_equity,
        "fees": float(filled_orders["commission"].astype(float).sum()),
        "closed_order_count": int(len(closed)),
        "closed_order_win_rate": float((closed["gains"].astype(float) > 0.0).mean()) if len(closed) else None,
        "closed_order_realized_gains": float(closed["gains"].astype(float).sum()),
        "protective_stop_events": int(
            (event_reason_codes == "protective_stop").sum()
        ),
        "redistribution_event_count": int(
            (event_reason_codes == "full_position_redistribution").sum()
        ),
    }


def _performance(scenario: ScenarioInput, positions: pd.DataFrame) -> dict[str, object]:
    return {**calculate_return_metrics(scenario.returns), **_risk_metrics(scenario, positions)}


def _group_contribution(rows: pd.DataFrame, key: str) -> list[dict[str, object]]:
    values = rows.groupby(key)["return_contribution"].sum().sort_values(key=lambda item: item.abs(), ascending=False)
    return [{"key": str(index), "contribution": float(value)} for index, value in values.items()]


def _attribution(scenario: ScenarioInput, pnl_facts: pd.DataFrame) -> dict[str, object]:
    daily_asset = pnl_facts.groupby("date")["return_contribution"].sum() if not pnl_facts.empty else pd.Series(dtype=float)
    residual = scenario.returns.subtract(daily_asset, fill_value=0.0)
    total_target = float(scenario.returns.sum())
    residual_total = float(residual.sum())

    def with_residual(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        completed = [*rows, {"key": "cash_fees_and_unclassified", "contribution": residual_total}]
        error = sum(float(row["contribution"]) for row in completed) - total_target
        if abs(error) > 1e-10:
            raise UnifiedAnalysisError("attribution does not reconcile")
        return completed

    security = with_residual(_group_contribution(pnl_facts, "security") if not pnl_facts.empty else [])
    groups = with_residual(_group_contribution(pnl_facts, "asset_group") if not pnl_facts.empty else [])
    reasons = with_residual(_group_contribution(pnl_facts, "attribution_reason") if not pnl_facts.empty else [])
    period_rows = pnl_facts.copy()
    if not period_rows.empty:
        period_rows["period"] = period_rows["date"].dt.strftime("%Y")
    periods = with_residual(_group_contribution(period_rows, "period") if not period_rows.empty else [])
    decision_events = (
        scenario.events.loc[scenario.events["event_type"] == "decision"]
        if "event_type" in scenario.events
        else scenario.events.iloc[0:0]
    )
    event_counts = {
        str(key): int(value)
        for key, value in decision_events.groupby("reason_code")
        .size()
        .sort_values(ascending=False)
        .items()
    }
    return {
        "method": "arithmetic daily PnL contribution divided by prior equity",
        "portfolio_arithmetic_return": total_target,
        "security": security,
        "asset_group": groups,
        "trading_reason": reasons,
        "period": periods,
        "exposure": with_residual(
            [{"key": "invested_assets", "contribution": float(daily_asset.sum())}]
        ),
        "event_counts": event_counts,
        "event_count_scope": "decision events only",
        "reconciliation_error": 0.0,
        "limitation": "arithmetic attribution is not geometric linking",
    }


def _benchmark_series(path: Path) -> dict[str, pd.Series]:
    frame = pq.read_table(path).to_pandas()
    frame["date"] = pd.to_datetime(frame["time"]).dt.normalize()
    output: dict[str, pd.Series] = {}
    for benchmark_id in BENCHMARK_IDS:
        rows = frame.loc[frame["benchmark_id"] == benchmark_id].sort_values("date")
        if rows.empty:
            raise UnifiedAnalysisError(f"benchmark is missing: {benchmark_id}")
        output[benchmark_id] = pd.Series(
            rows["returns"].astype(float).to_numpy(), index=rows["date"], name=benchmark_id
        )
    return output


def _period_result(
    scenario_id: str,
    dimension: str,
    returns: pd.Series,
    thresholds: Mapping[str, object],
) -> tuple[dict[str, object], ScenarioResult]:
    metrics = calculate_return_metrics(returns)
    status, reasons = evaluate_metrics(metrics, thresholds)
    document = {
        "scenario_id": scenario_id,
        "dimension": dimension,
        "status": status,
        "reasons": reasons,
        "metrics": metrics,
        "start": returns.index.min().date().isoformat(),
        "end": returns.index.max().date().isoformat(),
    }
    evidence = ScenarioResult(
        scenario_id=scenario_id,
        dimension=dimension,
        status=status,
        metrics=_numeric_metrics(metrics),
        input_sha256=evidence_digest(
            {"scenario_id": scenario_id, "returns": returns.astype(float).tolist()}
        ),
        reasons=tuple(reasons),
    )
    return document, evidence


def _fixed_and_rolling(
    baseline: ScenarioInput,
    analyses: Mapping[str, object],
    thresholds: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    for period in analyses["fixed_periods"]:
        selected = baseline.returns.loc[str(period["start"]): str(period["end"])]
        if selected.empty:
            raise UnifiedAnalysisError(f"fixed period has no observations: {period['id']}")
        row, result = _period_result(
            f"period-{period['id']}", "fixed_period", selected, thresholds
        )
        rows.append(row)
        evidence.append(result)

    rolling = analyses["rolling"]
    start = baseline.returns.index.min()
    last = baseline.returns.index.max()
    while start + pd.DateOffset(years=int(rolling["window_years"])) <= last:
        stop = start + pd.DateOffset(years=int(rolling["window_years"])) - pd.Timedelta(days=1)
        selected = baseline.returns.loc[start:stop]
        scenario_id = f"rolling-{int(rolling['window_years'])}y-{start.date().isoformat()}"
        row, result = _period_result(scenario_id, "rolling_period", selected, thresholds)
        rows.append(row)
        evidence.append(result)
        start = start + pd.DateOffset(months=int(rolling["step_months"]))
    return rows, evidence


def _deletion_sensitivity(
    baseline: ScenarioInput,
    security_pnl: pd.DataFrame,
    universe: Mapping[str, str],
    thresholds: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    definitions = (
        ("asset_delete_security", "security", sorted(universe)),
        ("asset_delete_group", "asset_group", sorted(set(universe.values()))),
    )
    for dimension, key, values in definitions:
        for value in values:
            contribution = (
                security_pnl.loc[security_pnl[key] == value]
                .groupby("date")["return_contribution"]
                .sum()
                .reindex(baseline.returns.index, fill_value=0.0)
            )
            adjusted = baseline.returns.subtract(contribution)
            scenario_id = f"delete-{key}-{str(value).lower().replace('.', '-').replace('_', '-')}"
            metrics = calculate_return_metrics(adjusted)
            status, reasons = evaluate_metrics(metrics, thresholds)
            row = {
                "scenario_id": scenario_id,
                "dimension": dimension,
                "removed": str(value),
                "method": "contribution-removal sensitivity; no capital reallocation",
                "status": status,
                "reasons": reasons,
                "metrics": metrics,
            }
            rows.append(row)
            evidence.append(
                ScenarioResult(
                    scenario_id=scenario_id,
                    dimension=dimension,
                    status=status,
                    metrics=_numeric_metrics(metrics),
                    input_sha256=evidence_digest(
                        {"scenario_id": scenario_id, "returns": adjusted.tolist()}
                    ),
                    reasons=tuple(reasons),
                )
            )
    return rows, evidence


def _market_open_lookup(repo_root: Path, snapshot_id: str) -> dict[str, pd.Series]:
    snapshot = open_snapshot(snapshot_id, root=repo_root / ".local" / "market-data")
    frame = pd.DataFrame([dict(row) for row in snapshot.rows])
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    return {
        str(security): rows.sort_values("date").set_index("date")["open"].astype(float)
        for security, rows in frame.groupby("security")
    }


def _cost_sensitivity(
    repo_root: Path,
    baseline: ScenarioInput,
    definitions: Sequence[Mapping[str, object]],
    thresholds: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    filled = baseline.orders.loc[
        (baseline.orders["status"] == "done") & (baseline.orders["filled"].astype(float) > 0)
    ].copy()
    equity = baseline.balances.set_index("date")["total_value"].astype(float).shift(1)
    snapshot_id = _load_json(baseline.result_dir / "manifest.json", label="result manifest")["run"]["snapshot_id"]
    opens = _market_open_lookup(repo_root, str(snapshot_id))
    rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    for definition in definitions:
        adjustments: dict[pd.Timestamp, float] = {}
        delayed = 0
        delay_missing = 0
        for order in filled.to_dict("records"):
            date = pd.Timestamp(order["date"]).normalize()
            quantity = float(order["filled"])
            price = float(order["price"])
            notional = quantity * price
            pnl_adjustment = -(
                (float(definition["commission_multiplier"]) - 1.0) * float(order["commission"])
                + float(definition["slippage"]) * notional
            )
            delay_days = int(definition["delay_days"])
            if delay_days:
                series = opens.get(str(order["security"]))
                if series is None or date not in series.index:
                    delay_missing += 1
                else:
                    location = series.index.get_loc(date)
                    target = location + delay_days
                    if target >= len(series):
                        delay_missing += 1
                    else:
                        delayed_price = float(series.iloc[target])
                        pnl_adjustment += (
                            -(delayed_price - price) * quantity
                            if order["action"] == "open"
                            else (delayed_price - price) * quantity
                        )
                        delayed += 1
            adjustments[date] = adjustments.get(date, 0.0) + pnl_adjustment
        adjusted = baseline.returns.copy()
        for date, adjustment in adjustments.items():
            denominator = equity.get(date)
            if denominator is not None and pd.notna(denominator) and denominator != 0:
                adjusted.loc[date] += adjustment / float(denominator)
        metrics = calculate_return_metrics(adjusted)
        status, reasons = evaluate_metrics(metrics, thresholds)
        if delay_missing:
            status = "evidence_insufficient"
            reasons = ["missing_delayed_open"]
        scenario_id = f"cost-{definition['id']}"
        row = {
            "scenario_id": scenario_id,
            "dimension": "cost_execution",
            "method": "first-order order-level cost and delayed-open sensitivity",
            "status": status,
            "reasons": reasons,
            "metrics": metrics,
            "delayed_orders": delayed,
            "missing_delayed_orders": delay_missing,
        }
        rows.append(row)
        evidence.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension="cost_execution",
                status=status,
                metrics={**_numeric_metrics(metrics), "delayed_orders": delayed, "missing_delayed_orders": delay_missing},
                input_sha256=evidence_digest(
                    {"definition": dict(definition), "returns": adjusted.tolist()}
                ),
                reasons=tuple(reasons),
            )
        )
    return rows, evidence


def _bootstrap(
    returns: pd.Series, definition: Mapping[str, object]
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    thresholds = definition["thresholds"]
    for block_size in definition["block_sizes"]:
        paths = block_bootstrap(
            returns.to_numpy(dtype=np.float64),
            block_size=int(block_size),
            paths=int(definition["paths"]),
            horizon=int(definition["horizon_days"]),
            seed=int(definition["seed"]),
        )
        summary = summarize_bootstrap(paths)
        del paths
        reasons: list[str] = []
        if summary["probability_drawdown_over_20pct"] > float(thresholds["probability_drawdown_over_20pct_max"]):
            reasons.append("probability_drawdown_over_20pct")
        if summary["probability_drawdown_over_30pct"] > float(thresholds["probability_drawdown_over_30pct_max"]):
            reasons.append("probability_drawdown_over_30pct")
        if summary["median_terminal_return"] <= float(thresholds["median_terminal_return_min_exclusive"]):
            reasons.append("median_terminal_return")
        scenario_id = f"bootstrap-block-{block_size}"
        metrics = {
            **summary,
            "block_size": int(block_size),
            "paths": int(definition["paths"]),
            "horizon_days": int(definition["horizon_days"]),
            "seed": int(definition["seed"]),
        }
        status = "fail" if reasons else "pass"
        rows.append(
            {"scenario_id": scenario_id, "dimension": "block_bootstrap", "status": status, "reasons": reasons, "metrics": metrics}
        )
        evidence.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension="block_bootstrap",
                status=status,
                metrics=_numeric_metrics(metrics),
                input_sha256=evidence_digest(
                    {"definition": dict(definition), "block_size": block_size, "returns": returns.tolist()}
                ),
                reasons=tuple(reasons),
            )
        )
    return rows, evidence


def _historical_stress(
    returns: pd.Series, definitions: Sequence[Mapping[str, object]]
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    for definition in definitions:
        selected = returns.loc[str(definition["start"]): str(definition["end"])]
        metrics = calculate_return_metrics(selected)
        passed = abs(float(metrics["max_drawdown"])) <= float(definition["max_drawdown_abs_max"])
        status = "pass" if passed else "fail"
        reasons = [] if passed else ["max_drawdown_abs_max"]
        scenario_id = str(definition["id"])
        rows.append(
            {"scenario_id": scenario_id, "dimension": "historical_stress", "status": status, "reasons": reasons, "metrics": metrics}
        )
        evidence.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension="historical_stress",
                status=status,
                metrics=_numeric_metrics(metrics),
                input_sha256=evidence_digest({"definition": dict(definition), "returns": selected.tolist()}),
                reasons=tuple(reasons),
            )
        )
    return rows, evidence


def _position_shocks(
    positions: pd.DataFrame, definitions: Sequence[Mapping[str, object]]
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    for definition in definitions:
        scenario_id = str(definition["id"])
        if definition.get("use_stop_failure_loss") is True:
            if (
                "stop_failure_loss" not in positions
                or "equity" not in positions
                or positions["stop_failure_loss"].isna().any()
            ):
                status = "evidence_insufficient"
                reasons = ["stop_failure_loss_missing_at_source"]
                metrics = {"evaluated_dates": 0, "worst_account_loss": None}
            else:
                daily_loss = positions.groupby("date")["stop_failure_loss"].sum()
                daily_equity = positions.groupby("date")["equity"].first()
                loss_ratio = daily_loss / daily_equity
                worst = float(loss_ratio.max()) if len(loss_ratio) else 0.0
                passed = worst <= float(definition["maximum_loss_abs_max"]) + 1e-12
                status = "pass" if passed else "fail"
                reasons = [] if passed else ["maximum_loss_abs_max"]
                metrics = {
                    "evaluated_dates": len(loss_ratio),
                    "worst_account_loss": worst,
                }
        else:
            losses: list[float] = []
            for _, daily in positions.groupby("date"):
                shock_return = 0.0
                for row in daily.to_dict("records"):
                    shock = definition.get("security_shocks", {}).get(
                        str(row["security"]),
                        definition["asset_group_shocks"].get(str(row["asset_group"])),
                    )
                    if shock is None:
                        raise UnifiedAnalysisError("position shock does not cover every asset group")
                    shock_return += float(row["weight"]) * float(shock)
                losses.append(max(0.0, -shock_return))
            worst = max(losses) if losses else 0.0
            passed = worst <= float(definition["maximum_loss_abs_max"]) + 1e-12
            status = "pass" if passed else "fail"
            reasons = [] if passed else ["maximum_loss_abs_max"]
            metrics = {"evaluated_dates": len(losses), "worst_account_loss": worst}
        rows.append(
            {"scenario_id": scenario_id, "dimension": "position_shock", "status": status, "reasons": reasons, "metrics": metrics}
        )
        evidence.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension="position_shock",
                status=status,
                metrics=_numeric_metrics(metrics),
                input_sha256=evidence_digest({"definition": dict(definition), "positions": len(positions)}),
                reasons=tuple(reasons),
            )
        )
    return rows, evidence


def _cvar(
    returns: pd.Series, definitions: Sequence[Mapping[str, object]]
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    base = returns.to_numpy(dtype=np.float64)
    for definition in definitions:
        horizon = int(definition["horizon_days"])
        samples = base if horizon == 1 else rolling_compound_returns(base, window=horizon)
        tail = float(len(samples) * (1.0 - float(definition["confidence"])))
        sufficient = tail >= float(definition["minimum_tail_observations"])
        value = calculate_cvar(samples, float(definition["confidence"])) if sufficient else None
        passed = value is not None and value <= float(definition["maximum_loss_abs_max"])
        status = "evidence_insufficient" if not sufficient else ("pass" if passed else "fail")
        reasons = ["insufficient_tail_observations"] if not sufficient else ([] if passed else ["maximum_loss_abs_max"])
        metrics = {
            "cvar": value,
            "confidence": float(definition["confidence"]),
            "horizon_days": horizon,
            "samples": len(samples),
            "tail_observations": tail,
        }
        scenario_id = str(definition["id"])
        rows.append(
            {"scenario_id": scenario_id, "dimension": "cvar", "status": status, "reasons": reasons, "metrics": metrics}
        )
        evidence.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension="cvar",
                status=status,
                metrics=_numeric_metrics(metrics),
                input_sha256=evidence_digest({"definition": dict(definition), "returns": base.tolist()}),
                reasons=tuple(reasons),
            )
        )
    return rows, evidence


def run_deterministic_analysis(
    repo_root: Path,
    preparation_workspace: Path,
    source_registry: Mapping[str, str],
) -> dict[str, object]:
    started = time.perf_counter()
    root = Path(repo_root).resolve()
    sources = _register_source_results(root, preparation_workspace, source_registry)
    analysis_root = root / ".local" / "strategy-analysis" / str(sources["analysis_id"])
    preparation = _load_json(analysis_root / "preparation.json", label="analysis preparation")
    expanded = _load_json(analysis_root / "analysis-scenarios.json", label="analysis scenarios")
    if preparation.get("preparation_id") != sources["preparation_id"]:
        raise UnifiedAnalysisError("analysis preparation identity mismatch")
    source_by_id = {str(item["scenario_id"]): item for item in sources["sources"]}
    scenarios = {
        scenario_id: _load_scenario(root / str(source["result_dir"]))
        for scenario_id, source in source_by_id.items()
    }
    baseline = scenarios["baseline"]
    universe = expanded["universe"]
    thresholds = expanded["thresholds"]
    positions = _position_facts(baseline, universe)
    security_pnl = _security_pnl_facts(baseline, universe)
    baseline_metrics = _performance(baseline, positions)
    baseline_status, baseline_reasons = evaluate_metrics(baseline_metrics, thresholds)

    challenge_rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    for plan_scenario in expanded["scenarios"]:
        scenario_id = str(plan_scenario["scenario_id"])
        current = scenarios[scenario_id]
        current_positions = _position_facts(current, universe)
        metrics = _performance(current, current_positions)
        status, reasons = evaluate_metrics(metrics, thresholds)
        challenge_rows.append(
            {
                "scenario_id": scenario_id,
                "dimension": str(plan_scenario["dimension"]),
                "status": status,
                "reasons": reasons,
                "metrics": metrics,
                "delta_vs_baseline": {
                    key: (
                        None
                        if metrics.get(key) is None or baseline_metrics.get(key) is None
                        else float(metrics[key]) - float(baseline_metrics[key])
                    )
                    for key in ("cagr", "max_drawdown", "calmar", "average_invested_ratio")
                },
                "cold_seconds": float(current.performance["cold_seconds"]),
                "warm_seconds": float(current.performance["warm_seconds"]),
            }
        )
        evidence.append(
            ScenarioResult(
                scenario_id=f"parameter-{scenario_id}",
                dimension="baseline" if scenario_id == "baseline" else "parameter",
                status=status,
                metrics=_numeric_metrics(metrics),
                input_sha256=evidence_digest(
                    {"scenario_id": scenario_id, "run_id": current.run_id, "metrics": _numeric_metrics(metrics)}
                ),
                reasons=tuple(reasons),
            )
        )

    benchmark_manifest = root / str(preparation["benchmark_set"]["manifest"])
    benchmark_document = _load_json(benchmark_manifest, label="benchmark set manifest")
    benchmark_data = benchmark_manifest.parent / str(benchmark_document["data"]["path"])
    benchmarks = _benchmark_series(benchmark_data)
    aligned, alignment = align_three_way_benchmarks(baseline.returns, benchmarks)
    benchmark_statistics = {
        benchmark_id: calculate_benchmark_statistics(
            {date.date().isoformat(): float(value) for date, value in aligned["strategy"].items()},
            {date.date().isoformat(): float(value) for date, value in aligned[benchmark_id].items()},
        )
        for benchmark_id in BENCHMARK_IDS
    }

    analyses = expanded["analyses"]
    period_rows, period_evidence = _fixed_and_rolling(baseline, analyses, thresholds)
    deletion_rows, deletion_evidence = _deletion_sensitivity(
        baseline, security_pnl, universe, thresholds
    )
    cost_rows, cost_evidence = _cost_sensitivity(root, baseline, analyses["cost_execution"], thresholds)
    bootstrap_rows, bootstrap_evidence = _bootstrap(baseline.returns, analyses["bootstrap"])
    stress_rows, stress_evidence = _historical_stress(baseline.returns, analyses["historical_stress"])
    shock_rows, shock_evidence = _position_shocks(positions, analyses["position_shocks"])
    cvar_rows, cvar_evidence = _cvar(baseline.returns, analyses["cvar"])
    evidence.extend(
        [
            *period_evidence,
            *deletion_evidence,
            *cost_evidence,
            *bootstrap_evidence,
            *stress_evidence,
            *shock_evidence,
            *cvar_evidence,
        ]
    )
    evidence_path = build_evidence_matrix(evidence, analysis_root / "evidence-matrix.parquet")

    failures = [result.to_document() for result in evidence if result.status == "fail"]
    insufficient = [
        result.to_document() for result in evidence if result.status == "evidence_insufficient"
    ]
    severe_dimensions = {"block_bootstrap", "position_shock", "cvar"}
    severe_failures = [row for row in failures if row["dimension"] in severe_dimensions]
    advance = baseline_status == "pass" and not severe_failures
    recommendation = {
        "decision": "recommend_joinquant_confirmation" if advance else "revise_before_joinquant",
        "baseline_status": baseline_status,
        "baseline_reasons": baseline_reasons,
        "severe_failure_count": len(severe_failures),
        "evidence_insufficient_count": len(insufficient),
        "authority": "local_exploratory",
    }
    opposing_evidence: list[dict[str, object]] = []
    for benchmark_id, metrics in benchmark_statistics.items():
        if float(metrics["active_return"]) < 0:
            opposing_evidence.append(
                {"kind": "benchmark_underperformance", "benchmark_id": benchmark_id, "active_return": metrics["active_return"]}
            )
    opposing_evidence.extend(
        {"kind": "scenario_failure", "scenario_id": row["scenario_id"], "dimension": row["dimension"], "reasons": row["reasons"]}
        for row in failures
    )
    opposing_evidence.extend(
        {"kind": "evidence_insufficient", "scenario_id": row["scenario_id"], "dimension": row["dimension"], "reasons": row["reasons"]}
        for row in insufficient
    )
    summary = {
        "schema_version": "deterministic-strategy-analysis/1",
        "formula_version": _FORMULA_VERSION,
        "analysis_id": analysis_root.name,
        "strategy_id": expanded["strategy_id"],
        "authority": "local_exploratory",
        "not_formal_joinquant_backtest": True,
        "sources": {
            "source_results": "source-results.json",
            "benchmark_set_id": preparation["benchmark_set"]["benchmark_set_id"],
            "analysis_plan_sha256": expanded["analysis_plan_sha256"],
        },
        "baseline": {
            "status": baseline_status,
            "reasons": baseline_reasons,
            "metrics": baseline_metrics,
            "risk_control": _risk_metrics(baseline, positions),
        },
        "benchmarks": {"alignment": alignment, "statistics": benchmark_statistics},
        "attribution": _attribution(baseline, security_pnl),
        "challenge_results": challenge_rows,
        "robustness": {
            "periods": period_rows,
            "asset_deletions": deletion_rows,
            "cost_execution": cost_rows,
            "bootstrap": bootstrap_rows,
            "historical_stress": stress_rows,
            "position_shocks": shock_rows,
            "cvar": cvar_rows,
        },
        "evidence_matrix": {
            "path": evidence_path.relative_to(analysis_root).as_posix(),
            "sha256": _sha256(evidence_path),
            "rows": len(evidence),
            "pass": sum(result.status == "pass" for result in evidence),
            "fail": len(failures),
            "evidence_insufficient": len(insufficient),
        },
        "opposing_evidence": opposing_evidence,
        "pre_vibe_recommendation": recommendation,
        "next_action": deterministic_next_action(),
    }
    analysis_path = analysis_root / "deterministic-analysis.json"
    summary = _with_analysis_seconds(
        summary,
        time.perf_counter() - started,
        analysis_path,
    )
    _atomic_json(analysis_path, summary)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic analysis over standard result packages")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--preparation-workspace", type=Path, required=True)
    parser.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="SCENARIO_ID=RUN_ID",
        help="explicitly register one scenario result; repeat for every planned scenario",
    )
    return parser


def _parse_source_registry(values: Sequence[str]) -> dict[str, str]:
    registry: dict[str, str] = {}
    for value in values:
        scenario_id, separator, run_id = value.partition("=")
        if not separator or not scenario_id or not run_id:
            raise UnifiedAnalysisError(
                "--source must use the SCENARIO_ID=RUN_ID form"
            )
        if scenario_id in registry:
            raise UnifiedAnalysisError(
                f"scenario {scenario_id} is registered more than once"
            )
        registry[scenario_id] = run_id
    return registry


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = run_deterministic_analysis(
        args.repo_root,
        args.preparation_workspace,
        _parse_source_registry(args.source),
    )
    print(
        json.dumps(
            {
                "analysis_id": result["analysis_id"],
                "status": "complete",
                "next_action": result["next_action"],
                "baseline": result["baseline"],
                "evidence_matrix": result["evidence_matrix"],
                "analysis_seconds": result["analysis_seconds"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
