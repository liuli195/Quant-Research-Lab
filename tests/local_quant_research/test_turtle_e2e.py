from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest


RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))

from turtle_etf.execution import (  # noqa: E402
    DailyMarket,
    ExecutionCosts,
    MarketQuote,
    TradingDay,
    process_day,
)
from turtle_etf.cli import run_research  # noqa: E402
from turtle_etf.reporting import (  # noqa: E402
    OutputValidationError,
    RunIdentity,
    validate_project_outputs,
)
from turtle_etf.risk import (  # noqa: E402
    CovarianceEstimate,
    PortfolioState,
    RiskInputs,
)
from turtle_etf.state import (  # noqa: E402
    OrderIntent,
    apply_entry_fill,
)
from scripts.research.market_data.contracts import SnapshotSelection  # noqa: E402
from scripts.research.market_data.query import (  # noqa: E402
    MARKET_DATA_FIELDS,
    open_snapshot,
)
from scripts.research.market_data.storage import create_snapshot, import_batch  # noqa: E402
from scripts.research.quant_analysis.contracts import (  # noqa: E402
    STANDARD_TABLES,
    validate_analysis_bundle,
    write_analysis_table,
)
from scripts.research.quant_analysis.evidence import (  # noqa: E402
    validate_evidence_matrix,
)


def _sell(
    security: str,
    action: str,
    quantity: int,
    *,
    price: str = "10",
) -> OrderIntent:
    return OrderIntent(
        security=security,
        asset_group="group-a",
        action=action,
        quantity=quantity,
        expected_price=Decimal(price),
        signal_date="2026-01-05",
        execution_date="2026-01-06",
        reason="test_sell",
    )


def _buy(
    security: str,
    action: str = "entry",
    *,
    price: str = "10",
    signal_n: str = "1",
) -> OrderIntent:
    return OrderIntent(
        security=security,
        asset_group="group-a",
        action=action,
        quantity=100,
        expected_price=Decimal(price),
        signal_date="2026-01-05",
        execution_date="2026-01-06",
        signal_n=Decimal(signal_n),
        standard_unit=100,
        common_stop_after=Decimal(price) - Decimal("2") * Decimal(signal_n),
        reason="test_buy",
    )


def _position(security: str, quantity: int, *, signal_n: str = "1"):
    return apply_entry_fill(
        security=security,
        asset_group="group-a",
        execution_date="2026-01-02",
        fill_price=Decimal("10"),
        quantity=quantity,
        signal_n=Decimal(signal_n),
        standard_unit=100,
    )


def _risk_inputs(securities: tuple[str, ...]) -> RiskInputs:
    ordered = tuple(sorted(securities))
    covariance = CovarianceEstimate(
        securities=ordered,
        matrix=tuple(
            tuple(
                Decimal("0.000001") if left == right else Decimal("0")
                for right in range(len(ordered))
            )
            for left in range(len(ordered))
        ),
        aligned_samples=60,
        window_days=60,
    )
    return RiskInputs(
        prices={security: Decimal("10") for security in securities},
        median_turnover_20d={
            security: Decimal("1000000000") for security in securities
        },
        covariance=covariance,
        security_risk_cap=Decimal("1"),
        security_value_cap=Decimal("1"),
        asset_group_risk_cap=Decimal("1"),
        asset_group_value_cap=Decimal("1"),
        portfolio_risk_cap=Decimal("1"),
        portfolio_value_cap=Decimal("1"),
        target_volatility=Decimal("1"),
    )


