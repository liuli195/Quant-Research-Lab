---
name: analyze-quant-robustness
description: Use when 用户或 Agent 需要对标准结果包运行量化分析、深度归因或稳健性复核，无论结果包由谁产生或存放在哪里。
---

# 标准量化分析流程

本 Skill（技能）是标准量化分析的唯一公开入口。它只读取显式提供的标准结果包，交付绩效、风险、深度归因证据和稳健性报告；实现位于本 Skill 的 `scripts\\quant_analysis`（量化分析）目录。

## 输入与停止条件

- 要求一个或多个完整的标准结果包、一个分析计划和一个独立基准清单。结果包可使用绝对路径或相对路径；生产者、父目录和文件名不参与分析身份或计算。
- 结果包必须自带策略、场景、冻结参数、共同事实和内容摘要。结果包参数必须与分析计划中的对应场景完全一致。
- 结果包、分析计划、基准或摘要缺失、漂移、重复或无法验证时停止并报告具体原因；不猜测 `latest`（最新）对象，也不扫描目录补选结果包。
- `evidence_insufficient`（证据不足）是分析结论，不是启动上游补数的理由。

## 固定调用

从仓库根目录仅使用项目 `.venv`（虚拟环境）执行本 Skill 的 `run`（运行）命令。`--repository`（仓库目录）只确定本地交付目录，不参与分析身份；分析计划与其声明的基线配置作为同一计划包传入：

```powershell
& .\.venv\Scripts\python.exe .agents\skills\analyze-quant-robustness\scripts\analyze_quant_robustness.py run `
  --repository . `
  --package <标准结果包目录> `
  --analysis-plan <分析计划.json> `
  --benchmark-manifest <基准集目录\manifest.json>
```

多个场景重复提供 `--package`（结果包）参数，每个场景一个结果包。

读取输出的 `analysis_id`（分析标识）后，用本 Skill 的 `report`（报告）命令生成交付：

```powershell
& .\.venv\Scripts\python.exe .agents\skills\analyze-quant-robustness\scripts\analyze_quant_robustness.py report `
  --repository . `
  --workspace .local\standard-strategy-analysis\<analysis_id>
```

返回 `deterministic-analysis.json`（确定性分析）、`standard-strategy-analysis-report.md`（标准分析报告）和 `recommendation.json`（建议），并分别说明通过、失败与证据不足数量。

## 只读边界

不得启动、提交、同步或修改任何研究、回测、模拟交易或结果包；不得联网、认证、读取凭证或调用 `run-local-quant-research`（本地量化研究）来代替本流程。正式回测和模拟交易仍只在 JoinQuant（聚宽）云端运行，是否进入该阶段必须由用户另行确认。
