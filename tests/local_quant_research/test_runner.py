from __future__ import annotations

import gc
import importlib
import inspect
import json
import os
import shutil
import subprocess
import uuid
import weakref
from pathlib import Path

import pytest

from scripts.research.local_quant_research.evidence import EvidenceError
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


def _build_repo(tmp_path: Path, source_repo: Path) -> tuple[Path, Path, dict[str, object]]:
    root = tmp_path / "repo"
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"test launcher")

    project_dir = root / "projects" / "generic-research"
    strategy_module = f"strategy_{uuid.uuid4().hex}"
    entry = project_dir / f"{strategy_module}.py"
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
            "module": strategy_module,
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
    assert "scripts/research/local_quant_research/adapter_guard.py" not in observed


def test_v2_bootstrap_does_not_install_adapter_guard(repo_root: Path) -> None:
    from scripts.research.local_quant_research import cli, runner

    private_execute = inspect.getsource(cli._private_execute)
    assert "adapter_guard" not in private_execute
    assert "install_access_guard" not in private_execute
    assert "sys.modules" not in private_execute

    _, runtime_sources = runner._runtime_lock(repo_root)
    assert (
        repo_root / "scripts/research/local_quant_research/adapter_guard.py"
        not in runtime_sources
    )


def test_parent_runner_never_executes_strategy_top_level_before_frozen_child(
    tmp_path: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import runner

    fake_root, config_path, config = _build_repo(tmp_path, repo_root)
    strategy = (
        fake_root
        / "projects/generic-research"
        / f"{config['strategy']['module']}.py"
    )
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
            "class ConfigurationError(ValueError):\n"
            "    def __init__(self, code, message):\n"
            "        super().__init__(message)\n"
            "        self.code = code\n"
            "def execute_frozen_inputs(_request, _staging):\n"
            "    return {'status': 'complete', 'reasons': [], 'source': 'frozen'}\n"
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


def test_public_cli_omits_release_performance_workflow() -> None:
    from scripts.research.local_quant_research import cli

    assert "performance" not in cli._parser().format_help()
