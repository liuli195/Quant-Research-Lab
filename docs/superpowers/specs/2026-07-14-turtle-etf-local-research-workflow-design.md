---
comet_change: build-turtle-etf-local-research-workflow
role: technical-design
canonical_spec: openspec
---

# 海龟 ETF 本地研究流程技术设计

## 1. 设计边界

需求和验收场景以以下 OpenSpec（开放规格）增量规格为唯一事实源：

- `openspec/changes/build-turtle-etf-local-research-workflow/specs/local-quant-research-workflow/spec.md`
- `openspec/changes/build-turtle-etf-local-research-workflow/specs/turtle-etf-local-research/spec.md`

本文只说明实现结构、接口、数据流、错误处理和测试方法，不建立第二份需求规格。

本变更只执行七方案确定性交易路径、完整绩效与归因、完整本地稳健性、挑战比较、报告、推荐和人工确认门禁。正式回测和模拟交易仍只在 JoinQuant（聚宽）云端运行，但聚宽正式复核不属于本变更。`complete` 只表示本地完整研究已执行并等待人工确认，不代表策略通过正式回测、稳健性验收或实盘准入。

## 2. 发布结构与依赖方向

实现采用四层单向依赖：

```text
run-local-quant-research Skill（流程编排）
                ↓
仓库共用脚本（行情中心、运行器、证据、量化分析）
                ↓
strategy-003 项目适配器与海龟纯计算模块
                ↓
标准 Parquet（列式文件）分析数据包
                ↓
.local 共享行情、策略运行、分析与人工决策证据
```

建议目录如下：

```text
.agents/skills/run-local-quant-research/
├── SKILL.md
├── agents/openai.yaml
└── references/operations.md

.claude/skills/run-local-quant-research
└── SymbolicLink -> ../../.agents/skills/run-local-quant-research

scripts/research/
├── market_data/
│   ├── __init__.py
│   ├── contract.py
│   ├── identity.py
│   ├── store.py
│   ├── query.py
│   ├── joinquant_export.py
│   └── cli.py
├── quant_analysis/
│   ├── contracts.py
│   ├── metrics.py
│   ├── benchmarks.py
│   ├── attribution.py
│   ├── robustness.py
│   ├── stress.py
│   ├── cvar.py
│   └── evidence.py
└── local_quant_research/
    ├── __init__.py
    ├── contract.py
    ├── identity.py
    ├── runner.py
    ├── evidence.py
    └── cli.py

joinquant/strategies/strategy-003/
├── default_code.py
├── manifest.json
└── research/
    ├── project.json
    ├── adapter.py
    ├── export-request.json
    ├── turtle/
    │   ├── indicators.py
    │   ├── signals.py
    │   ├── state.py
    │   ├── risk.py
    │   ├── allocation.py
    │   ├── execution.py
    │   └── reporting.py
    └── fixtures/

tests/
├── market_data/
├── local_quant_research/
└── strategy_003/
```

Skill（技能）目录不保存 Python 实现、行情、海龟参数或测试数据，只描述调用顺序和停止条件。共用脚本不导入 `strategy-003`；项目适配器可以调用共用行情和分析接口，但共用层不得反向解释海龟字段。海龟资产分类、挑战配置、压力定义和推荐门槛通过项目配置注入通用分析层。

不建设 Provider（数据提供方）插件框架、后台服务、持久数据库或第四个行情 Skill。以后新增来源或频率时，以新的明确变更扩展共用契约。

## 3. 真实策略身份

实施开始时先在聚宽创建真实策略空壳，再同步为 `joinquant/strategies/strategy-003/`。创建动作只建立远端详情页、本地索引和项目目录的唯一映射，不启动正式回测，不修改 `strategy-001` 或 `strategy-002`。

只有以下身份信息全部可核对后才创建研究项目：

- 本地 `strategy_id = strategy-003`；
- 聚宽详情页稳定身份和名称；
- 本地 `manifest.json` 与策略索引记录；
- 初始 `default_code.py` 文件摘要。

身份缺失、冲突或已被其他项目占用时输出 `evidence_insufficient`，不得预建一个没有真实远端来源的 `strategy-003` 目录。

## 4. 共享日线行情中心

### 4.1 稳定目录

完整行情只写入仓库已忽略的 `.local/`：

```text
.local/market-data/
├── batches/<batch_id>/
│   ├── manifest.json
│   ├── market-data.parquet
│   └── validation.json
└── snapshots/<snapshot_id>.json
```

