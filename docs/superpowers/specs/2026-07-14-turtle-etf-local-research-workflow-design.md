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

正式回测和模拟交易仍只在 JoinQuant（聚宽）云端运行。本地流程只验证规则、生成探索性研究证据并向后续聚宽回测交付固定候选包。`complete` 只表示本地流程完整执行，不代表策略通过正式回测、稳健性验收或实盘准入。

## 2. 发布结构与依赖方向

实现采用四层单向依赖：

```text
run-local-quant-research Skill（流程编排）
                ↓
仓库共用脚本（行情中心、运行器、证据）
                ↓
strategy-003 项目适配器与海龟纯计算模块
                ↓
.local 共享行情与策略运行证据
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

Skill（技能）目录不保存 Python 实现、行情、海龟参数或测试数据，只描述调用顺序和停止条件。共用脚本不导入 `strategy-003`；项目适配器可以调用共用行情读取接口，但共用层不得反向解释海龟字段。

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
│   ├── market-data.csv
│   └── validation.json
└── snapshots/<snapshot_id>.json
```

不建立 `raw/`、`data/`、`reports/` 多层重复目录，不保存长期远端副本，也不在公开仓库生成包含行情值的 receipt（摘要凭据）。`.local/` 是单机私有存储，用户自行负责备份；本变更不把它提升为跨机器正式归档。

### 4.2 批次身份

`market-data.csv` 是从来源精确保留的原始导出，也是唯一行情事实源。`batch_id` 由规范化来源身份、导出契约和 CSV 字节 SHA256（文件摘要）计算；清单至少记录：

- schema version（结构版本）；
- `source`、`asset_type`（标的类型）、`frequency=1d`；
- 证券列表和每只证券实际起止日；
- 字段顺序、时区、交易日历和价格口径；
- `snapshot_end_date`（快照截止日）；
- 导出代码摘要、CSV 字节摘要、行数和规范化内容摘要；
- 创建时间、验证状态和验证器版本。

同一来源身份和 CSV 字节摘要完全相同时复用已有批次。新标的或新日期可以追加新批次。相同来源、频率、证券和日期出现不同值或不同价格口径时拒绝导入；首版不实现自动修订、覆盖或 `supersedes`（取代关系）。

### 4.3 快照身份

`snapshot_id` 由规范化快照清单的 SHA256 计算。快照清单只选择已验证批次，并锁定证券、日期、字段、来源和价格口径，不复制行情。旧快照永不追随新批次变化；新增证券必须显式创建新快照。

项目配置通过 `snapshot_id` 声明数据需求。共用脚本先验证批次文件与摘要，再验证快照是否完整覆盖项目请求。缺少快照、证券、字段、区间、来源或口径时停止，不以旧快照、部分资产池或猜测值补齐。

### 4.4 DuckDB 查询层

查询进程使用 `duckdb.connect(':memory:')` 从快照引用的权威 CSV 建视图。查询层统一字段顺序、数据类型、空值、日期、证券排序，并把聚宽返回的 `paused` 从数值规范化为布尔值；规范化结果摘要必须与 CSV 规范化摘要一致。

`.local/market-data/` 不保存 `.duckdb` 文件。DuckDB（嵌入式分析数据库）视图可以随时仅凭快照清单和权威 CSV 重建，不构成第二事实源。

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

CSV 保存未复权实际价格与 `factor`（复权因子），本地研究不生成或使用复权价。供后续聚宽功能校验的策略信号同样显式使用 `fq=None`，含场内基金的策略设置 `use_real_price=False`。报告必须说明该模式的撮合价使用聚宽固定基准日前复权行为，不能把它描述为未复权实际成交价。

远端文件只是传输中转。本地收到文件后先与远端回读字节 SHA256 比较，再导入批次并删除远端文件。任何一项摘要不一致或无法确认远端清理都输出 `failed`。实现不得保存或打印账号、密码、Token（访问令牌）或 Cookie（浏览器凭证）。

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
→ 快照和 CSV 摘要校验
→ 内存 DuckDB 视图同源校验
→ 创建隔离暂存目录
→ 以参数数组调用项目适配器
→ 校验项目输出结构与摘要
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
| `reporting.py` | 审计、指标、报告、研究建议和候选清单 | 所有输出绑定运行身份和摘要 |

