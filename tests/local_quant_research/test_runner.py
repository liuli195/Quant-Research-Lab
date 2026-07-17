from __future__ import annotations

import hashlib
import importlib
import gc
import json
import os
import shutil
import subprocess
import uuid
import weakref
from pathlib import Path
from typing import Callable

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from scripts.research.local_quant_research.contracts import OutputSpec
from scripts.research.local_quant_research.evidence import (
    EvidenceError,
    collect_output_evidence,
)
from scripts.research.local_quant_research.runner import _project_status, run_project
from scripts.research.market_data.contracts import SnapshotSelection
from scripts.research.market_data.storage import create_snapshot, import_batch


FIELDS = (
    "date",
    "security",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "money",
    "factor",
    "paused",
    "high_limit",
    "low_limit",
)
COMPLETE_STATUS = {
    "schema_version": 1,
    "status": "complete",
    "reason_codes": [],
    "next_action": "return_to_caller",
}


def test_complete_project_status_rejects_human_confirmation_next_action(
    tmp_path: Path,
) -> None:
    _write_json(
        tmp_path / "project-status.json",
        {
            "schema_version": 1,
            "status": "complete",
            "reason_codes": [],
            "next_action": "human_confirmation_required",
        },
    )

    with pytest.raises(EvidenceError, match="next_action"):
        _project_status(tmp_path)


def test_complete_project_status_can_return_single_scenario_to_caller(
    tmp_path: Path,
) -> None:
    _write_json(
        tmp_path / "project-status.json",
        {
            "schema_version": 1,
            "status": "complete",
            "reason_codes": [],
            "next_action": "return_to_caller",
        },
    )

    assert _project_status(tmp_path) == ("complete", ())


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digests(path: Path) -> dict[str, str]:
    return {
        item.relative_to(path).as_posix(): _sha256(item)
        for item in path.rglob("*")
        if item.is_file()
    }


def _build_repo(tmp_path: Path, source_repo: Path) -> tuple[Path, Path, dict[str, object]]:
    root = tmp_path / "repo"
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"test launcher")

    project_dir = root / "projects" / "generic-research"
    entry = project_dir / "strategy.py"
    entry.parent.mkdir(parents=True)
    source_strategy = (
        source_repo
        / "tests/local_quant_research/fixtures/minimal_strategy/strategy.py"
    ).read_text(encoding="utf-8")
    entry.write_text(
        source_strategy.replace('strategy_id="minimal-fixture"', 'strategy_id="generic-research"'),
        encoding="utf-8",
    )
    project_config = project_dir / "scenario.json"
    _write_json(
        project_config,
        {"schema_version": 1, "scenario_id": "baseline", "parameter": 7},
    )
    declared_input = project_dir / "input.txt"
    declared_input.write_text("declared input\n", encoding="utf-8")
    for relative in (
        Path("scripts/research/local_quant_research"),
        Path("scripts/research/market_data"),
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_repo / relative, target)
    for relative in (Path("scripts/__init__.py"), Path("scripts/research/__init__.py")):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_repo / relative, target)

    fixture = (
        source_repo
        / "tests"
        / "local_quant_research"
        / "fixtures"
        / "daily-bars.csv"
    )
    manifest = {
        "schema_version": 1,
        "source": {"name": "joinquant", "environment": "research"},
        "asset_type": "etf",
        "frequency": "1d",
        "fields": list(FIELDS),
        "price_semantics": {"fq": None, "skip_paused": False},
        "export_code_sha256": "a" * 64,
        "corporate_actions": {
            "source": {
                "name": "joinquant",
                "dataset": "finance.FUND_DIVIDEND",
            },
            "knowledge_cutoff_date": "2026-01-06",
            "status": "verified_empty",
        },
    }
    market_root = root / ".local" / "market-data"
    batch = import_batch(csv_path=fixture, manifest=manifest, root=market_root)
    selection = SnapshotSelection(
        source={"name": "joinquant", "environment": "research"},
        asset_type="etf",
        frequency="1d",
        securities=("000001.XSHG", "000002.XSHE"),
        start_date="2026-01-05",
        end_date="2026-01-06",
        fields=FIELDS,
        price_semantics={"fq": None, "skip_paused": False},
    )
    snapshot = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=selection,
        root=market_root,
    )
    config = {
        "schema_version": 2,
        "project_id": "generic-research",
        "strategy": {
            "root": "projects/generic-research",
            "module": "strategy",
            "symbol": "MODULE",
        },
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_requirements": selection.to_document(),
        "scenario_config": "projects/generic-research/scenario.json",
        "declared_inputs": ["projects/generic-research/input.txt"],
    }
    config_path = project_dir / "run.json"
    _write_json(config_path, config)
    return root, config_path, config


def _output_dir(command: list[str]) -> Path:
    return Path(command[command.index("--staging") + 1])


def _successful_process(
    assertions: Callable[[list[str], dict[str, object]], None] | None = None,
):
    def fake_run(command: list[str], **kwargs):
        if assertions:
            assertions(command, kwargs)
        from scripts.research.local_quant_research.runner import execute_frozen_inputs

        document = execute_frozen_inputs(
            Path(command[command.index("--frozen-inputs") + 1]),
            _output_dir(command),
        )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(document, sort_keys=True),
            stderr="",
        )

    return fake_run


def test_required_output_accepts_valid_parquet_and_rejects_invalid_bytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "analysis.parquet"
    pq.write_table(pa.table({"date": ["2026-01-05"], "value": [1.0]}), path)
    spec = OutputSpec(path=path.name, format="parquet")

    evidence = collect_output_evidence(tmp_path, (spec,))

    assert evidence[0]["format"] == "parquet"
    path.write_bytes(b"not parquet")
    with pytest.raises(Exception, match="Parquet"):
        collect_output_evidence(tmp_path, (spec,))


def test_unknown_benchmark_input_is_rejected_before_shared_execution(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, config = _build_repo(tmp_path, repo_root)
    config["benchmark_input"] = ".local/market-data/benchmarks/input.parquet"
    _write_json(config_path, config)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("invalid v2 config must not execute"),
    )

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "evidence_insufficient"


