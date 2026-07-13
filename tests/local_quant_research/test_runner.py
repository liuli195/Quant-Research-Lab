from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from scripts.research.local_quant_research.runner import run_project
from scripts.research.local_quant_research.evidence import canonical_digest
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


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_repo(tmp_path: Path, source_repo: Path) -> tuple[Path, Path, dict[str, object]]:
    root = tmp_path / "repo"
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"test launcher")

    project_dir = root / "projects" / "generic-research"
    entry = project_dir / "adapter.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# generic adapter\n", encoding="utf-8")
    project_config = project_dir / "project.json"
    _write_json(project_config, {"schema_version": 1, "parameter": 7})
    declared_input = project_dir / "input.txt"
    declared_input.write_text("declared input\n", encoding="utf-8")
    code_identity = project_dir / "code-identity.json"
    _write_json(
        code_identity,
        {
            "schema_version": 1,
            "files": [
                {
                    "path": "projects/generic-research/adapter.py",
                    "sha256": _sha256(entry),
                }
            ],
        },
    )

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
        "schema_version": 1,
        "project_id": "generic-research",
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_requirements": selection.to_document(),
        "project_entry": "projects/generic-research/adapter.py",
        "command": [
            ".venv/Scripts/python.exe",
            "projects/generic-research/adapter.py",
        ],
        "project_config": "projects/generic-research/project.json",
        "code_identity": "projects/generic-research/code-identity.json",
        "declared_inputs": ["projects/generic-research/input.txt"],
        "required_outputs": [{"path": "result.json", "format": "json"}],
        "output_root": ".local/quant-research",
        "stop_states": ["complete", "evidence_insufficient", "failed"],
    }
    config_path = project_dir / "run.json"
    _write_json(config_path, config)
    return root, config_path, config


def _output_dir(command: list[str]) -> Path:
    return Path(command[command.index("--output-dir") + 1])


def _successful_process(
    assertions: Callable[[list[str], dict[str, object]], None] | None = None,
):
    def fake_run(command: list[str], **kwargs):
        if assertions:
            assertions(command, kwargs)
        output_dir = _output_dir(command)
        _write_json(
            output_dir / "project-status.json",
            {"schema_version": 1, "status": "complete", "reason_codes": []},
        )
        _write_json(output_dir / "result.json", {"answer": 42})
        return subprocess.CompletedProcess(command, 0, stdout="ignored", stderr="ignored")

    return fake_run


