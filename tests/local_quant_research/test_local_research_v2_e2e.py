from __future__ import annotations

import ast
import json
from pathlib import Path


_LEGACY_STRATEGY_FILES = (
    "indicators.py",
    "result_adapter.py",
    "single_scenario.py",
    "vectorbt_benchmark.py",
    "vectorbt_callbacks.py",
    "vectorbt_cli.py",
    "vectorbt_delayed.py",
    "vectorbt_engine.py",
    "vectorbt_inputs.py",
)
_LEGACY_CONFIG_FIELDS = {
    "code_identity",
    "command",
    "output_root",
    "project_config",
    "project_entry",
    "required_outputs",
    "stop_states",
}


def test_strategy_003_production_config_uses_only_v2_contract(
    repo_root: Path,
) -> None:
    config = json.loads(
        (
            repo_root
            / "joinquant/strategies/strategy-003/research/project-run.json"
        ).read_text(encoding="utf-8")
    )

    assert set(config) == {
        "schema_version",
        "project_id",
        "strategy",
        "snapshot_id",
        "snapshot_requirements",
        "scenario_config",
        "declared_inputs",
    }
    assert config["schema_version"] == 2
    assert config["project_id"] == "strategy-003"
    assert config["strategy"] == {
        "root": "joinquant/strategies/strategy-003/research",
        "module": "turtle_etf.strategy",
        "symbol": "MODULE",
    }
    assert config["scenario_config"] == (
        "joinquant/strategies/strategy-003/research/baseline.json"
    )
    assert config["declared_inputs"] == [
        "joinquant/strategies/strategy-003/manifest.json"
    ]
    assert config["snapshot_requirements"]
    assert _LEGACY_CONFIG_FIELDS.isdisjoint(config)


def test_obsolete_strategy_and_runner_paths_are_physically_absent(
    repo_root: Path,
) -> None:
    strategy_root = (
        repo_root / "joinquant/strategies/strategy-003/research/turtle_etf"
    )
    assert not [
        name for name in _LEGACY_STRATEGY_FILES if (strategy_root / name).exists()
    ]
    assert not (
        repo_root / "scripts/research/local_quant_research/adapter_guard.py"
    ).exists()
    assert not (
        repo_root / "joinquant/strategies/strategy-003/research/code-identity.json"
    ).exists()


def test_runner_contains_only_fixed_v2_execution_path(repo_root: Path) -> None:
    path = repo_root / "scripts/research/local_quant_research/runner.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    definitions = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    }

    assert {
        "_FrozenExecutionInputs",
        "_legacy_load_run_config",
        "_legacy_run_project",
    }.isdisjoint(definitions)
    assert "config.command" not in source
    assert "adapter_guard" not in source


def test_vectorbt_import_is_owned_only_by_shared_runtime(repo_root: Path) -> None:
    roots = (
        repo_root / "scripts/research/local_quant_research",
        repo_root / "joinquant/strategies/strategy-003/research/turtle_etf",
    )
    matches: list[str] = []
    for root in roots:
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = None
                if isinstance(node, ast.Import):
                    module = node.names[0].name
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    module = node.module
                if module and module.startswith("vectorbt"):
                    matches.append(path.relative_to(repo_root).as_posix())

    assert sorted(set(matches)) == [
        "scripts/research/local_quant_research/vectorbt_runtime.py"
    ]
