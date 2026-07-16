## ADDED Requirements

### Requirement: 共享结果包必须定义后端中立的核心事实
系统 MUST 用版本化 Schema 定义结果、资金、持仓和订单四张核心事实表，并允许策略通过版本化扩展增加归因等证据。核心 writer 不得导入具体策略动作码，策略扩展不得改变核心表的字段含义。

#### Scenario: 海龟策略生成结果包
- **WHEN** 海龟执行完成并提供策略轨迹
- **THEN** 共享 writer 从 ExecutionLedger 生成四张核心表，并把海龟归因作为独立扩展写入清单

#### Scenario: 最小策略不提供归因
- **WHEN** 第二个测试策略只提供核心执行账本而没有策略扩展
- **THEN** 共享 writer 仍生成合法完整结果包，并明确声明没有对应扩展

### Requirement: 账本视图和事实数据只能物化一次
ExecutionLedger MUST 对 orders、assets、cash 和 value 使用只读惰性缓存；共享 writer MUST 直接消费这些视图并只执行一次公共事实转换和一次最终 Parquet 固化。运行路径不得通过 JSON、字典或 Arrow 往返重新构造账本，也不得在多层重复复制完整矩阵。

#### Scenario: 多个消费者读取净值
- **WHEN** 性能摘要、结果 writer 和策略归因都需要组合净值
- **THEN** 它们复用同一个缓存视图，不分别调用 vectorbt 生成或复制完整净值数组

#### Scenario: 策略不需要某类轨迹
- **WHEN** Strategy Module 未声明某个策略审计字段
- **THEN** 执行底层不为该字段分配行数乘证券数的稠密矩阵

### Requirement: 结果包必须可验证并原子发布
系统 MUST 为每个数据集记录路径、状态、行数、时间范围、Schema 版本和 SHA256，并在暂存目录完成写入、回读、跨表校验和逻辑摘要后原子发布。任何校验或发布失败 MUST 清理暂存目录且不得留下可被识别为完整的结果。

#### Scenario: 成功固化结果包
- **WHEN** 四张核心表和全部声明扩展通过 Schema、摘要和跨表勾稽
- **THEN** 系统原子发布数据文件和清单，并把运行标记为 `complete`

#### Scenario: 回读摘要不一致
- **WHEN** Parquet 回读后的逻辑摘要与内存事实不同
- **THEN** 系统返回 `failed`，删除暂存结果且不覆盖既有完整运行

### Requirement: 共享分析必须读取本地和聚宽结果
analysis_data MUST 通过后端中立清单读取新的本地结果包和既有聚宽归档，并为相同概念提供一致查询视图。任何来源差异、缺失数据集和公式版本 MUST 保留显式证据，不得伪造成相同来源或静默补全。

#### Scenario: 比较本地研究和聚宽回测
- **WHEN** 分析流程同时打开一个本地 vectorbt 结果和一个聚宽正式回测
- **THEN** 它通过统一视图查询共同事实，同时保留 backend、来源身份和数据集状态

#### Scenario: 使用 vectorbt 统计交叉校验
- **WHEN** 分析流程调用 vectorbt returns 或 stats 复核收益指标
- **THEN** 结果被标记为交叉校验，不静默替换现有 Alpha、Information Ratio、CVaR 或其他公式版本

### Requirement: 结果性能证据必须覆盖完整固化路径
冷启动和预热计时 MUST 覆盖策略执行、核心事实、策略扩展、Parquet 写入、回读校验和逻辑摘要，并额外提供各阶段耗时。重构不得通过把工作移出计时范围来满足门禁。

#### Scenario: 抽取共享 writer
- **WHEN** 原策略 writer 迁移到共享结果包
- **THEN** 重构前后基准都计入相同固化步骤，并能单独比较执行、事实转换、扩展和文件阶段