不建立 `raw/`、`data/`、`reports/` 多层重复目录，不保存长期远端副本，也不在公开仓库生成包含行情值的 receipt（摘要凭据）。`.local/` 是单机私有存储，用户自行负责备份；本变更不把它提升为跨机器正式归档。

### 4.2 批次身份

`market-data.parquet` 是本地唯一行情事实源。聚宽 CSV（逗号分隔文件）只存在于远端传输和本地隐藏暂存目录，转换与验证完成后删除。`batch_id` 由规范化来源身份、结构版本、导出契约和规范化逻辑内容摘要计算，不直接依赖 Parquet 编码字节；清单至少记录：

- schema version（结构版本）；
- `source`、`asset_type`（标的类型）、`frequency=1d`；
- 证券列表和每只证券实际起止日；
- 字段顺序、时区、交易日历和价格口径；
- `snapshot_end_date`（快照截止日）；
- 导出代码摘要、远端与本地传输 CSV 字节摘要、Parquet 字节摘要、行数和规范化内容摘要；
- Parquet 写入器、DuckDB 和结构版本；
- 创建时间、验证状态和验证器版本。

同一来源身份、结构版本和规范化逻辑内容摘要完全相同时复用已有批次。Parquet 字节摘要用于完整性验证，但写入器版本造成的无语义字节变化不能产生两套逻辑事实。新标的或新日期可以追加新批次。相同来源、频率、证券和日期出现不同值或不同价格口径时拒绝导入；首版不实现自动修订、覆盖或 `supersedes`（取代关系）。

### 4.3 快照身份

`snapshot_id` 由规范化快照清单的 SHA256 计算。快照清单只选择已验证批次，并锁定证券、日期、字段、来源和价格口径，不复制行情。旧快照永不追随新批次变化；新增证券必须显式创建新快照。

项目配置通过 `snapshot_id` 声明数据需求。共用脚本先验证批次文件与摘要，再验证快照是否完整覆盖项目请求。缺少快照、证券、字段、区间、来源或口径时停止，不以旧快照、部分资产池或猜测值补齐。

### 4.4 DuckDB 查询层

查询进程使用 `duckdb.connect(':memory:')` 和 `read_parquet` 从快照引用的权威 Parquet 建视图。查询层统一字段顺序、数据类型、空值、日期、证券排序，并把聚宽返回的 `paused` 从数值规范化为布尔值；规范化结果摘要必须与批次清单的逻辑内容摘要一致。

`.local/market-data/` 不保存 `.duckdb` 文件。DuckDB（嵌入式分析数据库）视图可以随时仅凭快照清单和权威 Parquet 重建，不构成第二事实源。

首版只实现日线行情。通用清单预留来源、标的类型、频率和显式字段能力，但分钟线、基本面、财务和因子请求直接报告不支持，不自动降级成日线。

## 5. 聚宽导出链路

共用 `joinquant_export.py` 生成可在聚宽研究环境运行的日线导出入口，海龟项目只提供证券、字段、起止日和价格口径请求。实际研究内核已验证存在以下兼容边界：

- `get_price`、`write_file`、`read_file` 由研究内核注入，不能依赖 `from jqdata import get_price`；
- Pandas（数据处理库）0.23.4 的 `to_csv` 使用 `line_terminator`；
- `paused` 原始类型为 `float64`，本地查询层统一为布尔值；
- 导出调用固定使用 `fq=None` 和 `skip_paused=False`。

海龟首个正式请求包含 11 只 ETF 和以下固定字段：

```text
date, security, open, high, low, close, pre_close,
volume, money, factor, paused, high_limit, low_limit
```

每只 ETF 从自身首个可用完整交易日导出到显式 `snapshot_end_date`。2015-01-01 之前数据只用于指标预热；未满 60 个完整、有效且日期对齐样本的 ETF 可以存在于快照中，但不能新增风险。

传输 CSV 与固化 Parquet 保存未复权实际价格与 `factor`（复权因子），本地研究不生成或使用复权价。供后续聚宽功能校验的策略信号同样显式使用 `fq=None`，含场内基金的策略设置 `use_real_price=False`。报告必须说明该模式的撮合价使用聚宽固定基准日前复权行为，不能把它描述为未复权实际成交价。

远端文件和本地 CSV 都只是传输中转。本地收到文件后先与远端回读字节 SHA256 比较，再在隐藏暂存目录执行固定结构解析、Parquet 转换、规范化内容摘要和 DuckDB 回读复核；全部通过后原子发布批次并删除两端 CSV。任何一项摘要不一致、转换失败或无法确认清理都输出 `failed`。实现不得保存或打印账号、密码、Token（访问令牌）或 Cookie（浏览器凭证）。

