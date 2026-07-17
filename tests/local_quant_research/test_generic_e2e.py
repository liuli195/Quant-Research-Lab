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


def test_minimal_strategy_completes_reuses_and_rejects_digest_conflict(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    token = uuid.uuid4().hex
    project_id = "minimal-fixture-b"
    project_root = repo_root / ".local/e2e-tests" / token
    output_project = repo_root / ".local/quant-research" / project_id
    attempts_root = output_project / ".attempts"
    existing_attempts = (
        {path.name for path in attempts_root.glob("*.json")}
        if attempts_root.is_dir()
        else set()
    )
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
        scenario_config = project_root / "scenario.json"
        scenario_config.write_text(
            '{"scenario_id":"baseline","schema_version":1}\n',
            encoding="utf-8",
        )
        declared = project_root / "input.txt"
        declared.write_text("plain input\n", encoding="utf-8")
        run_config = {
            "schema_version": 2,
            "project_id": project_id,
            "strategy": {
                "root": "tests/local_quant_research/fixtures",
                "module": "minimal_strategy_b.strategy",
                "symbol": "MODULE",
            },
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_requirements": snapshot_document["selection"],
            "scenario_config": scenario_config.relative_to(repo_root).as_posix(),
            "declared_inputs": [declared.relative_to(repo_root).as_posix()],
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
        manifest = json.loads((run_output / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["object"]["strategy_id"] == project_id
        assert manifest["object"]["run_id"] == result["run_id"]
        assert {
            path.stem for path in (run_output / "data").glob("*.parquet")
        } == {"results", "balances", "positions", "orders"}
        assert (run_output / "report/execution-summary.md").is_file()
        assert (run_output / "report/metrics.json").is_file()
        expected_code = {
            "minimal_strategy_b/__init__.py": (
                repo_root
                / "tests/local_quant_research/fixtures/minimal_strategy_b/__init__.py"
            ),
            "minimal_strategy_b/strategy.py": (
                repo_root
                / "tests/local_quant_research/fixtures/minimal_strategy_b/strategy.py"
            ),
        }
        code_identity = json.loads(
            (run_output / "config/code-identity.json").read_text(encoding="utf-8")
        )
        strategy_identity = {
            item["path"]: item["sha256"]
            for item in code_identity["files"]
            if item["path"].startswith(
                "tests/local_quant_research/fixtures/minimal_strategy_b/"
            )
        }
        assert set(manifest["code"]) == set(expected_code)
        assert set(strategy_identity) == {
            source.relative_to(repo_root).as_posix()
            for source in expected_code.values()
        }
        for relative, source in expected_code.items():
            archived = run_output / "code" / relative
            reference = manifest["code"][relative]
            digest = _sha256(source)
            assert archived.read_bytes() == source.read_bytes()
            assert reference["path"] == f"code/{relative}"
            assert reference["sha256"] == digest
            assert strategy_identity[source.relative_to(repo_root).as_posix()] == digest
        performance = json.loads(
            (run_output / "evidence/performance.json").read_text(encoding="utf-8")
        )
        assert performance["cold"]["digest"] == performance["warm"]["digest"]
        assert 0 <= performance["cold"]["seconds"] < 180
        assert 0 <= performance["warm"]["seconds"] < 180
        assert tuple(performance["stages"]) == (
            "core_facts",
            "followup_prepare",
            "followup_vectorbt",
            "parquet_materialize",
            "primary_vectorbt",
            "readback_validate",
            "report_and_manifest",
            "strategy_extensions",
            "strategy_load",
            "strategy_prepare",
        )
        assert not tuple(run_output.rglob("*.duckdb"))
        assert not tuple(market_root.rglob("*.duckdb"))

        reused = subprocess.run(
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
        assert reused.returncode == 0, reused.stderr + reused.stdout
        assert json.loads(reused.stdout)["reused"] is True

        manifest_path = run_output / "manifest.json"
        tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
        tampered["package_sha256"] = "0" * 64
        manifest_path.write_text(
            json.dumps(tampered, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        conflict = subprocess.run(
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
        assert conflict.returncode == 1
        assert json.loads(conflict.stdout)["status"] == "failed"
    finally:
        if "run_output" in locals():
            shutil.rmtree(run_output, ignore_errors=True)
        try:
            output_project.rmdir()
        except OSError:
            pass
        if attempts_root.is_dir():
            for attempt in attempts_root.glob("*.json"):
                if attempt.name not in existing_attempts:
                    attempt.unlink(missing_ok=True)
            try:
                attempts_root.rmdir()
                output_project.rmdir()
            except OSError:
                pass
        shutil.rmtree(project_root, ignore_errors=True)
        if snapshot is not None:
            snapshot.path.unlink(missing_ok=True)
        for batch_id in batch_ids:
            shutil.rmtree(market_root / "batches" / batch_id, ignore_errors=True)
        _remove_empty_test_roots(repo_root, market_root)


def test_public_cli_maps_missing_config_to_evidence_insufficient(
    repo_root: Path,
) -> None:
    missing = repo_root / ".local/e2e-tests/does-not-exist.json"

    completed = subprocess.run(
        [
            str(repo_root / ".venv/Scripts/python.exe"),
            str(repo_root / "scripts/research/local_quant_research/cli.py"),
            "run",
            "--config",
            str(missing.relative_to(repo_root)),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout)["status"] == "evidence_insufficient"
    assert "Traceback" not in completed.stderr + completed.stdout


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

    shared_runtime = repo_root / "scripts/research/local_quant_research"
    for path in shared_runtime.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if path.name == "vectorbt_runtime.py":
            assert "Portfolio.from_order_func" in source
        else:
            assert "import vectorbt" not in source
            assert "from vectorbt" not in source
