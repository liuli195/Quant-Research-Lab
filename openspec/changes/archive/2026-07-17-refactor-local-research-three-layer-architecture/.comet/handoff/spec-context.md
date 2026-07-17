# Comet Spec Context

- Change: refactor-local-research-three-layer-architecture
- Phase: design
- Mode: beta
- Context hash: 9019bbd9b4e6090d0e7cd8bac63f5d774014ef46fe382907bc9acf1812428313

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This beta context pack verbatim-projects spec files and references supporting artifacts by hash, not an agent-authored summary.

## Source References

- Source: openspec/changes/refactor-local-research-three-layer-architecture/proposal.md
- SHA256: d1572168930634731ac7d0794d728c6675718b33b17fb223aab4a484e2d9a3f1
- Source: openspec/changes/refactor-local-research-three-layer-architecture/design.md
- SHA256: b30b85223a9d9b6445dd8896f5af5d99390818df3d55797feceee5a48386061e
- Source: openspec/changes/refactor-local-research-three-layer-architecture/tasks.md
- SHA256: fe435a57fee8a18c82692445b58e22cc2dfe4027f6fb7e26c90048aa020ada95
- Source: openspec/changes/refactor-local-research-three-layer-architecture/specs/local-quant-research-runtime/spec.md
- SHA256: 4943b07f8f5449c33d44f9625cb5280e555514232b392daac2be0a0edc490bae
- Source: openspec/changes/refactor-local-research-three-layer-architecture/specs/local-research-archive-promotion/spec.md
- SHA256: 5e43587054511b5a1f1b0093735b2744475aa3879c3c3a8522ba88d402934c30
- Source: openspec/changes/refactor-local-research-three-layer-architecture/specs/local-research-result-package/spec.md
- SHA256: 3454fb5e60c70901b99b11692441e86127c0838e780b3de95cc6e03f52eb9fb7

## Acceptance Projection

## openspec/changes/refactor-local-research-three-layer-architecture/specs/local-quant-research-runtime/spec.md

- Source: openspec/changes/refactor-local-research-three-layer-architecture/specs/local-quant-research-runtime/spec.md
- Lines: 1-71
- SHA256: 4943b07f8f5449c33d44f9625cb5280e555514232b392daac2be0a0edc490bae