def test_project_execution_timeout_covers_complete_research_workflow(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)

    def assert_invocation(_: list[str], kwargs: dict[str, object]) -> None:
        assert kwargs["timeout"] >= 3_600

    monkeypatch.setattr(subprocess, "run", _successful_process(assert_invocation))

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "complete"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda config: config.update(command="python adapter.py"),
        lambda config: config["strategy"].update(root="../outside"),
        lambda config: config.pop("snapshot_id"),
        lambda config: config.update(scenario_config="missing.json"),
        lambda config: config.update(stop_states=["complete"]),
        lambda config: config.update(
            command=["python.exe", "projects/generic-research/strategy.py"]
        ),
        lambda config: config.update(
            command=[".venv/Scripts/python.exe", "-m", "pip", "install", "duckdb"]
        ),
        lambda config: config.update(api_token="must-not-appear"),
    ],
    ids=[
        "shell-string",
        "outside-entry",
        "missing-snapshot",
        "missing-scenario",
        "unknown-state",
        "system-python",
        "implicit-install",
        "credential-field",
    ],
)
def test_unsafe_or_incomplete_config_stops_before_project_process(
    mutation,
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, config = _build_repo(tmp_path, repo_root)
    mutation(config)
    _write_json(config_path, config)
    called = False

    def forbidden_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("project process must not run")

    monkeypatch.setattr(subprocess, "run", forbidden_run)

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "evidence_insufficient"
    assert called is False
    assert "must-not-appear" not in json.dumps(result.to_document())


def test_missing_declared_input_is_evidence_insufficient_before_process(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    (fake_root / "projects" / "generic-research" / "input.txt").unlink()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("project process must not run"),
    )

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "evidence_insufficient"
    assert result.run_path is None


def test_missing_snapshot_and_tampered_snapshot_have_distinct_states(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_root, missing_config, _ = _build_repo(tmp_path / "missing", repo_root)
    next((missing_root / ".local/market-data/snapshots").glob("*.json")).unlink()
    tampered_root, tampered_config, _ = _build_repo(tmp_path / "tampered", repo_root)
    market_parquet = next(
        (tampered_root / ".local/market-data/batches").rglob("market-data.parquet")
    )
    market_parquet.write_bytes(market_parquet.read_bytes() + b"tampered")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("invalid snapshot must stop before project"),
    )

    missing = run_project(missing_config, repo_root=missing_root)
    tampered = run_project(tampered_config, repo_root=tampered_root)

    assert missing.status == "evidence_insufficient"
    assert tampered.status == "failed"


def test_project_can_report_evidence_insufficient_without_false_completion(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)

    def process(command: list[str], **kwargs):
        return subprocess.CompletedProcess(
            command,
            2,
            stdout=json.dumps(
                {
                    "status": "evidence_insufficient",
                    "reasons": ["insufficient_domain_samples"],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", process)

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "evidence_insufficient"
    assert result.run_path is None


def test_complete_run_uses_fixed_stages_sanitized_environment_and_atomic_path(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    monkeypatch.setenv("RESEARCH_TEST_PASSWORD", "credential-value")

    def assert_invocation(command: list[str], kwargs: dict[str, object]) -> None:
        assert Path(command[0]) == (fake_root / ".venv/Scripts/python.exe").resolve()
        assert Path(command[1]) == (
            fake_root / "scripts/research/local_quant_research/cli.py"
        ).resolve()
        assert command[2] == "_execute"
        frozen = Path(command[command.index("--frozen-inputs") + 1])
        assert frozen.name == "request.json"
        assert frozen.parent.name.endswith(".inputs")
        assert kwargs["shell"] is False
        assert Path(kwargs["cwd"]) == fake_root
        assert "RESEARCH_TEST_PASSWORD" not in kwargs["env"]
        assert command.count("--frozen-inputs") == 1
        assert command.count("--staging") == 1
        assert Path(kwargs["env"]["NUMBA_CACHE_DIR"]).is_relative_to(frozen.parent)

    monkeypatch.setattr(subprocess, "run", _successful_process(assert_invocation))

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "complete"
    assert result.reused is False
    assert result.run_path == (
        fake_root / ".local" / "quant-research" / "generic-research" / result.run_id
    )
    assert {path.name for path in result.run_path.iterdir()} == {
        "code",
        "config",
        "data",
        "evidence",
        "extensions",
        "manifest.json",
        "report",
    }
    manifest = json.loads(
        (result.run_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert [stage.name for stage in result.stages] == [
        "snapshot_validation",
        "config_validation",
        "project_execution",
        "output_validation",
        "evidence_finalization",
    ]
    assert all(stage.status == "complete" for stage in result.stages)
    assert manifest["object"]["status"] == "complete"
    assert not list((fake_root / ".local/quant-research").rglob("*.tmp"))


def test_same_complete_identity_is_revalidated_and_reused_without_execution(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    monkeypatch.setattr(subprocess, "run", _successful_process())
    first = run_project(config_path, repo_root=fake_root)
    before = _tree_digests(first.run_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("complete run must be reused"),
    )

    second = run_project(config_path, repo_root=fake_root)

    assert second.status == "complete"
    assert second.reused is True
    assert second.run_id == first.run_id
    assert _tree_digests(second.run_path) == before


def test_invalid_extension_type_fails_before_cold_warm_comparison(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    strategy = fake_root / "projects/generic-research/strategy.py"
    strategy.write_text(
        strategy.read_text(encoding="utf-8")
        .replace("import numpy as np", "import numpy as np\nimport pyarrow as pa")
        .replace("extension_names=(),", 'extension_names=("invalid",),')
        .replace(
            "return ()",
            "return (ResultExtension(\n"
            "    name=\"invalid\",\n"
            "    schema_version=\"invalid/1\",\n"
            "    table=pa.table({\"value\": pa.array([[\"nested\"]])}),\n"
            "    unique_key=(\"value\",),\n"
            "    evidence={},\n"
            "),)",
        ),
        encoding="utf-8",
    )
    def result_contract_process(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        from scripts.research.local_quant_research.result_package import ResultContractError
        from scripts.research.local_quant_research.runner import execute_frozen_inputs

        try:
            document = execute_frozen_inputs(
                Path(command[command.index("--frozen-inputs") + 1]),
                _output_dir(command),
            )
        except ResultContractError:
            document = {"status": "failed", "reasons": ["result_contract_failed"]}
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=json.dumps(document, sort_keys=True),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", result_contract_process)

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "failed"
    assert result.reasons == ("result_contract_failed",)


def test_complete_run_with_non_complete_package_status_is_not_reused(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    monkeypatch.setattr(subprocess, "run", _successful_process())
    first = run_project(config_path, repo_root=fake_root)
    manifest_path = first.run_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["object"]["status"] = "human_confirmation_required"
    _write_json(manifest_path, manifest)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("invalid complete run must not execute"),
    )

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "failed"
    assert result.reused is False


def test_tampered_complete_output_fails_without_overwriting_old_run(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    monkeypatch.setattr(subprocess, "run", _successful_process())
    first = run_project(config_path, repo_root=fake_root)
    output = first.run_path / "data/results.parquet"
    output.write_bytes(output.read_bytes() + b"tampered")
    tampered = output.read_bytes()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("tampered run must not execute"),
    )

    second = run_project(config_path, repo_root=fake_root)

    assert second.status == "failed"
    assert output.read_bytes() == tampered
    attempts = list(
        (fake_root / ".local/quant-research/generic-research/.attempts").glob("*.json")
    )
    assert len(attempts) == 1


def test_rewritten_manifest_cannot_replace_the_declared_output_contract(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    monkeypatch.setattr(subprocess, "run", _successful_process())
    first = run_project(config_path, repo_root=fake_root)
    manifest_path = first.run_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["datasets"] = {"results": manifest["datasets"]["results"]}
    _write_json(manifest_path, manifest)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("rewritten evidence must not be reused"),
    )

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "failed"


def test_failed_post_publish_validation_removes_false_complete_directory(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import runner

    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    monkeypatch.setattr(subprocess, "run", _successful_process())
    real_validate = runner._package_identity
    validations = 0

    def fail_after_publish(*args, **kwargs):
        nonlocal validations
        validations += 1
        if validations == 2:
            raise runner.EvidenceError("forced post-publish validation failure")
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(
        runner,
        "_package_identity",
        fail_after_publish,
    )

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "failed"
    project_root = fake_root / ".local/quant-research/generic-research"
    assert not any(path.is_dir() and len(path.name) == 64 for path in project_root.iterdir())


def test_transient_directory_publish_lock_is_retried_without_weakening_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import runner

    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text("{}\n", encoding="utf-8")
    target = tmp_path / "target"
    real_replace = runner.os.replace
    calls = 0

    def transient_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError(13, "directory is temporarily in use", str(source))
        real_replace(source, target)

    monkeypatch.setattr(runner.os, "replace", transient_replace)

    runner._publish_directory(source, target)

    assert calls == 2
    assert (target / "manifest.json").is_file()


@pytest.mark.parametrize("project_status", ["failed", "mystery"])
def test_project_failure_or_unknown_status_records_attempt_not_complete_run(
    project_status: str,
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)

    def process(command: list[str], **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=json.dumps(
                {"status": project_status, "reasons": ["project_declined"]}
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", process)

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "failed"
    assert result.run_path is None
    project_root = fake_root / ".local/quant-research/generic-research"
    assert not any(path.is_dir() and len(path.name) == 64 for path in project_root.iterdir())
    attempt = next((project_root / ".attempts").glob("*.json"))
    document = json.loads(attempt.read_text(encoding="utf-8"))
    assert set(document) == {
        "schema_version",
        "attempt_id",
        "project_id",
        "run_id",
        "status",
        "stage",
        "reason_codes",
    }


def test_failed_retry_keeps_each_compact_attempt_and_discards_process_output(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    secret = "credential-must-not-be-recorded"

    def process(command: list[str], **kwargs):
        return subprocess.CompletedProcess(command, 9, stdout=secret, stderr=secret)

    monkeypatch.setattr(subprocess, "run", process)

    first = run_project(config_path, repo_root=fake_root)
    second = run_project(config_path, repo_root=fake_root)

    assert first.status == second.status == "failed"
    attempts = list(
        (fake_root / ".local/quant-research/generic-research/.attempts").glob("*.json")
    )
    assert len(attempts) == 2
    assert secret not in json.dumps(first.to_document())
    assert secret not in json.dumps(second.to_document())
    assert all(secret not in path.read_text(encoding="utf-8") for path in attempts)


def test_missing_required_output_is_failed(tmp_path: Path, repo_root: Path, monkeypatch) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)

    def process(command: list[str], **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "complete", "reasons": []}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", process)

    assert run_project(config_path, repo_root=fake_root).status == "failed"


def test_project_write_outside_staging_is_detected(
    tmp_path: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)

    def process(command: list[str], **kwargs):
        (fake_root / "escaped.txt").write_text("not allowed", encoding="utf-8")
        return _successful_process()(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", process)

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "failed"
    assert "outside staging" in " ".join(result.reasons)


def test_project_reads_frozen_input_when_original_changes_temporarily(
    tmp_path: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    declared_input = fake_root / "projects/generic-research/input.txt"
    original_stat = declared_input.stat()

    def process(command: list[str], **kwargs):
        from scripts.research.local_quant_research.runner import execute_frozen_inputs

        declared_input.write_text("tampered input\n", encoding="utf-8")
        request_path = Path(command[command.index("--frozen-inputs") + 1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        frozen_input = Path(request["repository"]) / "projects/generic-research/input.txt"
        value_used = frozen_input.read_text(encoding="utf-8")
        declared_input.write_text("declared input\n", encoding="utf-8")
        os.utime(
            declared_input,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        assert value_used == "declared input\n"
        document = execute_frozen_inputs(request_path, _output_dir(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(document, sort_keys=True),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", process)

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "complete"
    project_root = fake_root / ".local/quant-research/generic-research"
    assert not list(project_root.glob(".*.inputs"))


def test_same_size_input_digest_change_is_rejected_after_shared_execution(
    tmp_path: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    declared_input = fake_root / "projects/generic-research/input.txt"
    original_stat = declared_input.stat()

    def process(command: list[str], **kwargs):
        from scripts.research.local_quant_research.runner import execute_frozen_inputs

        declared_input.write_text("tampered input\n", encoding="utf-8")
        os.utime(
            declared_input,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        request_path = Path(command[command.index("--frozen-inputs") + 1])
        document = execute_frozen_inputs(request_path, _output_dir(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(document, sort_keys=True),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", process)

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "failed"
    assert "changed" in " ".join(result.reasons)


def test_adapter_guard_allows_staging_writes_and_blocks_external_writes(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    execution_root = tmp_path / "execution"
    adapter = execution_root / "repository" / "adapter.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "with open(os.devnull, 'r+b'):\n"
        "    pass\n"
        "output = Path(sys.argv[1])\n"
        "cache = Path(os.environ['NUMBA_CACHE_DIR'])\n"
        "cache.mkdir(parents=True, exist_ok=True)\n"
        "(cache / 'compiled.bin').write_bytes(b'cache')\n"
        "(output / 'inside.txt').write_text('inside', encoding='utf-8')\n"
        "if len(sys.argv) > 3 and sys.argv[2] == 'write':\n"
        "    Path(sys.argv[3]).write_text('escaped', encoding='utf-8')\n"
        "if len(sys.argv) > 3 and sys.argv[2] == 'read':\n"
        "    Path(sys.argv[3]).read_text(encoding='utf-8')\n"
        "if len(sys.argv) > 2 and sys.argv[2] == 'process':\n"
        "    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    escaped = tmp_path / "escaped.txt"
    python = repo_root / ".venv" / "Scripts" / "python.exe"
    base_command = [
        str(python),
        "-m",
        "scripts.research.local_quant_research.adapter_guard",
        "--staging-root",
        str(output_dir),
        "--execution-root",
        str(execution_root),
        "--repository-root",
        str(repo_root),
        "--venv-root",
        str(repo_root / ".venv"),
        "--entry",
        str(adapter),
        "--",
        str(output_dir),
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repo_root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    allowed = subprocess.run(
        base_command,
        cwd=output_dir,
        env=environment,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    blocked = subprocess.run(
        [*base_command, "write", str(escaped)],
        cwd=output_dir,
        env=environment,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )

    assert allowed.returncode == 0, allowed.stderr
    assert (output_dir / "inside.txt").read_text(encoding="utf-8") == "inside"
    assert not (output_dir / ".runtime-cache").exists()
    assert blocked.returncode != 0
    assert not escaped.exists()

    blocked_read = subprocess.run(
        [*base_command, "read", str(repo_root / "AGENTS.md")],
        cwd=output_dir,
        env=environment,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    assert blocked_read.returncode != 0

    blocked_process = subprocess.run(
        [*base_command, "process"],
        cwd=output_dir,
        env=environment,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    assert blocked_process.returncode != 0


LEGACY_RUN_FIELDS = (
    "command",
    "project_entry",
    "code_identity",
    "required_outputs",
    "output_root",
    "stop_states",
)


def _build_v2_config(repo: Path) -> tuple[Path, dict[str, object]]:
    strategy_root = repo / "tests/local_quant_research/fixtures/minimal_strategy"
    strategy_root.mkdir(parents=True, exist_ok=True)
    (strategy_root / "strategy.py").write_text("MODULE = object()\n", encoding="utf-8")
    scenario = repo / "tests/local_quant_research/minimal-scenario.json"
    _write_json(scenario, {"schema_version": 1, "scenario_id": "baseline"})
    declared = repo / "tests/local_quant_research/declared.txt"
    declared.write_text("declared\n", encoding="utf-8")
    document: dict[str, object] = {
        "schema_version": 2,
        "project_id": "minimal-fixture",
        "strategy": {
            "root": "tests/local_quant_research/fixtures/minimal_strategy",
            "module": "strategy",
            "symbol": "MODULE",
        },
        "snapshot_id": "a" * 64,
        "snapshot_requirements": {},
        "scenario_config": "tests/local_quant_research/minimal-scenario.json",
        "declared_inputs": ["tests/local_quant_research/declared.txt"],
    }
    config = repo / "tests/local_quant_research/run-v2.json"
    _write_json(config, document)
    return config, document


def test_v2_config_has_only_strategy_snapshot_scenario_and_declared_inputs(
    tmp_path: Path,
) -> None:
    from scripts.research.local_quant_research import contracts, runner

    config_path, document = _build_v2_config(tmp_path)

    config = runner.load_run_config(config_path, repo_root=tmp_path)

    assert config == contracts.RunConfig(
        project_id="minimal-fixture",
        strategy_root=(
            tmp_path / "tests/local_quant_research/fixtures/minimal_strategy"
        ).resolve(),
        strategy_module="strategy",
        strategy_symbol="MODULE",
        snapshot_id="a" * 64,
        snapshot_requirements={},
        scenario_config=(
            tmp_path / "tests/local_quant_research/minimal-scenario.json"
        ).resolve(),
        declared_inputs=(
            (tmp_path / "tests/local_quant_research/declared.txt").resolve(),
        ),
        document=document,
    )
    assert contracts.RUN_OUTPUT_ROOT == Path(".local/quant-research")
    assert contracts.RUN_STATUSES == (
        "complete",
        "evidence_insufficient",
        "failed",
    )


@pytest.mark.parametrize("legacy_field", LEGACY_RUN_FIELDS)
def test_v2_config_rejects_each_legacy_run_field(
    legacy_field: str,
    tmp_path: Path,
) -> None:
    from scripts.research.local_quant_research import runner

    config_path, document = _build_v2_config(tmp_path)
    document[legacy_field] = []
    _write_json(config_path, document)

    with pytest.raises(runner.ConfigurationError) as caught:
        runner.load_run_config(config_path, repo_root=tmp_path)

    assert caught.value.code == "legacy_run_field"


@pytest.mark.parametrize(
    "scenario_id",
    (None, "", "   ", 7),
    ids=("missing", "empty", "whitespace", "non-string"),
)
def test_v2_config_rejects_missing_scenario_id_before_strategy_execution(
    scenario_id: object,
    tmp_path: Path,
) -> None:
    from scripts.research.local_quant_research import runner

    config_path, document = _build_v2_config(tmp_path)
    scenario_path = tmp_path / str(document["scenario_config"])
    scenario_document = {"schema_version": 1}
    if scenario_id is not None:
        scenario_document["scenario_id"] = scenario_id
    _write_json(scenario_path, scenario_document)

    with pytest.raises(runner.ConfigurationError) as caught:
        runner.load_run_config(config_path, repo_root=tmp_path)

    assert caught.value.code == "missing_scenario_id"


def test_parent_generates_one_fixed_private_execute_command(tmp_path: Path) -> None:
    from scripts.research.local_quant_research import runner

    execution_root = tmp_path / "inputs"
    staging = tmp_path / "staging"

    command = runner._execute_command(
        repo_root=tmp_path,
        execution_root=execution_root,
        staging=staging,
    )

    assert command == (
        tmp_path / ".venv/Scripts/python.exe",
        tmp_path / "scripts/research/local_quant_research/cli.py",
        "_execute",
        "--frozen-inputs",
        execution_root / "request.json",
        "--staging",
        staging,
    )


def test_parent_request_binds_project_run_attempt_and_derived_directories(
    tmp_path: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import runner

    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)

    def inspect_request(command: list[str], **_kwargs: object):
        request_path = Path(command[command.index("--frozen-inputs") + 1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        project_id = request["project_id"]
        run_id = request["run_id"]
        attempt_id = request["attempt_id"]
        project_root = fake_root / ".local/quant-research" / project_id
        assert request_path.parent == project_root / f".{run_id}.{attempt_id}.inputs"
        assert Path(request["staging"]) == project_root / f".{run_id}.{attempt_id}.tmp"
        assert Path(request["output_root"]) == fake_root / ".local/quant-research"
        return subprocess.CompletedProcess(
            command,
            1,
            stdout='{"reasons":["stop_after_inspection"],"status":"failed"}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", inspect_request)

    result = runner.run_project(config_path, repo_root=fake_root)

    assert result.status == "failed"


def test_fixed_output_root_cannot_escape_repository_through_directory_link(
    tmp_path: Path,
) -> None:
    from scripts.research.local_quant_research import runner

    repository = tmp_path / "repository"
    outside = tmp_path / "outside"
    (repository / ".local").mkdir(parents=True)
    outside.mkdir()
    try:
        (repository / ".local/quant-research").symlink_to(
            outside,
            target_is_directory=True,
        )
    except OSError as exc:
        pytest.skip(f"directory links are unavailable: {exc}")

    with pytest.raises(runner.ConfigurationError) as caught:
        runner._resolve_output_project_root(repository, "minimal-fixture")

    assert caught.value.code == "unsafe_output_root"


def _make_directory_link(link: Path, target: Path, kind: str) -> None:
    if kind == "symlink":
        link.symlink_to(target, target_is_directory=True)
        return
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"junctions are unavailable: {completed.stderr or completed.stdout}")


@pytest.mark.parametrize("field", ("repository", "market_data", "runtime_cache"))
@pytest.mark.parametrize("kind", ("symlink", "junction"))
def test_private_bootstrap_rejects_linked_frozen_roots_before_import(
    repo_root: Path,
    tmp_path: Path,
    field: str,
    kind: str,
) -> None:
    from scripts.research.local_quant_research import cli

    project_id = "bootstrap-link-test"
    run_id = "a" * 64
    attempt_id = uuid.uuid4().hex
    project_root = repo_root / ".local/quant-research" / project_id
    execution_root = project_root / f".{run_id}.{attempt_id}.inputs"
    staging = project_root / f".{run_id}.{attempt_id}.tmp"
    targets = {
        "repository": execution_root / "repository",
        "market_data": execution_root / "market-data",
        "runtime_cache": execution_root / "runtime-cache",
    }
    outside = tmp_path / f"outside-{field}-{kind}"
    outside.mkdir()
    execution_root.mkdir(parents=True)
    for name, path in targets.items():
        if name == field:
            try:
                _make_directory_link(path, outside, kind)
            except OSError as exc:
                pytest.skip(f"directory links are unavailable: {exc}")
        else:
            path.mkdir()
    _write_json(
        execution_root / "request.json",
        {
            "schema_version": 2,
            "project_id": project_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "output_root": str((repo_root / ".local/quant-research").resolve()),
            "repository": str(targets["repository"]),
            "market_data": str(targets["market_data"]),
            "live_repository": str(repo_root.resolve()),
            "runtime_cache": str(targets["runtime_cache"]),
            "staging": str(staging),
        },
    )

    try:
        with pytest.raises(cli._BootstrapError) as caught:
            cli._bootstrap_request(execution_root / "request.json", staging)
        assert caught.value.code == "unsafe_frozen_inputs"
    finally:
        linked = targets[field]
        if linked.is_symlink():
            linked.unlink()
        elif bool(getattr(os.path, "isjunction", lambda _value: False)(linked)):
            linked.rmdir()
        shutil.rmtree(project_root, ignore_errors=True)
        for parent in (project_root.parent, project_root.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                pass


def test_early_attempt_refuses_linked_attempts_directory(
    tmp_path: Path,
) -> None:
    from scripts.research.local_quant_research import runner

    repo = tmp_path / "repo"
    attempts = repo / ".local/quant-research/_invalid/.attempts"
    outside = tmp_path / "outside-attempts"
    attempts.parent.mkdir(parents=True)
    outside.mkdir()
    try:
        attempts.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links are unavailable: {exc}")

    result = runner.run_project(repo / "missing.json", repo_root=repo)

    assert result.status == "evidence_insufficient"
    assert result.attempt_id is None
    assert list(outside.iterdir()) == []


def test_output_run_directory_rejects_existing_directory_link(
    tmp_path: Path,
) -> None:
    from scripts.research.local_quant_research import runner

    repo = tmp_path / "repo"
    project_root = repo / ".local/quant-research/minimal-fixture"
    run_id = "b" * 64
    linked_run = project_root / run_id
    outside = tmp_path / "outside-run"
    project_root.mkdir(parents=True)
    outside.mkdir()
    try:
        linked_run.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links are unavailable: {exc}")

    with pytest.raises(runner.ConfigurationError) as caught:
        runner._resolve_output_run_dir(repo, "minimal-fixture", run_id)

    assert caught.value.code == "unsafe_output_root"


def test_private_execute_rejects_staging_not_bound_to_frozen_request(
    repo_root: Path,
) -> None:
    token = uuid.uuid4().hex
    project_root = repo_root / ".local/quant-research/private-protocol-test"
    execution_root = project_root / f".{token}.inputs"
    repository = execution_root / "repository"
    expected_staging = project_root / f".{token}.tmp"
    supplied_staging = project_root / f".{token}.other.tmp"
    try:
        repository.mkdir(parents=True)
        _write_json(
            execution_root / "request.json",
            {
                "schema_version": 2,
                "repository": str(repository.resolve()),
                "market_data": str((execution_root / "market-data").resolve()),
                "live_repository": str(repo_root.resolve()),
                "runtime_cache": str((execution_root / "runtime-cache").resolve()),
                "staging": str(expected_staging.resolve()),
            },
        )

        completed = subprocess.run(
            [
                str(repo_root / ".venv/Scripts/python.exe"),
                str(repo_root / "scripts/research/local_quant_research/cli.py"),
                "_execute",
                "--frozen-inputs",
                str(execution_root / "request.json"),
                "--staging",
                str(supplied_staging),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )

        assert completed.returncode == 2
        assert json.loads(completed.stdout) == {
            "reasons": ["staging_mismatch"],
            "status": "evidence_insufficient",
        }
        assert not supplied_staging.exists()
    finally:
        shutil.rmtree(project_root, ignore_errors=True)
        try:
            project_root.parent.rmdir()
        except OSError:
            pass


def _build_private_bootstrap_layout(
    repo_root: Path,
    project_id: str,
) -> tuple[Path, Path, Path, Path]:
    run_id = "d" * 64
    attempt_id = uuid.uuid4().hex
    project_root = repo_root / ".local/quant-research" / project_id
    execution_root = project_root / f".{run_id}.{attempt_id}.inputs"
    frozen = execution_root / "repository"
    staging = project_root / f".{run_id}.{attempt_id}.tmp"
    (frozen / "scripts/research/local_quant_research").mkdir(parents=True)
    (execution_root / "market-data").mkdir()
    (execution_root / "runtime-cache").mkdir()
    _write_json(
        execution_root / "request.json",
        {
            "schema_version": 2,
            "project_id": project_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "output_root": str((repo_root / ".local/quant-research").resolve()),
            "repository": str(frozen.resolve()),
            "market_data": str((execution_root / "market-data").resolve()),
            "live_repository": str(repo_root.resolve()),
            "runtime_cache": str((execution_root / "runtime-cache").resolve()),
            "staging": str(staging.resolve()),
        },
    )
    return project_root, execution_root, frozen, staging


def test_private_execute_maps_guard_import_failure_without_traceback(
    repo_root: Path,
) -> None:
    project_root, execution_root, frozen, staging = _build_private_bootstrap_layout(
        repo_root,
        "private-guard-error-test",
    )
    guard = frozen / "scripts/research/local_quant_research/adapter_guard.py"
    guard.write_text("raise RuntimeError('sensitive guard detail')\n", encoding="utf-8")
    try:
        completed = subprocess.run(
            [
                str(repo_root / ".venv/Scripts/python.exe"),
                str(repo_root / "scripts/research/local_quant_research/cli.py"),
                "_execute",
                "--frozen-inputs",
                str(execution_root / "request.json"),
                "--staging",
                str(staging),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )

        assert completed.returncode == 1
        assert json.loads(completed.stdout) == {
            "reasons": ["frozen_bootstrap_failed"],
            "status": "failed",
        }
        assert completed.stderr == ""
        assert "Traceback" not in completed.stdout
        assert "sensitive guard detail" not in completed.stdout
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def test_private_execute_rejects_linked_guard_before_top_level_execution(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    project_root, execution_root, frozen, staging = _build_private_bootstrap_layout(
        repo_root,
        "private-linked-guard-test",
    )
    marker = tmp_path / "linked-guard-executed"
    external = tmp_path / "external-guard.py"
    external.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    guard = frozen / "scripts/research/local_quant_research/adapter_guard.py"
    try:
        guard.symlink_to(external)
    except OSError as exc:
        shutil.rmtree(project_root, ignore_errors=True)
        pytest.skip(f"file links are unavailable: {exc}")
    try:
        completed = subprocess.run(
            [
                str(repo_root / ".venv/Scripts/python.exe"),
                str(repo_root / "scripts/research/local_quant_research/cli.py"),
                "_execute",
                "--frozen-inputs",
                str(execution_root / "request.json"),
                "--staging",
                str(staging),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )

        assert completed.returncode == 2
        assert json.loads(completed.stdout) == {
            "reasons": ["unsafe_frozen_inputs"],
            "status": "evidence_insufficient",
        }
        assert completed.stderr == ""
        assert not marker.exists()
    finally:
        guard.unlink(missing_ok=True)
        shutil.rmtree(project_root, ignore_errors=True)


@pytest.mark.parametrize("action", ("write", "process"))
def test_frozen_child_guards_strategy_top_level_before_execution(
    repo_root: Path,
    tmp_path: Path,
    action: str,
) -> None:
    project_root, execution_root, frozen, staging = _build_private_bootstrap_layout(
        repo_root,
        f"private-strategy-{action}-test",
    )
    for relative in (
        Path("scripts/__init__.py"),
        Path("scripts/research/__init__.py"),
    ):
        destination = frozen / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo_root / relative, destination)
    for relative in (
        Path("scripts/research/local_quant_research"),
        Path("scripts/research/market_data"),
    ):
        shutil.copytree(repo_root / relative, frozen / relative, dirs_exist_ok=True)
    strategy_root = frozen / "projects/malicious"
    strategy_root.mkdir(parents=True)
    marker = tmp_path / f"strategy-{action}-escaped"
    entered = execution_root / "runtime-cache" / f"entered-{action}.txt"
    caught = execution_root / "runtime-cache" / f"caught-{action}.txt"
    side_effect = (
        f"Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
        if action == "write"
        else "subprocess.run([sys.executable, '-c', 'pass'], check=True)"
    )
    (strategy_root / "strategy.py").write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        f"Path({str(entered)!r}).write_text('entered', encoding='utf-8')\n"
        "try:\n"
        f"    {side_effect}\n"
        "except BaseException as exc:\n"
        f"    Path({str(caught)!r}).write_text(type(exc).__name__, encoding='utf-8')\n"
        "    raise\n"
        "MODULE = object()\n",
        encoding="utf-8",
    )
    _write_json(strategy_root / "scenario.json", {"scenario_id": "baseline"})
    _write_json(
        strategy_root / "run.json",
        {
            "schema_version": 2,
            "project_id": f"private-strategy-{action}-test",
            "strategy": {
                "root": "projects/malicious",
                "module": "strategy",
                "symbol": "MODULE",
            },
            "snapshot_id": "e" * 64,
            "snapshot_requirements": {},
            "scenario_config": "projects/malicious/scenario.json",
            "declared_inputs": [],
        },
    )
    request_path = execution_root / "request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["config"] = "projects/malicious/run.json"
    _write_json(request_path, request)
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            [
                str(repo_root / ".venv/Scripts/python.exe"),
                str(repo_root / "scripts/research/local_quant_research/cli.py"),
                "_execute",
                "--frozen-inputs",
                str(request_path),
                "--staging",
                str(staging),
            ],
            cwd=repo_root,
            env=environment,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )

        assert completed.returncode == 1, completed.stderr + completed.stdout
        assert json.loads(completed.stdout) == {
            "reasons": ["access_guard_violation"],
            "status": "failed",
        }
        assert completed.stderr == ""
        assert entered.read_text(encoding="utf-8") == "entered"
        assert caught.read_text(encoding="utf-8") == "PermissionError"
        assert not marker.exists()
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def test_freezing_copies_every_captured_shared_runtime_source(
    tmp_path: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import runner

    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    observed: set[str] = set()

    def inspect_frozen_runtime(command: list[str], **_kwargs: object):
        request_path = Path(command[command.index("--frozen-inputs") + 1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        frozen_repository = Path(request["repository"])
        observed.update(
            item.relative_to(frozen_repository).as_posix()
            for item in frozen_repository.rglob("*.py")
        )
        return subprocess.CompletedProcess(
            command,
            1,
            stdout='{"reasons":["stop_after_inspection"],"status":"failed"}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", inspect_frozen_runtime)

    result = runner.run_project(config_path, repo_root=fake_root)

    assert result.status == "failed"
    assert {
        "scripts/__init__.py",
        "scripts/research/__init__.py",
        "scripts/research/local_quant_research/runner.py",
        "scripts/research/local_quant_research/scenario.py",
        "scripts/research/market_data/query.py",
    }.issubset(observed)


def test_parent_runner_never_executes_strategy_top_level_before_frozen_child(
    tmp_path: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import runner

    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    strategy = fake_root / "projects/generic-research/strategy.py"
    marker = fake_root.parent / "parent-strategy-executed"
    source = strategy.read_text(encoding="utf-8")
    strategy.write_text(
        source.replace(
            "from __future__ import annotations\n",
            "from __future__ import annotations\n"
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
            1,
        ),
        encoding="utf-8",
    )
    process_calls = 0

    def stop_at_child(command: list[str], **_kwargs: object):
        nonlocal process_calls
        process_calls += 1
        return subprocess.CompletedProcess(
            command,
            1,
            stdout='{"reasons":["stop_at_child"],"status":"failed"}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", stop_at_child)

    result = runner.run_project(config_path, repo_root=fake_root)

    assert result.status == "failed"
    assert process_calls == 1
    assert not marker.exists()


def test_freezing_uses_first_captured_scenario_digest_before_process(
    tmp_path: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import runner

    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    scenario = fake_root / "projects/generic-research/scenario.json"
    original_copy = runner._copy_v2_inputs
    process_calls = 0

    def change_after_identity(**kwargs: object) -> None:
        scenario.write_text(
            '{"parameter":8,"scenario_id":"baseline","schema_version":1}\n',
            encoding="utf-8",
        )
        original_copy(**kwargs)

    def record_process(*_args: object, **_kwargs: object):
        nonlocal process_calls
        process_calls += 1
        return subprocess.CompletedProcess([], 1, stdout="", stderr="")

    monkeypatch.setattr(runner, "_copy_v2_inputs", change_after_identity)
    monkeypatch.setattr(subprocess, "run", record_process)

    result = runner.run_project(config_path, repo_root=fake_root)

    assert result.status == "failed"
    assert "changed" in " ".join(result.reasons)
    assert process_calls == 0


def test_private_execute_bootstrap_loads_runner_from_frozen_repository(
    repo_root: Path,
) -> None:
    run_id = "c" * 64
    attempt_id = uuid.uuid4().hex
    project_root = repo_root / ".local/quant-research/private-freeze-test"
    execution_root = project_root / f".{run_id}.{attempt_id}.inputs"
    frozen = execution_root / "repository"
    staging = project_root / f".{run_id}.{attempt_id}.tmp"
    modules = {
        "scripts/__init__.py": "",
        "scripts/research/__init__.py": "",
        "scripts/research/local_quant_research/__init__.py": "",
        "scripts/research/local_quant_research/runner.py": (
            "from .adapter_guard import INSTALLED\n"
            "class ConfigurationError(ValueError):\n"
            "    def __init__(self, code, message):\n"
            "        super().__init__(message)\n"
            "        self.code = code\n"
            "def execute_frozen_inputs(_request, _staging):\n"
            "    if not INSTALLED:\n"
            "        raise RuntimeError('guard was not installed')\n"
            "    return {'status': 'complete', 'reasons': [], 'source': 'frozen'}\n"
        ),
        "scripts/research/local_quant_research/adapter_guard.py": (
            "INSTALLED = False\n"
            "def install_access_guard(*_args, **_kwargs):\n"
            "    global INSTALLED\n"
            "    INSTALLED = True\n"
        ),
        "scripts/research/local_quant_research/contracts.py": (
            "class StrategyEvidenceError(RuntimeError):\n"
            "    code = 'strategy_evidence'\n"
        ),
        "scripts/research/local_quant_research/performance.py": (
            "class PerformanceGateError(RuntimeError):\n"
            "    code = 'performance_gate'\n"
        ),
        "scripts/research/local_quant_research/result_package.py": (
            "class ResultContractError(ValueError):\n"
            "    pass\n"
        ),
        "scripts/research/local_quant_research/strategy_loader.py": (
            "class ConfigurationError(ValueError):\n"
            "    def __init__(self, code='strategy_config', message='invalid'):\n"
            "        super().__init__(message)\n"
            "        self.code = code\n"
        ),
        "scripts/research/market_data/__init__.py": "",
        "scripts/research/market_data/storage.py": (
            "class MarketDataError(RuntimeError):\n"
            "    pass\n"
        ),
    }
    try:
        for relative, source in modules.items():
            path = frozen / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        (execution_root / "market-data").mkdir()
        (execution_root / "runtime-cache").mkdir()
        _write_json(
            execution_root / "request.json",
                {
                    "schema_version": 2,
                    "project_id": "private-freeze-test",
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "output_root": str((repo_root / ".local/quant-research").resolve()),
                    "repository": str(frozen.resolve()),
                "market_data": str((execution_root / "market-data").resolve()),
                "live_repository": str(repo_root.resolve()),
                "runtime_cache": str((execution_root / "runtime-cache").resolve()),
                "staging": str(staging.resolve()),
            },
        )

        completed = subprocess.run(
            [
                str(repo_root / ".venv/Scripts/python.exe"),
                str(repo_root / "scripts/research/local_quant_research/cli.py"),
                "_execute",
                "--frozen-inputs",
                str(execution_root / "request.json"),
                "--staging",
                str(staging),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr + completed.stdout
        assert json.loads(completed.stdout)["source"] == "frozen"
    finally:
        shutil.rmtree(project_root, ignore_errors=True)
        try:
            project_root.parent.rmdir()
        except OSError:
            pass


def test_completed_package_reuse_binds_all_frozen_identity_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import result_package, runner
    from scripts.research.local_quant_research.evidence import canonical_digest

    package = tmp_path / "package"
    project_run = {"schema_version": 2, "project_id": "minimal-fixture"}
    scenario = {"schema_version": 1, "scenario_id": "baseline"}
    declared_inputs = [{"path": "input.txt", "sha256": "1" * 64}]
    code_identity = {
        "schema_version": 1,
        "files": [{"path": "strategy.py", "sha256": "2" * 64}],
        "inputs": {
            "project_run": {"path": "run.json", "sha256": "3" * 64},
            "scenario": {"path": "scenario.json", "sha256": "4" * 64},
            "declared_inputs": declared_inputs,
        },
    }
    runtime_lock = {"schema_version": 1, "python": "3.12", "dependencies": {}}
    market_snapshot = {"schema_version": 1, "snapshot_id": "5" * 64}
    config_digest = canonical_digest(
        {
            "project_run": project_run,
            "scenario": scenario,
            "declared_inputs": declared_inputs,
        }
    )
    code_digest = canonical_digest(
        {
            "code_identity": {
                "schema_version": 1,
                "files": code_identity["files"],
            },
            "runtime_lock": runtime_lock,
        }
    )
    run_id = runner.compute_run_id(
        canonical_digest(market_snapshot),
        config_digest,
        code_digest,
    )
    for relative, document in {
        "config/project-run.json": project_run,
        "config/scenario.json": scenario,
        "config/code-identity.json": code_identity,
        "evidence/market-snapshot.json": market_snapshot,
        "evidence/runtime-lock.json": runtime_lock,
    }.items():
        _write_json(package / relative, document)
    manifest = {
        "object": {
            "kind": "local_research",
            "status": "complete",
            "strategy_id": "minimal-fixture",
            "scenario_id": "baseline",
            "run_id": run_id,
        }
    }
    monkeypatch.setattr(result_package, "validate_result_package", lambda _path: manifest)
    expected = {
        "project_id": "minimal-fixture",
        "run_id": run_id,
        "project_run": project_run,
        "scenario_document": scenario,
        "code_identity": code_identity,
        "runtime_lock": runtime_lock,
        "market_snapshot": market_snapshot,
        "config_sha256": config_digest,
        "code_sha256": code_digest,
    }

    runner._package_identity(package, expected=expected)
    manifest["object"]["scenario_id"] = "other"
    with pytest.raises(EvidenceError, match="identity"):
        runner._package_identity(package, expected=expected)
    manifest["object"]["scenario_id"] = "baseline"
    _write_json(
        package / "config/project-run.json",
        {"schema_version": 2, "project_id": "other"},
    )

    with pytest.raises(EvidenceError, match="identity"):
        runner._package_identity(package, expected=expected)


def _performance_module():
    try:
        return importlib.import_module(
            "scripts.research.local_quant_research.performance"
        )
    except ModuleNotFoundError:
        pytest.fail("shared performance module is missing")


def test_daily_performance_runs_exactly_one_cold_and_one_warm() -> None:
    performance = _performance_module()
    calls: list[int] = []

    def operation() -> dict[str, object]:
        calls.append(len(calls))
        return {"execution": "same"}

    outcome, evidence = performance.run_cold_warm(
        operation,
        digest=lambda value: value["execution"],
    )

    assert outcome == {"execution": "same"}
    assert calls == [0, 1]
    assert evidence.cold.digest == evidence.warm.digest == "same"
    assert evidence.cold.seconds < 180
    assert evidence.warm.seconds < 180


def test_daily_performance_releases_cold_outcome_before_warm() -> None:
    performance = _performance_module()
    references: list[weakref.ReferenceType[object]] = []

    class Outcome:
        pass

    def operation() -> Outcome:
        if references:
            gc.collect()
            assert references[0]() is None
        outcome = Outcome()
        references.append(weakref.ref(outcome))
        return outcome

    performance.run_cold_warm(
        operation,
        digest=lambda _value: "same",
    )


def test_daily_performance_rejects_full_execution_digest_mismatch() -> None:
    performance = _performance_module()
    calls = 0

    def operation() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"execution": calls}

    with pytest.raises(performance.PerformanceGateError) as caught:
        performance.run_cold_warm(
            operation,
            digest=lambda value: str(value["execution"]),
        )

    assert caught.value.code == "execution_digest_mismatch"
    assert calls == 2


def test_daily_performance_counts_digest_time_in_each_180_second_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    performance = _performance_module()
    clock = 0.0

    def now() -> float:
        return clock

    def slow_digest(value: str) -> str:
        nonlocal clock
        clock = 180.0
        return value

    monkeypatch.setattr(performance.time, "perf_counter", now)

    with pytest.raises(performance.PerformanceGateError) as caught:
        performance.run_cold_warm(lambda: "same", digest=slow_digest)

    assert caught.value.code == "cold_performance_limit"


@pytest.mark.parametrize(
    ("clock", "reason"),
    (
        ([0.0, 180.0, 180.0, 181.0], "cold_performance_limit"),
        ([0.0, 1.0, 1.0, 181.0], "warm_performance_limit"),
    ),
)
def test_daily_performance_rejects_each_180_second_limit(
    clock: list[float],
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    performance = _performance_module()
    values = iter(clock)
    monkeypatch.setattr(performance.time, "perf_counter", lambda: next(values))

    with pytest.raises(performance.PerformanceGateError) as caught:
        performance.run_cold_warm(lambda: "same", digest=lambda value: value)

    assert caught.value.code == reason


def test_daily_performance_adds_single_writer_duration_to_both_limits() -> None:
    performance = _performance_module()
    _, evidence = performance.run_cold_warm(
        lambda: "same",
        digest=lambda value: value,
    )

    with pytest.raises(performance.PerformanceGateError) as caught:
        performance.include_shared_work(evidence, 180.0)

    assert caught.value.code == "cold_performance_limit"