def test_day_flow_executes_exit_reduction_then_same_level_buys_at_actual_open() -> None:
    exit_position = _position("EXIT", 200)
    reduce_position = _position("RED", 400)
    add_position = replace(
        _position("ADD", 100, signal_n="0.5"),
        last_add_request_date="2026-01-05",
    )
    state = PortfolioState(
        equity=Decimal("100000"),
        cash=Decimal("93000"),
        positions=(add_position, exit_position, reduce_position),
    )
    day = TradingDay(
        date="2026-01-06",
        intents=(
            _buy("NEW"),
            _buy("EXIT", action="addition"),
            _sell("RED", "mandatory_risk_reduction", 100),
            _buy("ADD", action="addition", signal_n="0.5"),
            _sell("EXIT", "full_exit", 200),
        ),
    )
    market = DailyMarket(
        quotes={
            "EXIT": MarketQuote(open=Decimal("11")),
            "RED": MarketQuote(open=Decimal("9")),
            "ADD": MarketQuote(open=Decimal("12")),
            "NEW": MarketQuote(open=Decimal("8")),
        },
        risk_inputs=_risk_inputs(("EXIT", "RED", "ADD", "NEW")),
    )

    first = process_day(day, state, market)
    second = process_day(day, state, market)

    filled_actions = [record.action for record in first.audit if record.status == "filled"]
    assert filled_actions == [
        "full_exit",
        "mandatory_risk_reduction",
        "addition",
        "entry",
    ]
    assert any(
        record.security == "EXIT"
        and record.action == "addition"
        and record.status == "cancelled"
        for record in first.audit
    )
    positions = {position.security: position for position in first.portfolio.positions}
    assert set(positions) == {"ADD", "NEW", "RED"}
    assert positions["RED"].quantity == 300
    assert positions["ADD"].quantity == 200
    assert positions["ADD"].common_stop == Decimal("11.0")
    assert positions["NEW"].common_stop == Decimal("6")
    assert first.portfolio.cash == Decimal("94080")
    assert first.audit_sha256 == second.audit_sha256


def test_paused_limits_and_missing_open_never_create_fills() -> None:
    held = _position("LOW", 100)
    state = PortfolioState(
        equity=Decimal("100000"),
        cash=Decimal("99000"),
        positions=(held,),
    )
    day = TradingDay(
        date="2026-01-06",
        intents=(
            _buy("PAUSED"),
            _buy("HIGH"),
            _buy("MISSING"),
            _sell("LOW", "full_exit", 100),
        ),
    )
    market = DailyMarket(
        quotes={
            "PAUSED": MarketQuote(open=Decimal("10"), paused=True),
            "HIGH": MarketQuote(open=Decimal("10"), high_limit=Decimal("10")),
            "MISSING": MarketQuote(open=None),
            "LOW": MarketQuote(open=Decimal("8"), low_limit=Decimal("8")),
        },
        risk_inputs=_risk_inputs(("PAUSED", "HIGH", "MISSING", "LOW")),
    )

    result = process_day(day, state, market)

    assert all(record.status == "unfilled" for record in result.audit)
    assert result.portfolio == state
    assert result.allocation.allocations == ()


def test_gap_open_recomputes_commission_before_cash_gate() -> None:
    intent = OrderIntent(
        security="GAP",
        asset_group="group-a",
        action="entry",
        quantity=10000,
        expected_price=Decimal("1"),
        signal_date="2026-01-05",
        execution_date="2026-01-06",
        signal_n=Decimal("0.1"),
        standard_unit=10000,
        common_stop_after=Decimal("0.8"),
        estimated_fee=Decimal("5"),
        reason="entry_breakout",
    )
    state = PortfolioState(Decimal("100006"), Decimal("100006"))
    market = DailyMarket(
        quotes={"GAP": MarketQuote(open=Decimal("10"))},
        risk_inputs=_risk_inputs(("GAP",)),
    )

    result = process_day(
        TradingDay(date="2026-01-06", intents=(intent,)),
        state,
        market,
    )

    assert result.portfolio.cash == Decimal("997.585000")
    assert result.portfolio.cash >= 0
    assert result.audit[0].status == "filled"
    assert result.audit[0].filled_quantity == 9900
    assert result.allocation.allocations[0].estimated_fee == Decimal("8.415000")


def test_gap_exit_recomputes_commission_from_actual_quantity_and_open() -> None:
    position = _position("GAP", 10000)
    state = PortfolioState(
        equity=Decimal("100000"),
        cash=Decimal("0"),
        positions=(position,),
    )
    day = TradingDay(
        date="2026-01-06",
        intents=(_sell("GAP", "full_exit", 10000, price="1"),),
    )
    market = DailyMarket(
        quotes={"GAP": MarketQuote(open=Decimal("10"))},
        risk_inputs=_risk_inputs(("GAP",)),
    )

    result = process_day(day, state, market)

    assert result.portfolio.cash == Decimal("99991.500000")
    assert result.portfolio.positions == ()
    assert result.audit[0].filled_quantity == 10000


