# local-research-archive-promotion Specification

## Purpose
TBD - created by archiving change refactor-local-research-three-layer-architecture. Update Purpose after archive.
## Requirements
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

晋升的安全边界 MUST 是本机同一用户控制的可信研究工作区：开始前用标准路径扫描拒绝 symlink、junction、hardlink 和非普通文件，使用 `shutil.copy2` 复制，复制后逐文件复核长度与 SHA256，并用 `os.replace` 原子发布。它不承诺防御同一用户在扫描后敌对替换源树，不得为此维护文件描述符、inode 或平台专属竞态状态机。

#### Scenario: 重复晋升相同内容
- **WHEN** 调用者以同一 analysis_id 再次晋升同一个完整运行
- **THEN** 系统重新验证既有档案并返回幂等复用，不改写任何文件

#### Scenario: analysis_id 内容冲突
- **WHEN** 目标 analysis_id 已绑定不同 run_id 或不同文件摘要
- **THEN** 系统返回失败并保持既有档案和源运行不变

#### Scenario: 发布中途失败
- **WHEN** 复制、摘要验证或原子发布任一步骤失败
- **THEN** 系统删除暂存目录，不留下半成品目标，也不影响其他档案

#### Scenario: 源树包含不支持的文件对象
- **WHEN** 晋升前扫描发现符号链接、目录连接、硬链接或非普通文件
- **THEN** 系统在复制前拒绝晋升且不创建完成目标

### Requirement: 本地研究档案必须与聚宽正式运行隔离
本地研究档案 MUST 只写入 `research/archives/`，其清单和报告 MUST 明确标记本地探索性 vectorbt 来源。正式聚宽回测和模拟交易 MUST 继续分别写入 `backtests/` 和 `simulations/`，分析层不得因目录位于同一策略下而混淆运行身份。

#### Scenario: 列出策略下的研究和正式回测
- **WHEN** 查询入口同时发现 `research/archives/` 和 `backtests/`
- **THEN** 它分别返回本地研究与聚宽正式运行类型，并保留各自 backend 和来源身份
