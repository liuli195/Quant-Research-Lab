from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path

from scripts.research.market_data.contracts import SnapshotSelection
from scripts.research.market_data.query import MARKET_DATA_FIELDS
from scripts.research.market_data.storage import create_snapshot, import_batch


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _remove_empty_test_roots(repo_root: Path, market_root: Path) -> None:
    for path in (market_root / "snapshots", market_root / "batches"):
        try:
            path.rmdir()
        except OSError:
            pass
    if (
        not (market_root / "snapshots").exists()
        and not (market_root / "batches").exists()
    ):
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


def test_non_strategy_project_completes_through_shared_market_and_runner(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    token = uuid.uuid4().hex
    project_id = f"plain-project-{token[:12]}"
    project_root = repo_root / ".local/e2e-tests" / token
    output_project = repo_root / ".local/quant-research" / project_id
    market_root = repo_root / ".local/market-data"
    snapshot = None
    batch_ids: list[str] = []
    try:
        source = tmp_path / "daily.csv"
        with source.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=MARKET_DATA_FIELDS,
                lineterminator="\n",
            )
            writer.writeheader()
            for row_date in ("2026-01-05", "2026-01-06"):
                writer.writerow(
                    {
                        "date": row_date,
                        "security": "000001.XSHG",
                        "open": "10",
                        "high": "11",
                        "low": "9",
                        "close": "10",
                        "pre_close": "10",
                        "volume": "1000",
                        "money": "10000",
                        "factor": "1",
                        "paused": "0",
                        "high_limit": "12",
                        "low_limit": "8",
                    }
                )
        export_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        batch = import_batch(
            csv_path=source,
            manifest={
                "schema_version": 1,
                "source": {"name": "joinquant", "environment": "research"},
                "asset_type": "etf",
                "frequency": "1d",
                "fields": list(MARKET_DATA_FIELDS),
                "price_semantics": {"fq": None, "skip_paused": False},
                "export_code_sha256": export_digest,
                "corporate_actions": {
                    "source": {
                        "name": "joinquant",
                        "dataset": "finance.FUND_DIVIDEND",
                    },
                    "knowledge_cutoff_date": "2026-01-06",
                    "status": "verified_empty",
                },
            },
            root=market_root,
        )
        source.unlink()
        assert not source.exists()
        assert (batch.path / "market-data.parquet").is_file()
        assert not tuple(batch.path.rglob("*.duckdb"))
        selection = SnapshotSelection(
            source={"name": "joinquant", "environment": "research"},
            asset_type="etf",
            frequency="1d",
            securities=("000001.XSHG",),
            start_date="2026-01-05",
            end_date="2026-01-06",
            fields=MARKET_DATA_FIELDS,
            price_semantics={"fq": None, "skip_paused": False},
        )
        snapshot = create_snapshot(
            batch_ids=(batch.batch_id,),
            selection=selection,
            root=market_root,
        )
        snapshot_document = json.loads(snapshot.path.read_text(encoding="utf-8"))
        batch_ids = list(snapshot_document["batch_ids"])

        project_root.mkdir(parents=True)
        adapter = (
            repo_root / "tests/local_quant_research/fixtures/plain_project_adapter.py"
        )
        project_config = project_root / "project.json"
        project_config.write_text('{"schema_version":1}\n', encoding="utf-8")
        declared = project_root / "input.txt"
        declared.write_text("plain input\n", encoding="utf-8")
        code_identity = project_root / "code-identity.json"
        shared_sources = [
            repo_root / "scripts/__init__.py",
            repo_root / "scripts/research/__init__.py",
            *sorted((repo_root / "scripts/research/market_data").glob("*.py")),
        ]
        identity_sources = sorted(
            {adapter, *shared_sources},
            key=lambda path: path.relative_to(repo_root).as_posix(),
        )
        code_identity.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "files": [
                        {
                            "path": source_path.relative_to(repo_root).as_posix(),
                            "sha256": _sha256(source_path),
                        }
                        for source_path in identity_sources
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        required_outputs = [
            {"path": "result.json", "format": "json"},
        ]
        run_config = {
            "schema_version": 1,
            "project_id": project_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_requirements": snapshot_document["selection"],
            "project_entry": adapter.relative_to(repo_root).as_posix(),
            "command": [
                ".venv/Scripts/python.exe",
                adapter.relative_to(repo_root).as_posix(),
            ],
            "project_config": project_config.relative_to(repo_root).as_posix(),
            "code_identity": code_identity.relative_to(repo_root).as_posix(),
            "declared_inputs": [declared.relative_to(repo_root).as_posix()],
            "required_outputs": required_outputs,
            "output_root": ".local/quant-research",
            "stop_states": ["complete", "evidence_insufficient", "failed"],
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
            timeout=120,
        )

        assert completed.returncode == 0, completed.stderr + completed.stdout
        result = json.loads(completed.stdout)
        assert result["status"] == "complete"
        run_output = Path(result["run_path"])
        document = json.loads((run_output / "result.json").read_text(encoding="utf-8"))
        assert document["snapshot_id"] == snapshot.snapshot_id
        assert document["run_id"] == result["run_id"]
        status = json.loads(
            (run_output / "project-status.json").read_text(encoding="utf-8")
        )
        assert status["next_action"] == "return_to_caller"
        assert not (run_output / "recommendation.json").exists()
        assert not (run_output / "local-research-report.md").exists()
        assert not tuple(run_output.glob("*.parquet"))
        assert not tuple(market_root.rglob("*.duckdb"))
    finally:
        shutil.rmtree(output_project, ignore_errors=True)
        shutil.rmtree(project_root, ignore_errors=True)
        if snapshot is not None:
            snapshot.path.unlink(missing_ok=True)
        for batch_id in batch_ids:
            shutil.rmtree(market_root / "batches" / batch_id, ignore_errors=True)
        _remove_empty_test_roots(repo_root, market_root)


def test_shared_sources_do_not_depend_on_one_strategy(repo_root: Path) -> None:
    paths = [
        *sorted((repo_root / "scripts/research/market_data").glob("*.py")),
        *sorted((repo_root / "scripts/research/local_quant_research").glob("*.py")),
        repo_root / ".agents/skills/run-local-quant-research/SKILL.md",
    ]
    text = "\n".join(path.read_text(encoding="utf-8").lower() for path in paths)
    for forbidden in (
        "turtle",
        "55日",
        "strategy-003",
        "510300.xshg",
        "512100.xshg",
    ):
        assert forbidden not in text