def test_execution_cost_override_changes_fill_prices_fees_and_cash() -> None:
    state = PortfolioState(Decimal("100000"), Decimal("100000"))
    day = TradingDay(date="2026-01-06", intents=(_buy("COST"),))
    market = DailyMarket(
        quotes={"COST": MarketQuote(open=Decimal("10"))},
        risk_inputs=_risk_inputs(("COST",)),
    )

    baseline = process_day(day, state, market)
    stressed = process_day(
        day,
        state,
        market,
        costs=ExecutionCosts(
            commission_multiplier=Decimal("2"),
            one_way_slippage=Decimal("0.01"),
        ),
    )

    assert baseline.audit[0].fill_price == Decimal("10")
    assert baseline.audit[0].fee == Decimal("5")
    assert stressed.audit[0].fill_price == Decimal("10.10")
    assert stressed.audit[0].fee == Decimal("10")
    assert stressed.portfolio.cash < baseline.portfolio.cash


UNIVERSE = (
    "510300.XSHG",
    "512100.XSHG",
    "512480.XSHG",
    "159819.XSHE",
    "516160.XSHG",
    "513100.XSHG",
    "513180.XSHG",
    "515180.XSHG",
    "516080.XSHG",
    "518880.XSHG",
    "511010.XSHG",
)


def _remove_empty_test_roots(repo_root: Path, market_root: Path) -> None:
    for path in (market_root / "snapshots", market_root / "batches"):
        try:
            path.rmdir()
        except OSError:
            pass
    if not (market_root / "snapshots").exists() and not (market_root / "batches").exists():
        try:
            (market_root / ".market-data.lock").unlink(missing_ok=True)
            market_root.rmdir()
        except OSError:
            pass
    for path in (
        repo_root / ".local/e2e-tests",
        repo_root / ".local/quant-research",
    ):
        try:
            path.rmdir()
        except OSError:
            pass


def _business_dates(start: date, count: int) -> list[str]:
    values: list[str] = []
    current = start
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _research_snapshot(
    tmp_path: Path,
    *,
    market_root: Path | None = None,
    export_code_sha256: str = "a" * 64,
    start_date: date = date(2025, 1, 2),
    cold_security_rows: int = 80,
):
    dates = _business_dates(start_date, 90)
    csv_path = tmp_path / "daily.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MARKET_DATA_FIELDS, lineterminator="\n")
        writer.writeheader()
        previous = {security: Decimal("10") for security in UNIVERSE}
        for index, row_date in enumerate(dates):
            for security in UNIVERSE:
                if (
                    security == "516080.XSHG"
                    and index < len(dates) - cold_security_rows
                ):
                    continue
                close = Decimal("10")
                high = Decimal("11")
                low = Decimal("9")
                open_price = Decimal("10")
                if security == "510300.XSHG" and index == 70:
                    close = high = Decimal("12")
                elif security == "510300.XSHG" and index == 71:
                    open_price = close = high = Decimal("12")
                    low = Decimal("11")
                elif security == "510300.XSHG" and index == 76:
                    open_price = close = low = Decimal("8")
                    high = Decimal("9")
                writer.writerow(
                    {
                        "date": row_date,
                        "security": security,
                        "open": str(open_price),
                        "high": str(high),
                        "low": str(low),
                        "close": str(close),
                        "pre_close": str(previous[security]),
                        "volume": "100000000",
                        "money": "1000000000",
                        "factor": "1",
                        "paused": "0",
                        "high_limit": "20",
                        "low_limit": "1",
                    }
                )
                previous[security] = close
    market_root = tmp_path / "market-data" if market_root is None else market_root
    batch = import_batch(
        csv_path=csv_path,
        manifest={
            "schema_version": 1,
            "source": {"name": "joinquant", "environment": "research"},
            "asset_type": "etf",
            "frequency": "1d",
            "fields": list(MARKET_DATA_FIELDS),
            "price_semantics": {"fq": None, "skip_paused": False},
            "export_code_sha256": export_code_sha256,
        },
        root=market_root,
    )
    selection = SnapshotSelection(
        source={"name": "joinquant", "environment": "research"},
        asset_type="etf",
        frequency="1d",
        securities=UNIVERSE,
        start_date=dates[0],
        end_date=dates[-1],
        fields=MARKET_DATA_FIELDS,
        price_semantics={"fq": None, "skip_paused": False},
    )
    snapshot = create_snapshot(
        batch_ids=(batch.batch_id,),
        selection=selection,
        root=market_root,
    )
    return snapshot, market_root


