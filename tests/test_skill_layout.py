from __future__ import annotations

import hashlib
import json
from pathlib import Path


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


def test_build_and_verify_covers_joinquant_docs_sync(repo_root: Path) -> None:
    config = json.loads(
        (repo_root / ".build-and-verify" / "config.json").read_text(encoding="utf-8")
    )
    checks = {check["id"]: check for check in config["verify"]["checks"]}
    check = checks["verify.docs-sync"]
    assert check["command"] == [
        ".\\.venv\\Scripts\\python.exe",
        "-m",
        "pytest",
        "tests\\joinquant_docs_sync\\test_cli.py",
    ]
    assert ".agents/skills/joinquant-docs-sync/**" in check["paths"]
    assert ".claude/skills/joinquant-docs-sync" in check["paths"]
    assert "tests/joinquant_docs_sync/test_cli.py" in check["inputs"]


def test_build_and_verify_covers_local_quant_research_without_local_data(
    repo_root: Path,
) -> None:
    config = json.loads(
        (repo_root / ".build-and-verify" / "config.json").read_text(encoding="utf-8")
    )
    checks = {check["id"]: check for check in config["verify"]["checks"]}
    unit = checks["verify.local-quant-research-unit"]
    e2e = checks["verify.local-quant-research-e2e"]
    layout = checks["verify.skill-layout"]

    assert unit["command"] == [
        ".\\.venv\\Scripts\\python.exe",
        "-m",
        "pytest",
        "tests\\local_quant_research",
        "tests\\quant_analysis",
        "--ignore=tests\\local_quant_research\\test_generic_e2e.py",
        "--ignore=tests\\local_quant_research\\test_turtle_e2e.py",
        "--ignore=tests\\local_quant_research\\test_local_research_v2_e2e.py",
    ]
    assert unit["timeoutSeconds"] == 300
    assert e2e["command"] == [
        ".\\.venv\\Scripts\\python.exe",
        "-m",
        "pytest",
        "tests\\local_quant_research\\test_generic_e2e.py",
        "tests\\local_quant_research\\test_turtle_e2e.py",
        "tests\\local_quant_research\\test_local_research_v2_e2e.py",
    ]
    required_paths = {
        ".agents/skills/run-local-quant-research/**",
        ".claude/skills/run-local-quant-research",
        "scripts/research/market_data/**",
        "scripts/research/local_quant_research/**",
        "scripts/research/analysis_data/**",
        "scripts/research/quant_analysis/**",
        "joinquant/strategies/strategy-003/research/**",
        "tests/local_quant_research/**",
        "tests/quant_analysis/**",
    }
    assert required_paths.issubset(unit["paths"])
    assert required_paths.issubset(e2e["paths"])
    assert {
        ".agents/skills/run-local-quant-research/**",
        ".claude/skills/run-local-quant-research",
    }.issubset(layout["paths"])
    assert {
        ".agents/skills/run-local-quant-research/**",
        ".claude/skills/run-local-quant-research",
    }.issubset(layout["inputs"])
    assert unit["checkParallel"] is True
    assert e2e["checkParallel"] is False
    assert all(not item.startswith(".local/") for item in unit["inputs"])
    assert all(not item.startswith(".local/") for item in e2e["inputs"])


def test_full_verify_checkout_downloads_git_lfs_objects(repo_root: Path) -> None:
    workflow = (
        repo_root / ".github" / "workflows" / "full-verify.yml"
    ).read_text(encoding="utf-8")

    assert "uses: actions/checkout@v4\n        with:\n          lfs: true" in workflow