模块之间使用明确记录对象，不读取全局路径或环境变量。报告模块只消费确定性结果，不能反向修改信号、参数或资产池。

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

## 10. 项目输出契约

一个完整海龟运行至少输出：

```text
run-manifest.json
daily-audit.csv
trades.csv
positions.csv
risk.csv
research-report.md
conclusion.json
candidate-strategies.json
```

`research-report.md` 包含方法、快照/配置/代码身份、事件和交易结果、实际仓位分布、现金占比、留现原因、资产组和组合风险使用率、限制及产物摘要。设计期 9 只 ETF 的 63.7% 和 55.7% 代理仓位只用于说明研究必要性，不得冒充最终 11 只 ETF + 完整规则结果。

`conclusion.json` 的项目建议与流程状态分离。建议值固定为：

```text
proceed_to_joinquant       进入聚宽回测
revise_and_reassess        修订后再评估
stop_evidence_insufficient 证据不足而停止
```

它必须列出确定性理由、阻断项、证据摘要和“不是正式回测或最终验收结论”的声明。

`candidate-strategies.json` 恰好包含七项：一项冻结基线，以及 40/60 日入场、1.5N/2.5N 止损、120 日滚动/30 日半衰期 EWMA（指数加权移动平均）协方差六项单参数挑战。七项共用同一策略代码摘要和 `snapshot_id`，只由配置区分。本地结果可以附方向性证据和风险提示，但不能按收益排名删除候选、替换基线或新增参数。

三类必需输出任一缺失、结构无效或摘要不匹配时不得输出 `complete`。

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
- 批次身份、字节摘要、字段、日期、空值、重复键和布尔规范化；
- 相同内容去重、冲突重叠拒绝、新标的追加和旧快照不变；
- 快照清单、内存 DuckDB 视图和规范化摘要；
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

正式 E2E（端到端）从 Skill 用户入口启动，使用固定小型日线夹具，经过共享批次、快照、CSV 校验、内存 DuckDB、通用运行器、`strategy-003` 适配器、收盘信号、次日订单、持仓/现金/风险、三类输出和不可变证据收口。不能以若干单元测试拼接替代。

另用不含海龟词汇和资产的最小项目适配器执行同一 E2E，证明 Skill、行情中心和运行器未反向依赖海龟。常规自动测试不访问网络、不加载历史 `.local` 数据，并在临时目录结束后清理。

### 12.4 真实集成验收

使用聚宽真实研究环境导出最终 11 只 ETF 全历史日线，验证固定字段、未复权口径、字节摘要、共享批次、快照、内存视图和一次完整本地研究。完成后复查聚宽远端临时文件和本地暂存产物均不存在。

最终执行 `quick_validate.py`、仓库布局测试、Build and Verify（构建与验证）完整检查、OpenSpec 严格校验和公开仓库敏感数据扫描。

## 13. 实施顺序与回滚

1. 建立并同步真实 `strategy-003` 身份，不启动正式回测。
2. 以 TDD（测试驱动开发）实现共享日线行情中心和内存 DuckDB 查询。
3. 初始化薄 Skill，建立共用运行器、证据和三态收口。
4. 以 TDD 实现海龟纯计算模块、项目适配器和固定夹具。
5. 完成离线海龟 E2E、非海龟 E2E 和三类输出。
6. 导入真实 11 只 ETF 快照并执行完整本地研究。
7. 运行仓库完整验证、独立前向验证和敏感数据扫描。

回滚只停用新增 Skill 和共用脚本，不删除已验证不可变批次、快照或完整运行证据。不得修改或删除 `strategy-001`、`strategy-002`，不得把功能分支本地合入主干。未固化暂存和未引用文件只有在确认不属于任何清单后才可清理。