def _run_identity(snapshot_id: str) -> RunIdentity:
    return RunIdentity(
        run_id="1" * 64,
        snapshot_id=snapshot_id,
        code_sha256="2" * 64,
        config_sha256="3" * 64,
    )


def _benchmark_input(snapshot, market_root: Path, root: Path) -> Path:
    dates = sorted(
        {str(row["date"]) for row in open_snapshot(snapshot.snapshot_id, root=market_root).rows}
    )
    levels = {
        "csi300_total_return_cny": 100.0,
        "nasdaq100_total_return_cny": 100.0,
    }
    rows: list[dict[str, object]] = []
    for index, current_date in enumerate(dates):
        returns = {
            "csi300_total_return_cny": 0.0 if index == 0 else (0.001 if index % 2 else -0.0005),
            "nasdaq100_total_return_cny": 0.0 if index == 0 else (0.0015 if index % 3 else -0.0007),
        }
        for benchmark_id, value in returns.items():
            if index:
                levels[benchmark_id] *= 1.0 + value
            rows.append(
                {
                    "date": current_date,
                    "benchmark_id": benchmark_id,
                    "currency": "CNY",
                    "total_return_index": levels[benchmark_id],
                    "return": value,
                    "source_id": f"fixture:{benchmark_id}",
                }
            )
    return write_analysis_table("benchmarks", rows, root)