## 6. 通用运行器接口

`local_quant_research/cli.py` 提供单一公开命令：

```text
run --config <project-run.json>
```

配置只接受 JSON（结构化清单），至少包含：

- 项目标识和仓库内项目入口；
- 参数数组形式的项目命令，禁止 Shell（命令解释器）字符串；
- `snapshot_id` 和项目需要的证券、日期、字段能力；
- 项目配置路径、代码身份文件和必需输出清单；
- 允许的输出根目录和停止状态。

运行器固定执行：

```text
配置与路径校验
→ 快照、Parquet 和逻辑内容摘要校验
→ 内存 DuckDB 视图同源校验
→ 创建隔离暂存目录
→ 以参数数组调用项目适配器
→ 校验标准分析数据包、完整分析、推荐和报告结构与摘要
→ 固化运行清单和唯一状态
```

项目适配器接收经过验证的快照清单路径、项目配置路径和暂存输出目录，并通过共用 `market_data` Python 接口读取数据。适配器不能写出暂存目录，也不能要求共用运行器解释策略字段。

## 7. 运行身份与原子证据

`run_id` 由以下规范化摘要计算：

```text
snapshot manifest + project config + declared code identity
```

策略成功证据位于：

```text
.local/quant-research/strategy-003/<run_id>/
```

项目先在同一文件系统的隐藏暂存目录生成全部产物。输入、进程、必需文件、JSON 结构和所有摘要通过后，运行器才使用原子目录替换固化 `<run_id>`。失败尝试只保留紧凑 attempt manifest（尝试清单）和诊断，不留下可被误认为完成的运行目录。

同一 `run_id` 已有 `complete` 证据时先重新校验：全部通过则复用，任何输出摘要差异都视为确定性冲突并输出 `failed`。快照、配置或代码变化会生成新 `run_id`。失败重试使用新的 `attempt_id`，不覆盖前次尝试或既有完整运行。

人工决定不写回不可变运行目录，而保存在：

```text
.local/quant-research-decisions/strategy-003/<run_id>/<decision_id>/human-decision.json
```

`decision_id` 绑定 `run_id`、完整报告摘要、`recommendation.json` 摘要和确认内容。新决定可以取代旧决定的后续行动，但不得修改旧决定或研究证据。

## 8. 海龟项目模块

海龟项目保持小型纯计算模块：

| 模块 | 责任 | 主要不变量 |
|---|---|---|
| `indicators.py` | TR（真实波幅）、N 值、突破通道、收益率和协方差输入 | 通道排除当日；不使用复权价 |
| `signals.py` | 55 日入场、20 日退出、0.5N 加仓档位 | 收盘确认；同一 ETF 每日最多一次加仓 |
| `state.py` | 批次、固定信号日 N、理论档位和共同止损状态 | 实际成交才改变状态；共同止损只上移 |
| `risk.py` | U0、资金、流动性、单 ETF、资产组、计划风险和目标波动率 | 现金不为负；所有硬上限不突破 |
| `allocation.py` | A1 等比分配、整手取整和余额补分 | 输入顺序不改变结果；同分按代码升序 |
| `execution.py` | 次日开盘订单、成交夹具、退出和强制减仓 | 不虚构成交；退出优先 |
| `reporting.py` | 项目审计、标准分析数据包和候选配置事实 | 不复制通用指标或稳健性实现；所有输出绑定运行身份和摘要 |

模块之间使用明确记录对象，不读取全局路径或环境变量。项目报告适配只消费确定性结果并输出标准事实，不能反向修改信号、参数或资产池；通用分析脚本位于仓库共用层，通过配置读取这些事实。报告阶段只调用仓库现有 Vibe-Trading 能力，本变更不新增适配层或职责契约。

## 9. 每日状态流与 A1 分配

每日处理顺序固定为：

```text
T 日收盘更新指标与信号
→ T+1 日开盘处理全仓退出
→ 处理强制风险减仓
→ 汇总同级的新建仓与加仓候选
→ A1 分配资金和风险预算
→ 应用整手与成交约束
→ 更新批次、现金、持仓、共同止损和审计
```

同一 ETF 当日出现退出时取消它的所有买入候选。每个有效建仓或加仓候选先产生最多一个 U0（标准单位）的请求量。A1 对所有仍可行候选使用同一完成比例，直到资金、单 ETF、资产组、组合计划风险和目标波动率全部满足；候选被自身或所属组上限卡住后，未用预算可以流向其他可行候选。

