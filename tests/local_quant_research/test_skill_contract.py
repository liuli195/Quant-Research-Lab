from __future__ import annotations

from pathlib import Path

from scripts.research.local_quant_research.cli import _parser


PUBLIC_COMMAND = (
    ".\\.venv\\Scripts\\python.exe "
    "scripts\\research\\local_quant_research\\cli.py run --config <path>"
)
PROMOTE_COMMAND = (
    ".\\.venv\\Scripts\\python.exe "
    "scripts\\research\\local_quant_research\\cli.py promote `\n"
    "  --strategy-id <strategy_id> `\n"
    "  --run-id <run_id> `\n"
    "  --analysis-id <analysis_id>"
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
    assert text.count(PROMOTE_COMMAND) == 1
    assert all(
        status in text for status in ("complete", "evidence_insufficient", "failed")
    )
    for required in (
        "snapshot_id",
        "market-data.parquet",
        "DuckDB（嵌入式分析数据库）",
        "单场景",
        "完整报告",
        "return_to_caller",
        "archive-ready（可归档）",
        "输出固定为唯一 archive-ready（可归档）结果包",
        "不由配置声明",
        "正式回测",
        "JoinQuant（聚宽）",
        "Cookie（浏览器凭证）",
        "Token（访问令牌）",
    ):
        assert required in text
    for forbidden in ("海龟", "turtle", "55日", "0.5N", "strategy-003", "510300"):
        assert forbidden not in text
    for forbidden in (
        "project_entry",
        "project-entry",
        "项目入口",
        "任意命令",
        "required_outputs",
        "output_root",
        "stop_states",
        "唯一必需输出",
        "必需输出声明",
    ):
        assert forbidden not in text


def test_local_research_skill_has_one_fixed_orchestration_order(
    repo_root: Path,
) -> None:
    text = _skill_text(repo_root)
    stages = [
        "校验行情快照",
        "校验单场景配置",
        "运行共享场景",
        "校验单场景结果",
        "固化运行证据",
        "返回调用者",
    ]

    positions = [text.index(stage) for stage in stages]
    assert positions == sorted(positions)
    assert "执行前缺少身份、快照、范围或声明输入" in text
    assert "既有证据被篡改或摘要不一致" in text
    assert "复数场景由主 agent（代理）多次调用" in text
    assert "不在 Skill 内聚合" in text
    assert "Vibe-Trading（AI 研究助理）" in text


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


def test_private_execute_protocol_is_absent_from_public_cli_help() -> None:
    help_text = _parser().format_help()

    assert "run" in help_text
    assert "promote" in help_text
    assert "_execute" not in help_text