def test_project_research_writes_reports_conclusion_candidates_and_audits(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    snapshot, market_root = _research_snapshot(tmp_path)
    output_dir = tmp_path / "output"
    config_path = (
        repo_root / "joinquant/strategies/strategy-003/research/baseline.json"
    )
    identity = _run_identity(snapshot.snapshot_id)
    benchmark_input = _benchmark_input(snapshot, market_root, tmp_path / "benchmark")

    result = run_research(
        config_path,
        snapshot.path,
        output_dir,
        market_data_root=market_root,
        benchmark_input=benchmark_input,
        identity=identity,
    )

    assert result.status == "complete"
    assert {path.name for path in output_dir.iterdir()} == {
        "project-status.json",
        "daily-audit.csv",
        "trades.csv",
        "positions.csv",
        "risk.csv",
        "research-report.md",
        "conclusion.json",
            "candidate-strategies.json",
            "local-evidence-matrix.parquet",
            *(f"{name}.parquet" for name in STANDARD_TABLES),
        }
    evidence = validate_evidence_matrix(output_dir / "local-evidence-matrix.parquet")
    assert {
        "parameter",
        "fixed_period",
        "asset_delete_etf",
        "asset_delete_group",
        "cost_execution",
        "block_bootstrap",
        "historical_stress",
        "position_shock",
        "cvar",
    }.issubset({row.dimension for row in evidence})
    by_id = {row.scenario_id: row for row in evidence}
    assert by_id["cost-high-slippage"].metrics["cagr"] < by_id[
        "cost-double-commission"
    ].metrics["cagr"]
    assert by_id["execution-delay-one-day"].metrics != by_id[
        "cost-high-slippage"
    ].metrics
    conclusion = json.loads((output_dir / "conclusion.json").read_text(encoding="utf-8"))
    candidates = json.loads(
        (output_dir / "candidate-strategies.json").read_text(encoding="utf-8")
    )
    report = (output_dir / "research-report.md").read_text(encoding="utf-8")
    assert conclusion["identity"] == identity.to_document()
    assert conclusion["metrics"]["filled_trades"] >= 2
    assert "cumulative_return" in conclusion["metrics"]
    assert "max_drawdown" in conclusion["metrics"]
    assert set(conclusion["benchmark_statistics"]) == {
        "csi300_total_return_cny",
        "nasdaq100_total_return_cny",
    }
    bundle = validate_analysis_bundle(output_dir)
    assert tuple(bundle.tables) == STANDARD_TABLES
    assert conclusion["recommendation"] in {
        "proceed_to_joinquant",
        "revise_and_reassess",
        "stop_evidence_insufficient",
    }
    assert len(candidates["candidates"]) == 7
    assert {item["code_sha256"] for item in candidates["candidates"]} == {
        identity.code_sha256
    }
    assert {item["snapshot_id"] for item in candidates["candidates"]} == {
        snapshot.snapshot_id
    }
    assert all("rank" not in item and "score" not in item for item in candidates["candidates"])
    with (output_dir / "trades.csv").open(encoding="utf-8", newline="") as handle:
        actions = {row["action"] for row in csv.DictReader(handle)}
    assert {"entry", "full_exit"}.issubset(actions)
    for required in (
        "方法",
        "输入身份",
        "事件与交易",
        "实际仓位分布",
        "现金占比",
        "留现原因",
        "资产组风险使用率",
        "组合风险使用率",
        "收益与回撤",
        "Alpha（超额收益）与 Beta（市场暴露）",
        "限制",
        "产物摘要",
        "不是正式回测或最终验收结论",
        "Vibe-Trading（AI 研究助理）组合优化器：已跳过",
    ):
        assert required in report
    assert "63.7%" not in report
    assert "55.7%" not in report
    validate_project_outputs(output_dir, identity)


def test_cold_security_does_not_block_other_securities_or_the_project(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    snapshot, market_root = _research_snapshot(tmp_path, cold_security_rows=10)
    output_dir = tmp_path / "cold-output"
    identity = _run_identity(snapshot.snapshot_id)
    benchmark_input = _benchmark_input(snapshot, market_root, tmp_path / "cold-benchmark")

    result = run_research(
        repo_root / "joinquant/strategies/strategy-003/research/baseline.json",
        snapshot.path,
        output_dir,
        market_data_root=market_root,
        benchmark_input=benchmark_input,
        identity=identity,
    )

    assert result.status == "complete"
    with (output_dir / "trades.csv").open(encoding="utf-8", newline="") as handle:
        trades = list(csv.DictReader(handle))
    assert any(row["security"] != "516080.XSHG" for row in trades)
    assert all(row["security"] != "516080.XSHG" for row in trades)
    with (output_dir / "risk.csv").open(encoding="utf-8", newline="") as handle:
        final_risk = list(csv.DictReader(handle))[-1]
    assert "516080.XSHG" in json.loads(final_risk["cold_start_securities"])


def test_missing_explicit_benchmark_input_is_evidence_insufficient(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    snapshot, market_root = _research_snapshot(tmp_path)

    result = run_research(
        repo_root / "joinquant/strategies/strategy-003/research/baseline.json",
        snapshot.path,
        tmp_path / "missing-benchmark-output",
        market_data_root=market_root,
        identity=_run_identity(snapshot.snapshot_id),
    )

    assert result.status == "evidence_insufficient"
    assert result.reason_codes == ("missing_benchmark_input",)


@pytest.mark.parametrize("mutation", ["missing", "invalid-json", "identity-mismatch"])
def test_three_required_outputs_reject_missing_invalid_or_mismatched_evidence(
    mutation: str,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    snapshot, market_root = _research_snapshot(tmp_path)
    output_dir = tmp_path / "output"
    identity = _run_identity(snapshot.snapshot_id)
    benchmark_input = _benchmark_input(snapshot, market_root, tmp_path / "validation-benchmark")
    run_research(
        repo_root / "joinquant/strategies/strategy-003/research/baseline.json",
        snapshot.path,
        output_dir,
        market_data_root=market_root,
        benchmark_input=benchmark_input,
        identity=identity,
    )
    if mutation == "missing":
        (output_dir / "research-report.md").unlink()
    elif mutation == "invalid-json":
        (output_dir / "conclusion.json").write_text("[]\n", encoding="utf-8")
    else:
        path = output_dir / "candidate-strategies.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["identity"]["run_id"] = "9" * 64
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")

    with pytest.raises(OutputValidationError):
        validate_project_outputs(output_dir, identity)


def test_project_run_config_references_snapshot_and_disables_biased_optimizer(
    repo_root: Path,
) -> None:
    research_root = repo_root / "joinquant/strategies/strategy-003/research"
    run_config = json.loads((research_root / "project-run.json").read_text(encoding="utf-8"))
    baseline = json.loads((research_root / "baseline.json").read_text(encoding="utf-8"))

    assert len(run_config["snapshot_id"]) == 64
    assert run_config["project_entry"].endswith("/turtle_etf/cli.py")
    assert run_config["project_config"].endswith("/baseline.json")
    assert all(not path.lower().endswith(".csv") for path in run_config["declared_inputs"])
    assert {
        item["path"]
        for item in run_config["required_outputs"]
        if item["format"] == "parquet"
    } == {
        *(f"{name}.parquet" for name in STANDARD_TABLES),
        "local-evidence-matrix.parquet",
    }
    assert baseline["research"]["vibe_optimizer"]["enabled"] is False
    assert baseline["research"]["vibe_optimizer"]["reason"]


def test_skill_public_command_runs_complete_turtle_workflow(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    token = uuid.uuid4().hex
    project_id = f"strategy-003-e2e-{token[:12]}"
    temp_project = repo_root / ".local/e2e-tests" / token
    market_root = repo_root / ".local/market-data"
    output_project = repo_root / ".local/quant-research" / project_id
    export_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    snapshot = None
    batch_ids: list[str] = []
    try:
        snapshot, _ = _research_snapshot(
            tmp_path,
            market_root=market_root,
            export_code_sha256=export_digest,
            start_date=date(2099, 1, 2),
        )
        snapshot_document = json.loads(snapshot.path.read_text(encoding="utf-8"))
        batch_ids = list(snapshot_document["batch_ids"])
        run_config = json.loads(
            (
                repo_root
                / "joinquant/strategies/strategy-003/research/project-run.json"
            ).read_text(encoding="utf-8")
        )
        run_config.update(
            project_id=project_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_requirements=snapshot_document["selection"],
        )
        temp_project.mkdir(parents=True)
        benchmark_input = _benchmark_input(snapshot, market_root, temp_project)
        run_config["benchmark_input"] = benchmark_input.relative_to(repo_root).as_posix()
        config_path = temp_project / "run.json"
        config_path.write_text(
            json.dumps(run_config, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        skill = (
            repo_root / ".agents/skills/run-local-quant-research/SKILL.md"
        ).read_text(encoding="utf-8")
        command_line = next(
            line.strip()
            for line in skill.splitlines()
            if "local_quant_research\\cli.py run --config <path>" in line
        )
        relative_config = config_path.relative_to(repo_root).as_posix()
        command = command_line.replace("<path>", relative_config).split()

        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=120,
        )

        assert completed.returncode == 0, completed.stderr + completed.stdout
        result = json.loads(completed.stdout)
        assert result["status"] == "complete"
        run_path = Path(result["run_path"])
        assert run_path.parent == output_project
        manifest = json.loads(
            (run_path / "run-manifest.json").read_text(encoding="utf-8")
        )
        validate_project_outputs(
            run_path,
            RunIdentity(
                run_id=result["run_id"],
                snapshot_id=snapshot.snapshot_id,
                code_sha256=manifest["inputs"]["code_sha256"],
                config_sha256=manifest["inputs"]["config_sha256"],
            ),
        )
        assert not list(output_project.glob(".*.tmp"))
        assert not list(output_project.glob(".*.inputs"))
    finally:
        shutil.rmtree(output_project, ignore_errors=True)
        shutil.rmtree(temp_project, ignore_errors=True)
        if snapshot is not None:
            snapshot.path.unlink(missing_ok=True)
        for batch_id in batch_ids:
            shutil.rmtree(market_root / "batches" / batch_id, ignore_errors=True)
        _remove_empty_test_roots(repo_root, market_root)