缩放结果先向下取整到交易整手。剩余预算按小数余额从大到小逐手补分，每补一手重新检查全部硬门槛，完全同分时按 ETF 代码升序。算法不得依赖输入列表顺序。

停牌是正常市场状态，不伪造成交。未满 60 个样本只禁止对应 ETF 新增风险。任一持仓 ETF 缺少可用价格或风险输入时暂停整个组合新增风险，不填零、不使用陈旧协方差；其他可交易 ETF 仍允许退出和强制减仓。

## 10. 完整本地分析与挑战筛选

### 10.1 标准分析数据包

每个基线或挑战运行先输出八类版本化 Parquet 表：

```text
equity.parquet
returns.parquet
trades.parquet
orders.parquet
positions.parquet
risk.parquet
events.parquet
benchmarks.parquet
```

`trades.parquet` 保存可复算的完整往返交易，不以建仓、加仓和退出事件流水冒充交易。所有表声明结构版本、主键、日期、货币、单位和文件摘要，跨表必须满足权益变化、现金、持仓、成交、费用和收益勾稽关系。沪深 300 与纳斯达克 100 人民币口径基准按显式日历对齐；缺失时停止相关分析，不使用 ETF 代理或零收益补齐。

### 10.2 绩效、风险和归因

通用确定性分析层计算累计收益、CAGR（复合年增长率）、年化波动率、最大回撤与恢复时间、Sharpe（夏普比率）、Sortino（索提诺比率）、Calmar（卡玛比率）、胜率、盈亏比、利润因子、持有期、换手率、费用、滑点、仓位和现金分布，以及 Alpha（超额收益）、Beta（市场暴露）、信息比率和上下行捕获率。

归因至少按 ETF、资产组、时期、交易原因、仓位、现金、趋势过滤和风险约束拆分。贡献总和必须在固定容差内回到组合收益或风险事实。

### 10.3 本地完整稳健性

本地使用固定公式、固定随机种子、场景定义和门槛，完成：

- 40/60 日入场、1.5N/2.5N 止损、120 日滚动/30 日半衰期 EWMA 协方差；
- 三个固定时期和季度移动三年窗口；
- 11 个逐 ETF 删除与 6 个逐资产组删除；
- 5 个费用、滑点和延迟执行场景；
- 5/20/60 日连续区块各 10,000 条、每条 756 日的 Block Bootstrap（区块自助抽样）；
- 5 个历史压力窗口、4 个持仓假设冲击和 3 项 CVaR（条件风险价值）。

参数、资产、成本或延迟变化会改变交易路径时，必须重新运行本地确定性海龟模块。静态压力和尾部分析只消费实际输出。全部结果写入 `local-evidence-matrix.parquet`，并明确 `authority=local_exploratory`。

### 10.4 七方案挑战与推荐

七个固定方案全部完成模拟、绩效、归因和推荐所需稳健性分析。系统不计算用于自动淘汰的综合分数，也不按本地收益排名。报告可以明确推荐维持基线、关注某些挑战、修订后再评估或因证据不足停止；无论推荐如何，六个挑战都保留并交由人工确认。

报告阶段复用仓库现有 Vibe-Trading 能力形成归因解释、证据挑战、反例、不确定性和报告材料，存在已知前视偏差的组合优化器继续禁用。本变更不改变 Vibe 的既有责任边界或实现。

### 10.5 完整输出与人工确认

一个完整海龟本地研究至少输出：

```text
run-manifest.json
八类标准分析 Parquet
candidate-comparison.parquet
candidate-screening.parquet
local-evidence-matrix.parquet
attribution.parquet
local-research-report.md
challenge-report.md
recommendation.json
candidate-strategies.json
```

`recommendation.json` 的建议值固定为 `proceed_to_joinquant`、`revise_and_reassess` 或 `stop_evidence_insufficient`，并列出推荐基线行动、挑战关注项、确定性理由、反对证据、不确定性、阻断项及“不是正式回测或最终验收结论”的声明。

完整输出校验通过后，运行状态可以为 `complete`，但同时必须输出 `next_action=human_confirmation_required`。人工确认通过独立追加式 `human-decision.json` 引用运行、报告和推荐摘要；确认前不得启动聚宽、替换基线、修改参数、冻结策略或启动模拟交易。

任一标准分析表、完整报告、挑战对比、证据矩阵、归因、推荐或七项候选缺失、结构无效或摘要不匹配时不得输出 `complete`。

## 11. 状态与错误处理

运行状态且只能是：

