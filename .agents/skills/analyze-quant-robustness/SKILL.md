---
name: analyze-quant-robustness
description: Use when 用户或 Agent 需要对已登记的本地研究、聚宽回测或聚宽模拟交易快照运行标准量化分析、深度归因或稳健性复核。
---

# 标准量化分析流程

本 Skill（技能）是标准量化分析的唯一公开入口。它只读取显式登记且已归档的来源，交付共同绩效、风险、深度归因证据和稳健性报告；实现包位于本 Skill 的 `scripts\\quant_analysis`（量化分析）目录，不能以仓库旧模块作为替代入口。

## 输入与停止条件

- 要求一个仓库内、版本为 `standard-analysis-source-registry/1` 的来源登记 JSON（结构化数据）。它必须显式列出每个来源、清单摘要；聚宽模拟交易还必须列出快照标识。
- 登记、清单摘要、快照或共同基准缺失、漂移或无法验证时停止并报告具体证据状态；不猜测 `latest`（最新）对象，不扫描目录补选来源。
- `evidence_insufficient`（证据不足）是分析结论，不是启动上游补数的理由。

## 固定调用

从仓库根目录仅使用项目 `.venv`（虚拟环境）执行本 Skill 的 `run`（运行）命令：

```powershell
& .\.venv\Scripts\python.exe .agents\skills\analyze-quant-robustness\scripts\analyze_quant_robustness.py run `
  --repository . `
  --source-registry <仓库相对来源登记.json>
```

读取输出的 `analysis_id`（分析标识）后，用本 Skill 的 `report`（报告）命令生成交付：

```powershell
& .\.venv\Scripts\python.exe .agents\skills\analyze-quant-robustness\scripts\analyze_quant_robustness.py report `
  --repository . `
  --workspace .local\standard-strategy-analysis\<analysis_id>
```

返回 `deterministic-analysis.json`（确定性分析）、`standard-strategy-analysis-report.md`（标准分析报告）和 `recommendation.json`（建议），并分别说明通过、失败与证据不足数量。

## 只读边界

不得启动、提交、同步或修改本地研究、聚宽回测、聚宽模拟交易及其归档；不得联网、认证、读取凭证或调用 `run-local-quant-research`（本地量化研究）来代替本流程。正式回测和模拟交易仍只在 JoinQuant（聚宽）云端运行，是否进入该阶段必须由用户另行确认。
