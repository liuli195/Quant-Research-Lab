from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from scripts.research import analysis_data
from scripts.research.market_data.contracts import SnapshotSelection
from scripts.research.market_data.query import MARKET_DATA_FIELDS
from scripts.research.market_data.storage import create_snapshot, import_batch


def _write_market_csv(
    path: Path,
    *,
    securities: tuple[str, ...],
    dates: pd.DatetimeIndex,
) -> None:
    breakout_rows = {
        securities[0]: 55,
        securities[1]: 60,
    }
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=MARKET_DATA_FIELDS, lineterminator="\n"
        )
        writer.writeheader()
        for security in securities:
            previous_close = 10.0
            breakout_row = breakout_rows.get(security)
            for index, date in enumerate(dates):
                close = 11.0 if breakout_row is not None and index >= breakout_row else 10.0
                writer.writerow(
                    {
                        "date": date.date().isoformat(),
                        "security": security,
                        "open": f"{close:.2f}",
                        "high": f"{close + 0.20:.2f}",
                        "low": f"{close - 0.20:.2f}",
                        "close": f"{close:.2f}",
                        "pre_close": f"{previous_close:.2f}",
                        "volume": "1000000",
                        "money": f"{close * 1000000:.2f}",
                        "factor": "1",
                        "paused": "0",
                        "high_limit": f"{close * 1.10:.2f}",
                        "low_limit": f"{close * 0.90:.2f}",
                    }
                )
                previous_close = close


