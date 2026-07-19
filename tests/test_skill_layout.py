from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_claude_skill_resolves_to_agents_skill(repo_root: Path) -> None:
    source = repo_root / ".agents" / "skills" / "joinquant-archive-sync"
    claude = repo_root / ".claude" / "skills" / "joinquant-archive-sync"
    assert (source / "SKILL.md").is_file()
    assert claude.is_symlink()
    assert claude.resolve() == source.resolve()
    assert _sha256(claude / "SKILL.md") == _sha256(source / "SKILL.md")
    assert _sha256(claude / "scripts" / "jq_sync.py") == _sha256(
        source / "scripts" / "jq_sync.py"
    )


def test_skill_contains_no_plugin_manifest(repo_root: Path) -> None:
    assert not list(repo_root.glob("**/.codex-plugin/plugin.json"))
    assert not list(repo_root.glob("**/.claude-plugin/plugin.json"))


def test_skill_routes_every_operation_through_one_cli(repo_root: Path) -> None:
    skill = (
        repo_root / ".agents" / "skills" / "joinquant-archive-sync" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert skill.startswith("---\nname: joinquant-archive-sync\ndescription: Use when ")
    assert "scripts/jq_sync.py" in skill
    for command in (
        "auth",
        "list-targets",
        "sync-backtest",
        "sync-active-simulations",
        "verify",
        "query",
        "export-csv",
        "self-test",
        "schedule-install",
        "schedule-status",
        "schedule-uninstall",
    ):
        assert f"`{command}`" in skill
    assert "latest" in skill
    assert "1000" in skill
    assert "capped_free" in skill
    assert "missing_at_source" in skill
    assert "auth_required" in skill
    assert "积分" in skill
    assert "归因" in skill


def test_joinquant_docs_skill_resolves_to_agents_skill(repo_root: Path) -> None:
    source = repo_root / ".agents" / "skills" / "joinquant-docs-sync"
    claude = repo_root / ".claude" / "skills" / "joinquant-docs-sync"
    assert (source / "SKILL.md").is_file()
    assert (source / "agents" / "openai.yaml").is_file()
    assert (source / "references" / "sources.json").is_file()
    assert claude.is_symlink()
    assert claude.resolve() == source.resolve()
    assert _sha256(claude / "SKILL.md") == _sha256(source / "SKILL.md")
    assert _sha256(claude / "scripts" / "jq_docs_sync.py") == _sha256(
        source / "scripts" / "jq_docs_sync.py"
    )


def test_joinquant_docs_skill_routes_operations_through_one_cli(
    repo_root: Path,
) -> None:
    skill = (
        repo_root / ".agents" / "skills" / "joinquant-docs-sync" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert skill.startswith("---\nname: joinquant-docs-sync\ndescription: Use when ")
    assert "scripts/jq_docs_sync.py" in skill
    for command in ("preview", "sync", "verify", "self-test"):
        assert f"`{command}`" in skill
    assert "Cookie" in skill
    assert "Token" in skill
    assert "SHA-256" in skill


def test_local_quant_research_skill_resolves_to_agents_skill(
    repo_root: Path,
) -> None:
    source = repo_root / ".agents" / "skills" / "run-local-quant-research"
    claude = repo_root / ".claude" / "skills" / "run-local-quant-research"
    assert (source / "SKILL.md").is_file()
    assert (source / "agents" / "openai.yaml").is_file()
    assert claude.is_symlink()
    assert claude.resolve() == source.resolve()
    assert _sha256(claude / "SKILL.md") == _sha256(source / "SKILL.md")


def test_local_research_and_standard_analysis_skills_do_not_call_each_other(
    repo_root: Path,
) -> None:
    local_skill = (
        repo_root / ".agents" / "skills" / "run-local-quant-research" / "SKILL.md"
    ).read_text(encoding="utf-8")
    analysis_skill = (
        repo_root / ".agents" / "skills" / "analyze-quant-robustness" / "SKILL.md"
    ).read_text(encoding="utf-8")
    local_runtime = repo_root / "scripts" / "research" / "local_quant_research"
    analysis_runtime = (
        repo_root
        / ".agents"
        / "skills"
        / "analyze-quant-robustness"
        / "scripts"
    )

    assert "analyze-quant-robustness" not in local_skill
    assert "quant_analysis" not in local_skill
    assert "不得联网、认证、读取凭证或调用 `run-local-quant-research`" in analysis_skill
    assert all(
        "analyze-quant-robustness" not in path.read_text(encoding="utf-8")
        and "quant_analysis" not in path.read_text(encoding="utf-8")
        and "scripts.research.analysis_data" not in path.read_text(encoding="utf-8")
        for path in local_runtime.rglob("*.py")
    )
    assert all(
        "run-local-quant-research" not in path.read_text(encoding="utf-8")
        and "scripts.research.local_quant_research" not in path.read_text(encoding="utf-8")
        for path in analysis_runtime.rglob("*.py")
    )


def test_standard_analysis_import_graph_excludes_local_research_runtime(
    repo_root: Path,
) -> None:
    entry = (
        repo_root
        / ".agents/skills/analyze-quant-robustness/scripts/analyze_quant_robustness.py"
    )
    completed = subprocess.run(
        [
            str(repo_root / ".venv/Scripts/python.exe"),
            "-c",
            (
                "import runpy,sys;"
                f"runpy.run_path({str(entry)!r},run_name='skill_import');"
                "print('\\n'.join(sorted(name for name in sys.modules "
                "if name.startswith('scripts.research.local_quant_research'))))"
            ),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        shell=False,
        check=True,
    )

    assert completed.stdout == "\n"


def test_local_research_import_graph_excludes_analysis_runtime(
    repo_root: Path,
) -> None:
    completed = subprocess.run(
        [
            str(repo_root / ".venv/Scripts/python.exe"),
            "-c",
            (
                "import scripts.research.local_quant_research.runner,sys;"
                "print('\\n'.join(sorted(name for name in sys.modules "
                "if name.startswith('scripts.research.analysis_data'))))"
            ),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        shell=False,
        check=True,
    )

    assert completed.stdout == "\n"


def test_standard_analysis_skill_is_the_only_public_entry(repo_root: Path) -> None:
    source = repo_root / ".agents" / "skills" / "analyze-quant-robustness"
    claude = repo_root / ".claude" / "skills" / "analyze-quant-robustness"

    assert (source / "SKILL.md").is_file()
    assert (source / "agents" / "openai.yaml").is_file()
    assert (source / "scripts" / "analyze_quant_robustness.py").is_file()
    assert (source / "scripts" / "quant_analysis" / "__init__.py").is_file()
    assert claude.is_symlink()
    assert claude.resolve() == source.resolve()
    skill = (source / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: analyze-quant-robustness\ndescription: Use when ")
    assert "scripts\\analyze_quant_robustness.py" in skill
    assert "`run`" in skill
    assert "`report`" in skill
    assert "-m scripts.research.quant_analysis" not in skill
    assert "不得启动、提交、同步或修改" in skill


def test_build_and_verify_covers_joinquant_docs_sync(repo_root: Path) -> None:
    config = json.loads(
        (repo_root / ".build-and-verify" / "config.json").read_text(encoding="utf-8")
    )
    checks = {check["id"]: check for check in config["verify"]["checks"]}
    check = checks["verify.docs-sync"]
    assert check["command"] == (
        "set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& "
        ".\\.venv\\Scripts\\python.exe -m pytest "
        "tests\\joinquant_docs_sync\\test_cli.py"
    )
    assert ".agents/skills/joinquant-docs-sync/**" in check["paths"]
    assert ".claude/skills/joinquant-docs-sync" in check["paths"]
    assert "tests/joinquant_docs_sync/**" in check["inputs"]


def test_build_and_verify_covers_local_quant_research_without_local_data(
    repo_root: Path,
) -> None:
    config = json.loads(
        (repo_root / ".build-and-verify" / "config.json").read_text(encoding="utf-8")
    )
    checks = {check["id"]: check for check in config["verify"]["checks"]}
    unit_checks = [
        checks["verify.local-quant-research-package-unit"],
        checks["verify.local-quant-research-market-data-unit"],
        checks["verify.local-quant-research-contract-unit"],
    ]
    vectorbt_unit = checks["verify.local-quant-research-vectorbt-unit"]
    equivalence = [
        checks["verify.local-quant-research-equivalence-immediate-11"],
        checks["verify.local-quant-research-equivalence-immediate-17"],
        checks["verify.local-quant-research-equivalence-delayed-11"],
    ]
    e2e = checks["verify.local-quant-research-e2e"]
    turtle_e2e = checks["verify.local-quant-research-e2e-turtle"]
    jit = checks["verify.local-quant-research-jit"]
    layout = checks["verify.skill-layout"]
    scheduler_unit = checks["verify.scheduler-unit"]

    assert config["verify"]["maxParallel"] == 10
    assert config["verify"]["fullBudgetSeconds"] == 60
    assert len(checks) == 19
    assert [
        check["id"] for check in config["verify"]["checks"][:10]
    ] == [
        "verify.local-quant-research-equivalence-immediate-17",
        "verify.local-quant-research-equivalence-delayed-11",
        "verify.local-quant-research-equivalence-immediate-11",
        "verify.local-quant-research-e2e-turtle",
        "verify.local-quant-research-e2e",
        "verify.local-quant-research-package-unit",
        "verify.self-test",
        "verify.docs-sync",
        "verify.browser-research",
        "verify.skill-layout",
    ]
    assert all("NUMBA_DISABLE_JIT=1" in item["command"] for item in unit_checks)
    assert all("not turtle and not vectorbt" in item["command"] for item in unit_checks)
    assert "test_archive_promotion.py" in unit_checks[0]["command"]
    assert "test_market_data_storage.py" in unit_checks[1]["command"]
    assert "test_local_research_equivalence.py" in unit_checks[2]["command"]
    assert "not test_strategy_module_matches_frozen_equivalence_fixture" in (
        unit_checks[2]["command"]
    )
    assert "NUMBA_DISABLE_JIT=1" in vectorbt_unit["command"]
    assert '-k "turtle or vectorbt"' in vectorbt_unit["command"]
    assert [
        item["command"].rsplit("[", 1)[-1].split("]", 1)[0]
        for item in equivalence
    ] == ["immediate-11-etf", "immediate-17-etf", "delayed-11-etf-1d"]
    assert all("NUMBA_DISABLE_JIT=1" in item["command"] for item in equivalence)
    assert "test_generic_e2e.py" in e2e["command"]
    assert "test_local_research_v2_e2e.py" in e2e["command"]
    assert "test_turtle_e2e.py" in turtle_e2e["command"]
    assert "NUMBA_DISABLE_JIT=1" in e2e["command"]
    assert "NUMBA_DISABLE_JIT=1" in turtle_e2e["command"]
    assert jit["command"] == (
        "set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& "
        ".\\.venv\\Scripts\\python.exe -m pytest "
        "tests\\local_quant_research\\test_turtle_vectorbt_callbacks.py::"
        "test_group_and_portfolio_unit_scales_follow_confirmed_formula"
    )
    required_paths = {
        ".agents/skills/run-local-quant-research/**",
        ".claude/skills/run-local-quant-research",
        "scripts/research/**",
        "joinquant/strategies/strategy-003/research/**",
        "tests/local_quant_research/**",
        "tests/quant_analysis/**",
    }
    assert all(required_paths.issubset(item["paths"]) for item in unit_checks)
    assert required_paths.issubset(e2e["paths"])
    assert {".agents/skills/**", ".claude/skills/**"}.issubset(layout["paths"])
    assert {".agents/skills/**", ".claude/skills/**"}.issubset(layout["inputs"])
    local_checks = [*unit_checks, vectorbt_unit, *equivalence, e2e, turtle_e2e, jit]
    assert all(item["checkParallel"] is True for item in checks.values())
    assert scheduler_unit["pytestXdistWorkers"] == 4
    assert "-p xdist.plugin" in scheduler_unit["command"]
    assert "-n 4" not in scheduler_unit["command"]
    assert "tests\\joinquant_sync\\test_scheduler.py" in scheduler_unit["command"]
    assert "tests\\joinquant_sync\\test_scheduled_sync.py" in scheduler_unit["command"]
    assert 'not schtasks_runs_self_test' in scheduler_unit["command"]
    assert all(
        "pytestXdistWorkers" not in item
        for item in checks.values()
        if item is not scheduler_unit
    )
    assert all(
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in item["command"]
        for item in checks.values()
        if "pytest" in item["command"]
    )
    assert all(
        not path.startswith(".local/")
        for item in local_checks
        for path in item["inputs"]
    )


def test_full_verify_checkout_downloads_git_lfs_objects(repo_root: Path) -> None:
    workflow = (
        repo_root / ".github" / "workflows" / "full-verify.yml"
    ).read_text(encoding="utf-8")

    assert "uses: actions/checkout@v4\n        with:\n          lfs: true" in workflow
