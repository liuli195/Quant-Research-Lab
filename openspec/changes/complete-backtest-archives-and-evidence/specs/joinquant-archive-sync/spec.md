## MODIFIED Requirements

### Requirement: 历史运行同步必须由明确目标驱动
系统 MUST 只同步调用者明确指定的历史回测记录。有效目标 SHALL（应当）为策略页面内可复核的序号或详情链接；缺失目标、含义不明确的目标或 `latest`（最新）选择器 MUST 被拒绝。多个目标只有逐项明确列出后才能同步。补录小于当前最大页面序号的历史回测时，系统 MUST 保持 `latest_backtest_id`（最新回测编号）单调不减。

#### Scenario: 同步一个指定回测
- **WHEN** 用户或 Agent（代理）传入一个有效的策略和回测页面序号
- **THEN** 系统只解析并同步该回测及其所属策略必要元数据，不同步其他历史回测

#### Scenario: 先列出再选择
- **WHEN** Agent 请求可选历史运行列表但未指定同步目标
- **THEN** 系统只返回轻量元数据，不下载代码、结果或日志

#### Scenario: 拒绝隐式全量或最新目标
- **WHEN** 调用未提供目标、使用 `latest` 或只传入策略而未列出历史运行
- **THEN** 系统在任何历史数据下载前返回可操作的目标校验错误

#### Scenario: 补录较早历史回测
- **WHEN** 策略索引的最新回测编号为 115，调用者显式同步页面序号 88
- **THEN** 系统归档页面序号 88，同时保持最新回测编号为 115

## ADDED Requirements

### Requirement: 官方回测摘要必须保留来源和用途边界
系统 MUST 将 `data/official-summary.csv` 作为聚宽回测详情页官方导出源文件的唯一合法路径，`reports/` MUST 只存放人工分析报告。所有回测清单的 `official_summary` 数据集 MUST 明确记录版本化证据，包括详情页下载来源、编码、表头、行数和关联的 Research（研究环境）数据集。系统 MUST 迁移既有官方摘要和清单引用，不得保留 `reports/official-summary.csv`、双路径或旧路径兼容读取。文档 MUST 区分可近似对齐、仅可交叉校验和不可由官方摘要推导的字段，并说明日常分析应读取的权威明细数据集。

#### Scenario: 归档官方摘要下载
- **WHEN** 同步器从指定回测详情页的导出入口取得官方摘要
- **THEN** 系统保存 `data/official-summary.csv`，并在清单证据中记录来源 URL（链接）、导出动作、编码、表头、行数及 `results`、`balances`、`orders` 关联数据集

#### Scenario: 分析当日盈亏
- **WHEN** 调用者需要分析某日盈亏及其交易构成
- **THEN** 文档指引调用者以 Research 的 `balances` 和 `orders` 为明细来源，并仅用官方摘要复核页面展示口径

#### Scenario: 迁移既有官方摘要
- **WHEN** 既有回测仍将官方摘要保存在 `reports/official-summary.csv` 或清单尚未包含版本化来源证据
- **THEN** 系统在保持文件内容摘要不变的前提下移动文件、更新清单路径和证据，并在迁移完成后拒绝任何残留旧路径
