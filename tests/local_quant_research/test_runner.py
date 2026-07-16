from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
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
        "    Path(sys.argv[3]).read_text(encoding='utf-8')\n",
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