@pytest.mark.parametrize(
    "mutation",
    [
        lambda config: config.update(command="python adapter.py"),
        lambda config: config.update(project_entry="../outside.py"),
        lambda config: config.pop("snapshot_id"),
        lambda config: config.update(required_outputs=[]),
        lambda config: config.update(
            stop_states=["complete", "evidence_insufficient", "unknown"]
        ),
        lambda config: config.update(
            command=["python.exe", "projects/generic-research/adapter.py"]
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
        "missing-outputs",
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
    market_csv = next((tampered_root / ".local/market-data/batches").rglob("market-data.csv"))
    market_csv.write_bytes(market_csv.read_bytes() + b"\n")
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
        _write_json(
            _output_dir(command) / "project-status.json",
            {
                "schema_version": 1,
                "status": "evidence_insufficient",
                "reason_codes": ["insufficient_domain_samples"],
            },
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

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
            fake_root / "scripts/research/local_quant_research/adapter_guard.py"
        ).resolve()
        assert Path(command[command.index("--entry") + 1]) == (
            Path(command[command.index("--execution-root") + 1])
            / "repository/projects/generic-research/adapter.py"
        )
        assert kwargs["shell"] is False
        assert Path(kwargs["cwd"]) == _output_dir(command)
        assert "RESEARCH_TEST_PASSWORD" not in kwargs["env"]
        assert command.count("--snapshot-manifest") == 1
        assert command.count("--project-config") == 1
        assert command.count("--output-dir") == 1

    monkeypatch.setattr(subprocess, "run", _successful_process(assert_invocation))

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "complete"
    assert result.reused is False
    assert result.run_path == (
        fake_root / ".local" / "quant-research" / "generic-research" / result.run_id
    )
    assert {path.name for path in result.run_path.iterdir()} == {
        "project-status.json",
        "result.json",
        "run-manifest.json",
    }
    manifest = json.loads(
        (result.run_path / "run-manifest.json").read_text(encoding="utf-8")
    )
    assert [stage["name"] for stage in manifest["stages"]] == [
        "snapshot_validation",
        "config_validation",
        "project_execution",
        "output_validation",
        "evidence_finalization",
    ]
    assert all(stage["status"] == "complete" for stage in manifest["stages"])
    assert manifest["status"] == "complete"
    assert not list((fake_root / ".local/quant-research").rglob("*.tmp"))


def test_same_complete_identity_is_revalidated_and_reused_without_execution(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    monkeypatch.setattr(subprocess, "run", _successful_process())
    first = run_project(config_path, repo_root=fake_root)
    before = {path.name: _sha256(path) for path in first.run_path.iterdir()}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("complete run must be reused"),
    )

    second = run_project(config_path, repo_root=fake_root)

    assert second.status == "complete"
    assert second.reused is True
    assert second.run_id == first.run_id
    assert {path.name: _sha256(path) for path in second.run_path.iterdir()} == before


def test_tampered_complete_output_fails_without_overwriting_old_run(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)
    monkeypatch.setattr(subprocess, "run", _successful_process())
    first = run_project(config_path, repo_root=fake_root)
    output = first.run_path / "result.json"
    output.write_text('{"answer":99}\n', encoding="utf-8")
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
    manifest_path = first.run_path / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    (first.run_path / "result.json").unlink()
    replacement = first.run_path / "replacement.txt"
    replacement.write_text("replacement\n", encoding="utf-8")
    outputs = [
        {
            "path": "replacement.txt",
            "format": "text",
            "bytes": replacement.stat().st_size,
            "sha256": _sha256(replacement),
        }
    ]
    manifest["outputs"] = outputs
    manifest["output_set_sha256"] = canonical_digest(outputs)
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
    monkeypatch.setattr(
        runner,
        "validate_complete_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            runner.EvidenceError("forced post-publish validation failure")
        ),
    )

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "failed"
    project_root = fake_root / ".local/quant-research/generic-research"
    assert not any(path.is_dir() and len(path.name) == 64 for path in project_root.iterdir())


@pytest.mark.parametrize("project_status", ["failed", "mystery"])
def test_project_failure_or_unknown_status_records_attempt_not_complete_run(
    project_status: str,
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root, config_path, _ = _build_repo(tmp_path, repo_root)

    def process(command: list[str], **kwargs):
        _write_json(
            _output_dir(command) / "project-status.json",
            {
                "schema_version": 1,
                "status": project_status,
                "reason_codes": ["project_declined"],
            },
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

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
        _write_json(
            _output_dir(command) / "project-status.json",
            {"schema_version": 1, "status": "complete", "reason_codes": []},
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

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
        declared_input.write_text("tampered input\n", encoding="utf-8")
        frozen_project_config = Path(command[command.index("--project-config") + 1])
        frozen_input = frozen_project_config.parent / "input.txt"
        value_used = frozen_input.read_text(encoding="utf-8")
        os.utime(
            declared_input,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        declared_input.write_text("declared input\n", encoding="utf-8")
        os.utime(
            declared_input,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        output_dir = _output_dir(command)
        _write_json(
            output_dir / "project-status.json",
            {"schema_version": 1, "status": "complete", "reason_codes": []},
        )
        _write_json(output_dir / "result.json", {"input": value_used})
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", process)

    result = run_project(config_path, repo_root=fake_root)

    assert result.status == "complete"
    output = json.loads((result.run_path / "result.json").read_text(encoding="utf-8"))
    assert output == {"input": "declared input\n"}
    project_root = fake_root / ".local/quant-research/generic-research"
    assert not list(project_root.glob(".*.inputs"))


def test_adapter_guard_allows_staging_writes_and_blocks_external_writes(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    execution_root = tmp_path / "execution"
    adapter = execution_root / "repository" / "adapter.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "output = Path(sys.argv[1])\n"
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
