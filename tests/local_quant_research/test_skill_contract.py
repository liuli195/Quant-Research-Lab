from __future__ import annotations

from pathlib import Path


PUBLIC_COMMAND = (
    ".\\.venv\\Scripts\\python.exe "
    "scripts\\research\\local_quant_research\\cli.py run --config <path>"
)


def _skill_text(repo_root: Path) -> str:
    return (
        repo_root / ".agents" / "skills" / "run-local-quant-research" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_local_research_skill_is_thin_and_strategy_agnostic(
    repo_root: Path,
) -> None:
    text = _skill_text(repo_root)

    assert text.startswith(
        "---\nname: run-local-quant-research\ndescription: Use when "
    )
    assert text.count(PUBLIC_COMMAND) == 1
    assert all(
        status in text for status in ("complete", "evidence_insufficient", "failed")
    )
    for required in (
        "snapshot_id",
        "market-data.parquet",
        "DuckDB（嵌入式分析数据库）",
        "标准分析",
        "完整报告",
        "human_confirmation_required",
        "必需输出",
        "正式回测",
        "JoinQuant（聚宽）",
        "Cookie（浏览器凭证）",
        "Token（访问令牌）",
    ):
        assert required in text
    for forbidden in ("海龟", "turtle", "55日", "0.5N", "strategy-003", "510300"):
        assert forbidden not in text


def test_local_research_skill_has_one_fixed_orchestration_order(
    repo_root: Path,
) -> None:
    text = _skill_text(repo_root)
    stages = [
        "校验行情快照",
        "校验项目配置",
        "运行项目入口",
        "校验必需输出",
        "固化运行证据",
        "停止并等待人工确认",
    ]

    positions = [text.index(stage) for stage in stages]
    assert positions == sorted(positions)
    assert "执行前缺少身份、快照、范围或声明输入" in text
    assert "既有证据被篡改或摘要不一致" in text
    assert "运行目录之外" in text
    assert "不修改不可变运行" in text
    assert "Vibe-Trading（AI 研究助理）" in text
    assert "不可用" in text


def test_local_research_skill_ui_metadata_matches_public_entry(
    repo_root: Path,
) -> None:
    metadata = (
        repo_root
        / ".agents"
        / "skills"
        / "run-local-quant-research"
        / "agents"
        / "openai.yaml"
    ).read_text(encoding="utf-8")

    assert 'display_name: "本地量化研究流程"' in metadata
    assert "$run-local-quant-research" in metadata