```md
## ADDED Requirements

### Requirement: 本地研究必须使用三层单向架构
系统 MUST 将本地研究组织为 Strategy Module、Skill 通用能力层和 vectorbt 执行底层，并且依赖只能从 Strategy Module 经共享 contracts 进入 Skill 能力和执行底层。具体策略不得导入 vectorbt 上下文、订单枚举、记录结构或原始 Portfolio；共享能力不得导入具体策略的私有实现。

#### Scenario: 加载海龟策略
- **WHEN** 共享入口加载海龟研究项目
- **THEN** 它只通过 `turtle_etf.strategy:MODULE` 使用策略，并通过共享执行 Interface 调用 vectorbt，不直接导入海龟私有文件

#### Scenario: 第二个策略复用共享能力
- **WHEN** 仓库提供一个实现同一 Strategy Module Interface 的最小测试策略
- **THEN** 共享 CLI、性能门禁、结果包和停止状态无需修改即可运行该策略

### Requirement: vectorbt 必须成为唯一账户账本
系统 MUST 使用 vectorbt `Portfolio.from_order_func()` 处理即时和延迟场景的订单接受、拒绝、部分成交、费用、共享现金、持仓和组合估值。策略只保存决策所需的单位、止损、冻结计划、原因和归因轨迹，不得维护第二套成交、现金、持仓、费用或净值账本。

#### Scenario: 即时执行
- **WHEN** 策略在同一交易日生成并执行目标订单
- **THEN** 实际成交、费用、现金、持仓和净值全部来自单一 vectorbt Portfolio

#### Scenario: 延迟执行
- **WHEN** 场景声明正数 `additional_delay_days`
- **THEN** 策略冻结原计划并在执行日复核可交易性和机械约束，但实际账户变化仍由第二个 `from_order_func()` 程序完成

#### Scenario: vectorbt 拒绝订单
- **WHEN** vectorbt 因现金、持仓或订单约束拒绝一笔请求
- **THEN** 系统把拒绝状态和策略原因保存为执行事实，不把正常拒单误报为框架失败

### Requirement: 每个策略必须只有一个公开 Strategy Module
每个本地研究策略 MUST 暴露一个版本化公开 Strategy Module，负责配置校验、输入准备、订单程序和策略结果扩展。策略 MAY 使用私有实现文件，但外部调用和测试不得把这些私有文件当作稳定 Interface。

#### Scenario: 策略内部重组
- **WHEN** 策略维护者在不改变公开 Module 和行为的前提下拆分或合并私有 Numba 内核文件
- **THEN** 共享层、其他策略和 Interface 级测试无需修改

#### Scenario: 禁止动态回调
- **WHEN** Strategy Module 根据场景参数构造订单程序
- **THEN** 它使用模块级固定 Numba 函数和稳定数组类型，不为每个配置动态创建闭包或 lambda

### Requirement: 所有项目必须通过固定共享入口运行
系统 MUST 从项目 `.venv` 调用共享本地研究 CLI。项目配置 MUST 声明仓库内 strategy root、module 和 symbol，不得声明策略专属 project entry、任意命令、系统 Python 或隐式依赖安装。

#### Scenario: 运行合法项目配置
- **WHEN** 配置声明有效的仓库内 Strategy Module、单一行情快照和一个场景
- **THEN** runner 在清理后的子进程中调用固定共享 CLI 并冻结全部声明输入

#### Scenario: 配置尝试执行任意命令
- **WHEN** 配置包含旧 `command`、策略专属 `project_entry`、仓库外模块路径或安装命令
- **THEN** 系统在启动项目进程前拒绝配置并返回 `evidence_insufficient`

### Requirement: 单场景停止状态必须保持固定
共享运行 MUST 每次只接受一个场景，并且只返回 `complete`、`evidence_insufficient` 或 `failed`。完整运行 MUST 返回 `next_action=return_to_caller`；Skill 不得循环多个场景、解释策略字段或自动给出研究推荐。

#### Scenario: 输入证据缺失
- **WHEN** 策略身份、配置、行情快照、范围或必需字段缺失
- **THEN** 系统返回 `evidence_insufficient`，不猜测替代输入且不执行策略

#### Scenario: 执行或证据失败
- **WHEN** vectorbt 执行异常、输出缺失、摘要冲突、临时清理失败或性能超限
- **THEN** 系统返回 `failed` 并保留紧凑失败证据，不发布完整运行

### Requirement: 重构必须保持结果一致且性能不实质退化
系统 MUST 在固定环境比较重构前后的真实规模、扩展资产和延迟场景。Schema、行数、成交、费用、现金、持仓、净值和逻辑摘要 MUST 完全一致；冷启动、预热、峰值内存和结果体积的中位数变化不得超过 5% 测量噪声，并继续满足冷、热各 180 秒绝对门禁。

#### Scenario: 真实规模性能回归
- **WHEN** 在相同机器和输入上运行 3,432 日 × 11 ETF 主场景，使用三个冷启动新进程和五次预热采样
- **THEN** 结果证据零差异，时间、内存和结果体积均不超过基线 5% 噪声带

#### Scenario: 扩展和延迟场景
- **WHEN** 运行 3,432 日 × 17 ETF 场景以及 `additional_delay_days=1` 场景
- **THEN** 两者分别通过相同的正确性、确定性和性能门禁

```

## openspec/changes/refactor-local-research-three-layer-architecture/specs/local-research-archive-promotion/spec.md

- Source: openspec/changes/refactor-local-research-three-layer-architecture/specs/local-research-archive-promotion/spec.md
- Lines: 1-60
- SHA256: 5e43587054511b5a1f1b0093735b2744475aa3879c3c3a8522ba88d402934c30

