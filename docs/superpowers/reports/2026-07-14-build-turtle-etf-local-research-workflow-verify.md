# 海龟 ETF 本地研究流程验证报告

- 验证日期：2026-07-18
- Change（变更）：`build-turtle-etf-local-research-workflow`
- 验证提交：`3fc430611dec743fa880cc73ac04db30e2868847`
- 分支状态：`main` 与 `origin/main` 一致，既有 PR（拉取请求）已处理
- 工程验证结论：通过，有警告
- 当前停止状态：`archive_confirmation_required`

## 结论

该 change（变更）的工程实现已完成并通过完整验证，可以进入归档前人工确认：

- 34 项任务全部完成。验证阶段发现 task 2.2、3.3 及其 proposal/design（提案/设计）仍引用旧 `local-backtest/1` 和 `Portfolio.from_orders()`；现已最小修正为当前实现使用的 `local-research-package/2` 和统一 `Portfolio.from_order_func()`。
- 30 个 Requirement（要求）、109 个 Scenario（场景）已逐项核对，没有未处理的 CRITICAL（阻断）实现缺口。
- Build and Verify（构建与验证）完整模式真实运行，692 项 Pytest（测试框架）测试全部通过，OpenSpec（开放规格）8 项严格校验全部通过。
- 所有测试均在本机进程及其子进程内完成，没有联网调用外部系统。
- 工程实现通过不等于策略研究结论通过。既有真实 11 ETF 基线仍建议停止并等待人工确认，不进入聚宽正式复核。

## 完整验证证据

运行命令：

```powershell
.\.venv\Scripts\python.exe .build-and-verify\runtime\build_and_verify.py verify --project . --full
```

结果：

| 检查 | 结果 | 耗时 |
| --- | ---: | ---: |
| Skill layout（技能布局） | 9 passed | 15.53 秒 |
| Docs sync（文档同步） | 16 passed | 13.44 秒 |
| Archive（归档能力） | 56 passed | 14.78 秒 |
| Browser research（浏览器研究） | 49 passed | 12.75 秒 |
| Query（查询） | 8 passed | 12.38 秒 |
| Scheduler unit（调度单元） | 22 passed | 14.62 秒 |
| Sync pipeline（同步流水线） | 70 passed | 17.84 秒 |
| Self test（自测） | 3 passed | 11.83 秒 |
| Local research unit（本地研究单元） | 451 passed | 95.56 秒 |
| Local research E2E（本地研究端到端） | 8 passed | 126.66 秒 |
| OpenSpec（开放规格） | 8 passed，0 failed | 2.55 秒 |

- 完整命令墙钟时间：241.5 秒。
- `full-not-run=false`，最终 `status=passed`。
- 仅有 4 条 vectorbt（向量化回测框架）依赖触发的 Pandas（数据处理库）大小写日期单位弃用警告，不影响结果。
- 文档纠偏后重新运行构建：`checked: build.placeholder`，`status: passed`。
- 文档纠偏后重新运行 `openspec validate --all --strict --no-interactive`：8 passed，0 failed。

## OpenSpec 对齐计分

| 维度 | 结论 |
| --- | --- |
| Completeness（完整性） | 34/34；两项过时任务语义已同步到当前实现 |
| Correctness（正确性） | 30 个要求、109 个场景均有实现或有效条件边界；无阻断缺口 |
| Coherence（连贯性） | proposal/design/tasks 与 delta specs（增量规格）及当前代码已统一为 package/2 和单一 vectorbt 账本 |

## 归档合并安全验证

在固定临时目录中真实执行了一次 `openspec archive build-turtle-etf-local-research-workflow --yes` 预览，未修改工作区：

- 归档前已存在的 5 份主规格 SHA256（文件摘要）前后全部一致，包括最新的 `local-quant-research-runtime`、`local-research-result-package` 和 `local-research-archive-promotion`；旧 change 不会覆盖它们。
- 归档只新增 `local-quant-research-workflow`、`standard-strategy-analysis-data`、`turtle-etf-local-research` 3 份互补能力规格，共 30 个要求。
- 复审发现“必须生成新的结果包”与相同 `run_id` 复用既有结果包冲突；已修正为“生成或复用经重新校验的唯一不可变结果包”，与当前幂等实现一致。
- 合并后扫描不到 `Portfolio.from_orders()`、`object.kind=local_backtest` 或 `source.kind=local_vectorbt` 等过时契约。
- 合并后的 OpenSpec 严格校验为 10 passed、0 failed；临时预览目录已清理。

关键实现证据：

- 本地结果写出 `local-research-package/2`：`scripts/research/local_quant_research/result_package.py`。
- 即时和补充执行统一经过 `Portfolio.from_order_func()`：`scripts/research/local_quant_research/vectorbt_runtime.py`。
- 延迟研究从主运行冻结动作生成后续订单程序：`joinquant/strategies/strategy-003/research/turtle_etf/_delayed.py`。
- 历史 `local-backtest/1` 仅保留只读兼容：`scripts/research/analysis_data/manifest.py`。

## 真实基线证据复核

本轮没有重复运行研究场景；只复核已固化的单次真实 11 ETF 基线及其报告：

- 范围：11 只 ETF、6 个资产组、55/20/20、4/6/12、全量仓位再分配。
- 冷启动 26.00 秒、预热 2.40 秒，结果摘要一致，均通过 180 秒门禁。
- 累计收益 120.07%，最大回撤 -34.66%，Calmar（卡玛比率）0.172。
- 没有运行旧方案对照、17 ETF 扩展、7 个参数场景或稳健性矩阵。
- 研究结论仍为“不推荐按当前规则进入聚宽复核；等待人工确认”。

权威研究报告：`docs/research/2026-07-16-turtle-full-position-redistribution-baseline-report.md`。

## 剩余警告

1. 同一 ETF 同日退出覆盖入场或加仓已有生产实现，但缺少针对该组合条件的直接回归测试；现有 E2E（端到端）和完整回归均通过。
2. 行情中心已拒绝非 JoinQuant（聚宽）、非 ETF（交易型开放式指数基金）和非日线输入，但这些拒绝分支缺少直接单元断言。

以上均不改变当前实现通过结论，属于测试深度改进项。

## 工作区边界

- 本轮没有修改生产代码或测试。
- `.superpowers/sdd/task-5-report.md` 是无引用的临时实施报告，当前删除属于本 change 收尾。
- strategy-001/002 的 manifest（清单）、索引和 simulation-001（模拟交易）归档文件属于并行工作，未被修改、清理、暂存或纳入本报告结论。

## 归档建议

验证阶段可以结束。归档安全预览已证明旧 change 不会覆盖最新主规格，合并后的规格与当前实现一致。下一步停在 Archive（归档）人工确认点。
