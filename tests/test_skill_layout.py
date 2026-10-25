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


def test_standard_analysis_skill_is_the_only_public_entry(repo_root: Path) -> None:
    source = repo_root / ".agents" / "skills" / "analyze-quant-robustness"
    claude = repo_root / ".claude" / "skills" / "analyze-quant-robustness"

    assert (source / "SKILL.md").is_file()
    assert (source / "agents" / "openai.yaml").is_file()
    assert (source / "scripts" / "analyze_quant_robustness.py").is_file()
    assert (source / "scripts" / "quant_analysis" / "__init__.py").is_file()
    assert claude.is_symlink()
    assert claude.readlink() == Path("../../.agents/skills/analyze-quant-robustness")
    assert claude.resolve() == source.resolve()
    skill = (source / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: analyze-quant-robustness\ndescription: Use when ")
    assert "scripts\\analyze_quant_robustness.py" in skill
    assert "`run`" in skill
    assert "`report`" in skill
    assert "--package" in skill
    assert "--analysis-plan" in skill
    assert "--benchmark-manifest" in skill
    assert "--source-registry" not in skill
    assert not (
        source / "scripts" / "quant_analysis" / "source_registry.py"
    ).exists()
    assert not (
        source / "scripts" / "quant_analysis" / "schemas" / "source-registry.schema.json"
    ).exists()
    assert "-m scripts.research.quant_analysis" not in skill
    assert "不得启动、提交、同步或修改" in skill


def test_build_and_verify_covers_joinquant_docs_sync(repo_root: Path) -> None:
    config = json.loads((repo_root / ".build-and-verify/config.json").read_text(encoding="utf-8"))
    checks = {check["id"]: check for check in config["verify"]["checks"]}
    for identity in ("verify.docs-unit", "verify.docs-real"):
        check = checks[identity]
        assert "tests/joinquant_docs_sync/test_cli.py" in check["command"]
        assert ".agents/skills/joinquant-docs-sync/**" in check["paths"]
        assert ".claude/skills/joinquant-docs-sync" in check["paths"]
        assert "tests/joinquant_docs_sync/**" in check["inputs"]
    assert "verify.docs-real" in config["verify"]["realChecks"]
    assert "verify.docs-unit" not in config["verify"]["realChecks"]


def test_build_and_verify_covers_local_quant_research_without_local_data(
    repo_root: Path,
) -> None:
    config = json.loads(
        (repo_root / ".build-and-verify/config.json").read_text(encoding="utf-8")
    )
    checks = {check["id"]: check for check in config["verify"]["checks"]}
    real = config["verify"]["realChecks"]
    assert config["version"] == 2
    assert config["build"]["checks"] == []
    assert config["verify"]["maxParallel"] == 4
    assert config["verify"]["fullBudgetSeconds"] == 60
    assert config["verify"]["enforceLocalBudget"] is True
    assert real and len(real) == len(set(real))
    assert set(real) < set(checks)
    assert "verify.local-quant-research-jit" not in checks
    assert all(check["paths"] and check["inputs"] for check in checks.values())
    for identity, check in checks.items():
        assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in check["command"]
        assert ".\\.venv\\Scripts\\python.exe -m pytest" in check["command"]
        assert "--basetemp=.local/pytest/" + identity in check["command"]
        assert ('-m real' if identity in real else '-m "not real"') in check["command"]
    worktree = checks["verify.worktree-setup"]
    assert worktree["pytestXdistWorkers"] == 2
    assert "-p xdist.plugin" in worktree["command"]
    assert all("pytestXdistWorkers" not in check for identity, check in checks.items()
               if identity != "verify.worktree-setup")
    numeric = checks["verify.quant-calculation"]
    assert "NUMBA_DISABLE_JIT=1" in numeric["command"]
    assert "test_turtle_vectorbt_callbacks.py" in numeric["command"]
    assert "verify.quant-equivalence" not in checks
    assert "verify.skill-layout-real" not in checks
    assert "test_local_research_equivalence.py" in checks["verify.quant-unit"]["command"]
    assert all(not path.startswith(".local/") for check in checks.values() for path in check["inputs"])


def test_full_verify_checkout_downloads_git_lfs_objects(repo_root: Path) -> None:
    workflow = (
        repo_root / ".github" / "workflows" / "full-verify.yml"
    ).read_text(encoding="utf-8")

    assert "uses: actions/checkout@v4\n        with:\n          lfs: true" in workflow
    assert '$buildAndVerify.mode -eq "dev"' in workflow
    assert "$buildAndVerify.sourceCommit" in workflow
    assert '$buildAndVerify.mode -eq "release"' in workflow
    assert "$buildAndVerify.packageVersion" in workflow
    assert 'PYTHONUTF8: "1"' in workflow