```md
## ADDED Requirements

### Requirement: 完成本地研究必须能够显式晋升为策略档案
共享 CLI MUST 提供独立晋升动作，只接受已通过 `complete` 门禁且清单验证成功的 `.local/quant-research/<strategy_id>/<run_id>/`。目标 MUST 为 `joinquant/strategies/<strategy_id>/research/archives/<analysis_id>/`，并且 `analysis_id` 必须由调用者显式提供。

#### Scenario: 晋升一个完整运行
- **WHEN** 调用者指定合法 strategy_id、run_id 和尚未使用的 analysis_id
- **THEN** 系统校验源运行并把完整档案原子发布到对应策略目录

#### Scenario: 尝试晋升失败运行
- **WHEN** 源运行缺少 `complete` 状态、必需文件或有效摘要
- **THEN** 系统拒绝晋升，不创建目标档案且不修改源运行

### Requirement: 策略档案必须对分析自包含
每个档案 MUST 包含不可变清单、完整策略源码、运行配置、代码身份、四张核心事实表、全部声明策略扩展、性能证据、环境证据、行情快照身份和机械执行报告。调用者 MUST 能在不访问 `.local` 的情况下查询结果、比较参数、检查策略代码和重新生成机械执行报告。

#### Scenario: 删除运行缓存后复盘
- **WHEN** 一个已晋升档案对应的 `.local` 运行缓存不可用
- **THEN** analysis_data 仍能从档案清单打开全部已声明事实和策略扩展并生成分析视图

#### Scenario: 检查执行来源
- **WHEN** 调用者检查档案的可重放证据
- **THEN** 清单提供 Strategy Module、共享运行时、Git、第三方依赖、配置和 snapshot_id 的版本及摘要

#### Scenario: 机械执行报告保持事实边界
- **WHEN** 共享流程为完成包生成 `report/` 内容
- **THEN** 报告只包含可从包内复核的运行身份、参数、数据范围、成交与持仓统计、净值摘要、性能和完整性事实，不生成策略推荐、稳健性结论或实盘准入判断

### Requirement: 晋升不得重新计算研究结果
晋升 MUST 只复制并校验完成运行中已经固化的字节。它不得加载 Strategy Module、调用 vectorbt、重新生成核心事实或扩展、重新序列化 Parquet、重新计算报告指标，且不得计入回测冷启动或预热耗时。

#### Scenario: 证明晋升没有执行引擎
- **WHEN** 测试把策略和 vectorbt 调用替换为调用即失败后执行晋升
- **THEN** 晋升仍成功，并且源、目标数据文件逐文件 SHA256 完全一致

#### Scenario: 共享行情保持单份
- **WHEN** 晋升一个使用共享行情快照的运行
- **THEN** 档案只复制快照身份、来源、范围、字段、价格口径和摘要，不复制共享 market-data.parquet

### Requirement: 晋升必须不可变、幂等且冲突安全
晋升 MUST 先在目标同级暂存目录复制全部文件并复核摘要，再以原子目录替换发布。相同 analysis_id 和相同内容 MUST 返回复用；相同 analysis_id 但内容不同 MUST 失败且不得覆盖、合并或生成隐式后缀。

#### Scenario: 重复晋升相同内容
- **WHEN** 调用者以同一 analysis_id 再次晋升同一个完整运行
- **THEN** 系统重新验证既有档案并返回幂等复用，不改写任何文件

#### Scenario: analysis_id 内容冲突
- **WHEN** 目标 analysis_id 已绑定不同 run_id 或不同文件摘要
- **THEN** 系统返回失败并保持既有档案和源运行不变

#### Scenario: 发布中途失败
- **WHEN** 复制、摘要验证或原子发布任一步骤失败
- **THEN** 系统删除暂存目录，不留下半成品目标，也不影响其他档案

### Requirement: 本地研究档案必须与聚宽正式运行隔离
本地研究档案 MUST 只写入 `research/archives/`，其清单和报告 MUST 明确标记本地探索性 vectorbt 来源。正式聚宽回测和模拟交易 MUST 继续分别写入 `backtests/` 和 `simulations/`，分析层不得因目录位于同一策略下而混淆运行身份。

#### Scenario: 列出策略下的研究和正式回测
- **WHEN** 查询入口同时发现 `research/archives/` 和 `backtests/`
- **THEN** 它分别返回本地研究与聚宽正式运行类型，并保留各自 backend 和来源身份

```

## openspec/changes/refactor-local-research-three-layer-architecture/specs/local-research-result-package/spec.md

- Source: openspec/changes/refactor-local-research-three-layer-architecture/specs/local-research-result-package/spec.md
- Lines: 1-52
- SHA256: 3454fb5e60c70901b99b11692441e86127c0838e780b3de95cc6e03f52eb9fb7

```md
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

```

Full source files remain canonical. If a required heading or scenario is missing here, regenerate the handoff or read the source spec directly. Supporting files (proposal, design, tasks) are referenced by hash only.