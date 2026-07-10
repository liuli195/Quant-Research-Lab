from __future__ import annotations

import hashlib
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
        repo_root
        / ".agents"
        / "skills"
        / "joinquant-archive-sync"
        / "SKILL.md"
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