| 状态 | 使用条件 |
|---|---|
| `evidence_insufficient` | 真实策略身份、快照、来源、字段、范围或声明输入在项目执行前不完整 |
| `failed` | 已有文件摘要或内容不一致、结构/类型/重复键违规、批次冲突、项目进程异常、硬约束突破、同输入不同输出或远端临时文件清理不可确认 |
| `complete` | 输入门禁、项目流程、必需输出、摘要和原子固化全部通过 |

不得把部分结果标记为完成，也不得用零值、旧数据或默认口径继续。项目建议为“修订后再评估”时流程仍可 `complete`；两套状态不合并。

## 12. 测试与验收

### 12.1 共用脚本单元测试

- 配置、仓库路径、参数数组和输出边界；
- CSV 暂存导入、Parquet 批次身份、逻辑与字节摘要、字段、日期、空值、重复键和布尔规范化；
- 相同内容去重、冲突重叠拒绝、新标的追加和旧快照不变；
- 快照清单、内存 DuckDB 视图和规范化摘要；
- 八类标准分析表的结构、主键、单位、摘要和跨表勾稽；
- 绩效、基准、Alpha、Beta、归因、区块抽样、压力和 CVaR 黄金样例；
- 现有 Vibe 能力调用、禁用优化器、报告产物和能力不可用场景；
- 人工决定追加记录和确认前停止门禁；
- `run_id`、原子固化、幂等复用、失败重试和确定性冲突；
- 三态唯一收口和敏感信息清理。

共用测试不得出现海龟资产、参数或规则常量。

### 12.2 海龟规则与不变量测试

- 55/20 日通道排除当日、TR、20 日 N、U0 和次日执行；
- 固定 0.5N 档位、每日一次、共同止损只上移；
- 资金、流动性、单 ETF、资产组、计划风险和波动率上限；
- 退出、强制减仓、同级买入顺序；
- A1 同比例、整手、小数余额、代码同分和输入乱序；
- 现金不为负、硬上限不突破、相同输入输出摘要一致；
- 停牌、涨跌停、不可成交、60 样本冷启动和风险输入故障安全。

### 12.3 完整 E2E

完整 E2E（端到端）从 Skill 用户入口启动，使用固定小型日线夹具，经过 CSV 暂存、Parquet 批次、快照、内存 DuckDB、通用运行器、`strategy-003` 适配器、收盘信号、次日订单、持仓/现金/风险、标准分析数据包、绩效归因、本地稳健性、七方案挑战、现有 Vibe 能力调用、报告、推荐、人工确认前停止门禁和不可变证据收口。不能以若干单元测试拼接替代。

另用不含海龟词汇和资产的最小项目适配器执行同一 E2E，证明 Skill、行情中心和运行器未反向依赖海龟。常规自动测试不访问网络、不加载历史 `.local` 数据，并在临时目录结束后清理。

### 12.4 真实集成验收

使用聚宽真实研究环境导出最终 11 只 ETF 全历史日线，验证固定字段、未复权口径、传输摘要、Parquet 共享批次、快照、内存视图、七方案和一次完整本地分析。人工复核完整报告、推荐与证据矩阵；完成后确认 `next_action=human_confirmation_required`，并复查聚宽远端临时文件和本地暂存产物均不存在。

最终执行 `quick_validate.py`、仓库布局测试、Build and Verify（构建与验证）完整检查、OpenSpec 严格校验和公开仓库敏感数据扫描。

## 13. 实施顺序与回滚

1. 保留已验证的 `strategy-003` 身份、海龟纯计算模块和历史 CSV 运行证据，不把旧结果伪装成新契约结果。
2. 以 TDD（测试驱动开发）把共享行情中心迁移到 Parquet 权威批次和内存 DuckDB 查询，重新生成快照身份。
3. 以 TDD 建立标准分析数据包和共用绩效/基准/归因/稳健性能力，报告阶段复用现有 Vibe-Trading 能力。
4. 对七方案执行完整本地研究，生成报告、证据矩阵和推荐，并在人工确认门禁停止。
5. 完成离线海龟 E2E、非海龟 E2E、真实 11 只 ETF 集成验收和临时产物清理。
6. 运行仓库完整验证、独立前向验证、OpenSpec 严格校验和敏感数据扫描。

回滚只停用新增 Skill 和共用脚本，不删除已验证不可变批次、快照或完整运行证据。不得修改或删除 `strategy-001`、`strategy-002`，不得把功能分支本地合入主干。未固化暂存和未引用文件只有在确认不属于任何清单后才可清理。