def test_turtle_project_completes_full_single_scenario_entrypoint(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    token = uuid.uuid4().hex
    research_root = repo_root / "joinquant/strategies/strategy-003/research"
    baseline = json.loads(
        (research_root / "baseline.json").read_text(encoding="utf-8")
    )
    securities = tuple(
        sorted(str(item["security"]) for item in baseline["universe"])
    )
    dates = pd.bdate_range("2030-01-02", periods=70)
    market_root = repo_root / ".local/market-data"
    project_root = repo_root / ".local/e2e-tests" / token
    snapshot = None
    batch_ids: list[str] = []
    run_output: Path | None = None
    analysis_id = f"e2e-{token[:16]}"
    archive_output = research_root / "archives" / analysis_id
    try:
        source = tmp_path / "turtle-e2e.csv"
        _write_market_csv(source, securities=securities, dates=dates)
        batch = import_batch(
            csv_path=source,
            manifest={
                "schema_version": 1,
                "source": {"name": "joinquant", "environment": "research"},
                "asset_type": "etf",
                "frequency": "1d",
                "fields": list(MARKET_DATA_FIELDS),
                "price_semantics": {"fq": None, "skip_paused": False},
                "export_code_sha256": hashlib.sha256(
                    token.encode("ascii")
                ).hexdigest(),
                "corporate_actions": {
                    "source": {
                        "name": "joinquant",
                        "dataset": "finance.FUND_DIVIDEND",
                    },
                    "knowledge_cutoff_date": dates[-1].date().isoformat(),
                    "status": "verified_empty",
                },
            },
            root=market_root,
        )
        source.unlink()
        selection = SnapshotSelection(
            source={"name": "joinquant", "environment": "research"},
            asset_type="etf",
            frequency="1d",
            securities=securities,
            start_date=dates[0].date().isoformat(),
            end_date=dates[-1].date().isoformat(),
            fields=MARKET_DATA_FIELDS,
            price_semantics={"fq": None, "skip_paused": False},
        )
        snapshot = create_snapshot(
            batch_ids=(batch.batch_id,), selection=selection, root=market_root
        )
        snapshot_document = json.loads(snapshot.path.read_text(encoding="utf-8"))
        batch_ids = list(snapshot_document["batch_ids"])

        project_root.mkdir(parents=True)
        config = json.loads(json.dumps(baseline))
        config["risk"]["portfolio_unit_cap"] = 1.0
        config_path = project_root / "baseline.json"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        run_config = {
            "schema_version": 2,
            "project_id": "strategy-003",
            "strategy": {
                "root": "joinquant/strategies/strategy-003/research",
                "module": "turtle_etf.strategy",
                "symbol": "MODULE",
            },
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_requirements": snapshot_document["selection"],
            "scenario_config": config_path.relative_to(repo_root).as_posix(),
            "declared_inputs": [
                "joinquant/strategies/strategy-003/manifest.json"
            ],
        }
        run_path = project_root / "run.json"
        run_path.write_text(
            json.dumps(run_config, sort_keys=True) + "\n", encoding="utf-8"
        )

        completed = subprocess.run(
            [
                str(repo_root / ".venv/Scripts/python.exe"),
                str(repo_root / "scripts/research/local_quant_research/cli.py"),
                "run",
                "--config",
                str(run_path.relative_to(repo_root)),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=300,
        )

        assert completed.returncode == 0, completed.stderr + completed.stdout
        outcome = json.loads(completed.stdout)
        assert outcome["status"] == "complete"
        run_output = Path(outcome["run_path"])
        assert outcome["next_action"] == "return_to_caller"

        result_root = run_output
        manifest = json.loads(
            (result_root / "manifest.json").read_text(encoding="utf-8")
        )
        assert set(manifest["datasets"]) == {
            "results",
            "balances",
            "positions",
            "orders",
        }
        performance = json.loads(
            (result_root / "evidence/performance.json").read_text(encoding="utf-8")
        )
        assert performance["cold"]["digest"] == performance["warm"]["digest"]
        assert performance["cold"]["seconds"] < 180.0
        assert performance["warm"]["seconds"] < 180.0
        assert (result_root / "config/code-identity.json").is_file()
        assert (result_root / "evidence/runtime-lock.json").is_file()
        assert (result_root / "report/execution-summary.md").is_file()
        assert (result_root / "report/metrics.json").is_file()

        attribution_ref = manifest["extensions"]["turtle_etf"]["files"][0][
            "path"
        ]
        attribution = pq.read_table(result_root / attribution_ref).to_pandas()
        redistributions = attribution.loc[
            (attribution["event_type"] == "decision")
            & (
                attribution["reason_code"]
                == "full_position_redistribution"
            )
        ]
        assert not redistributions.empty
        details = [
            json.loads(value) for value in redistributions["details_json"]
        ]
        assert all(
            item["redistribution_state_changed"] is False for item in details
        )
        assert any(float(item["portfolio_scale"]) < 1.0 for item in details)
        assert not tuple(result_root.rglob("*.tmp"))
        assert not tuple(market_root.rglob("*.duckdb"))

        promote = subprocess.run(
            [
                str(repo_root / ".venv/Scripts/python.exe"),
                str(repo_root / "scripts/research/local_quant_research/cli.py"),
                "promote",
                "--strategy-id",
                "strategy-003",
                "--run-id",
                outcome["run_id"],
                "--analysis-id",
                analysis_id,
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=120,
        )
        assert promote.returncode == 0, promote.stderr + promote.stdout
        promoted = json.loads(promote.stdout)
        assert promoted["status"] == "complete"
        assert promoted["reused"] is False
        assert archive_output.is_dir()
        source_files = {
            path.relative_to(run_output).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in run_output.rglob("*")
            if path.is_file()
        }
        archive_files = {
            path.relative_to(archive_output).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in archive_output.rglob("*")
            if path.is_file()
        }
        assert archive_files == source_files

        reused = subprocess.run(
            [
                str(repo_root / ".venv/Scripts/python.exe"),
                str(repo_root / "scripts/research/local_quant_research/cli.py"),
                "promote",
                "--strategy-id",
                "strategy-003",
                "--run-id",
                outcome["run_id"],
                "--analysis-id",
                analysis_id,
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=120,
        )
        assert reused.returncode == 0
        assert json.loads(reused.stdout)["reused"] is True

        shutil.rmtree(run_output)
        run_output = None
        archived_source = analysis_data.open_analysis_source(archive_output)
        assert archived_source.kind == "local_research"
        assert archived_source.manifest["object"]["run_id"] == outcome["run_id"]
    finally:
        if run_output is not None:
            shutil.rmtree(run_output, ignore_errors=True)
        shutil.rmtree(archive_output, ignore_errors=True)
        try:
            archive_output.parent.rmdir()
        except OSError:
            pass
        shutil.rmtree(project_root, ignore_errors=True)
        if snapshot is not None:
            snapshot.path.unlink(missing_ok=True)
        for batch_id in batch_ids:
            shutil.rmtree(market_root / "batches" / batch_id, ignore_errors=True)
        for path in (
            repo_root / ".local/e2e-tests",
            market_root / "snapshots",
            market_root / "batches",
        ):
            try:
                path.rmdir()
            except OSError:
                pass
