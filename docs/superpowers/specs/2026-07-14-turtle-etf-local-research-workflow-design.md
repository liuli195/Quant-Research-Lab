---
comet_change: build-turtle-etf-local-research-workflow
role: technical-design
canonical_spec: openspec
archived-with: 2026-07-17-build-turtle-etf-local-research-workflow
status: final
---

# 海龟 ETF 本地研究流程与聚宽原生分析数据技术设计（vectorbt 执行内核修订）

## 1. 设计边界

需求和验收场景以以下 OpenSpec（开放规格）增量规格为唯一事实源：

- `openspec/changes/build-turtle-etf-local-research-workflow/specs/local-quant-research-workflow/spec.md`
- `openspec/changes/build-turtle-etf-local-research-workflow/specs/standard-strategy-analysis-data/spec.md`
- `openspec/changes/build-turtle-etf-local-research-workflow/specs/turtle-etf-local-research/spec.md`

本文只说明实现结构、接口、数据流、错误处理和测试方法，不建立第二份需求规格。

整体架构固定为三个独立 Skill（技能）：本地研究流程、JoinQuant（聚宽）回测流程、策略分析。标准分析数据以仓库现有聚宽回测归档为物理基准：聚宽现有目录 0 改动直读，本地 vectorbt（向量化回测框架）结果生成聚宽同名的四类共同执行事实；聚宽官方 `risk` 和 `period_risks` 作为来源参考，不要求本地流程预先计算。本变更主要实现本地研究流程和统一分析读取能力；聚宽回测流程与策略分析 Skill 另立变更。

2026-07-14 的真实行情运行表明，旧 Python（编程语言）逐日内核在 A1（同日共享预算分配）和组合风险可行性复算处连续触发 10、30 和 60 分钟超时。性能剖析已把主要耗时定位到 `process_day → allocate_a1 → _maximum_hamilton_allocation → evaluate_risk`。本次修订不改变策略规则和行情口径，只把本地模拟内核迁移到 vectorbt 官方 `Portfolio.from_order_func()`（自定义订单函数）。单次冷/热计时均从已准备输入进入 vectorbt 开始，到交易执行、四类共同事实、海龟必需归因日志及其结构/摘要/勾稽校验完成时停止，均限制在 180 秒内；停止计时后才写入 `performance.json` 和最终清单。独立确定性分析耗时单独记录，不计入该门禁。

## 2. 发布结构与依赖方向

实现采用单向依赖，并以聚宽原生结果读取契约隔离三个流程：

```text
run-local-quant-research Skill（本变更）
  → 一次只接收一个场景
  → 行情中心、通用运行器、strategy-003 适配器
  → vectorbt 官方 Portfolio.from_order_func
  → .local/quant-research/strategy-003/<run_id>/backtests/<local_backtest_id>/
  → next_action=return_to_caller

run-joinquant-backtest Skill（后续变更）
  → 保持现有聚宽回测与归档产物，不增加适配或转换步骤

strategy-analysis Skill（后续变更）
  ← 统一读取现有聚宽目录或本地聚宽口径兼容目录

本变更独立确定性策略分析
  ← 主 agent 多次调用 Skill 得到的独立兼容结果
  ← strategy-003 自有 analysis-plan.json 展开的场景、双基准集与完整稳健性结果
  → .local/strategy-analysis/<analysis_id>/
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
├── analysis_data/
│   ├── __init__.py
│   ├── manifest.py
│   ├── views.py
│   ├── derived.py
│   ├── cli.py
│   └── schemas/local-backtest-manifest.schema.json
├── quant_analysis/
│   ├── metrics.py
│   ├── benchmarks.py
│   ├── attribution.py
│   ├── robustness.py
│   ├── stress.py
│   ├── cvar.py
│   ├── evidence.py
│   ├── cli.py
│   └── schemas/analysis-plan.schema.json
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
    ├── analysis-plan.json
    ├── adapter.py
    ├── export-request.json
    ├── turtle_etf/
    │   ├── indicators.py
    │   ├── vectorbt_inputs.py
    │   ├── vectorbt_callbacks.py
    │   ├── vectorbt_engine.py
    │   └── vectorbt_adapter.py
    └── fixtures/

tests/
├── market_data/
├── local_quant_research/
└── strategy_003/
```

Skill（技能）目录不保存 Python 实现、行情、海龟参数或测试数据，只描述一次单场景调用顺序和停止条件。`analysis_data` 只负责读取现有聚宽归档、建立六表内存视图、校验和派生查询；它不导入 `strategy-003`、vectorbt 或分析算法，也不写回聚宽目录。`quant_analysis` 是独立策略分析能力的算法实现，只按通用 Schema（结构约束）校验和展开策略自有 `analysis-plan.json`；本地研究运行器和海龟项目不得导入。项目适配器只负责把本地执行事实适配为聚宽口径结果。

共享行情中心另在 `.local/market-data/benchmark-sets/<benchmark_set_id>/` 保存双基准清单和 `benchmark-returns.parquet`。该基准集与策略、执行引擎和来源回测目录解耦；聚宽现有单基准收益只作来源参考。

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
│   ├── corporate-actions.parquet
│   └── validation.json
└── snapshots/<snapshot_id>.json
```

不建立 `raw/`、`data/`、`reports/` 多层重复目录，不保存长期远端副本，也不在公开仓库生成包含行情值的 receipt（摘要凭据）。`.local/` 是单机私有存储，用户自行负责备份；本变更不把它提升为跨机器正式归档。

### 4.2 批次身份

`market-data.parquet` 保存原始未复权日线，`corporate-actions.parquet` 保存经来源核验且保留版本语义的拆分、现金分红等公司行动；两者共同构成本地行情事实。连续总回报价格只在查询或输入构造时用生效日可见的原始 `close/pre_close` 行情事实派生；公司行动元数据只用于核对，晚公布记录必须标为事后核对，不回写此前历史，也不固化成第二份行情。聚宽 CSV（逗号分隔文件）只存在于远端传输和本地隐藏暂存目录，转换与验证完成后删除。`batch_id` 由规范化来源身份、结构版本、导出契约和两类规范化逻辑内容摘要计算，不直接依赖 Parquet 编码字节；清单至少记录：

- schema version（结构版本）；
- `source`、`asset_type`（标的类型）、`frequency=1d`；
- 证券列表和每只证券实际起止日；
- 字段顺序、时区、交易日历和价格口径；
- `snapshot_end_date`（快照截止日）；
- 导出代码摘要、远端与本地传输 CSV 字节摘要、Parquet 字节摘要、行数和规范化内容摘要；
- Parquet 写入器、DuckDB 和结构版本；
- 公司行动来源事件标识、类型、公告日、登记日/除权日/生效日/支付日、拆分比例或每份现金、状态、知识截止日、来源身份与摘要；
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

传输 CSV 与固化 Parquet 保存原始未复权实际价格、`pre_close`（前收盘参考价）与 `factor`（复权因子），只作为行情事实和审计依据。本地 vectorbt 只按实际应用日可见的 `上一交易日原始 close / 当日 pre_close` 更新从该日生效的累计连续因子，并将同一因子应用于当日及以后 OHLC 与 `pre_close`；生效日停牌时应用日延后到首个复牌行情日。公司行动元数据只用于授权和审计价格基准变化：公告日在生效日之前或当日标为 `point_in_time`，晚于生效日标为 `retrospective_reconciliation`；取消状态按取消日期与快照截止日重建，截止日后的取消不得回写历史，当前显示已取消却缺少取消日期时停止导出；官方拆分比例或每份现金均不决定因子幅度或订单，有效事件未出现价格基准变化时只记录审计。突破、N 值、协方差、波动率、vectorbt 成交与估值统一使用连续经济价格和经济单位；现金分红按除权日隐含再投资近似，不在支付日另增现金。公司行动来源必须通过真实接口与真实事件最小验证；取消事件不得授权变化，若状态未知、知识截止日无效，或 `pre_close` 与前一交易日收盘的价格基准变化没有对应有效事件，则批次或运行以 `evidence_insufficient`（证据不足）停止。供后续聚宽功能校验的策略信号同样显式使用 `fq=None`，含场内基金的策略设置 `use_real_price=False`。报告必须分别说明本地研究近似与聚宽撮合限制，不能把两者描述成逐日账户精确对账。

远端文件和本地 CSV 都只是传输中转。本地收到文件后先与远端回读字节 SHA256 比较，再在隐藏暂存目录执行固定结构解析、Parquet 转换、规范化内容摘要和 DuckDB 回读复核；随后先删除两端 CSV 并确认清理，再把清理结果写入批次清单，最后只原子发布已整理的 Parquet 批次。发布后不再依赖清理动作。任何一项摘要不一致、转换失败或无法确认清理都输出 `failed`。实现不得保存或打印账号、密码、Token（访问令牌）或 Cookie（浏览器凭证）。

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
→ 对同一场景完成冷启动与预热执行、结果摘要一致性和180秒门禁
→ 用聚宽原生结果读取器校验一份本地兼容结果与 performance.json
→ 全部门禁通过后原子固化单场景清单、唯一权威结果和状态
→ 输出 next_action=return_to_caller 后停止
```

项目适配器接收经过验证的快照清单路径、项目配置路径和暂存输出目录，并通过共用 `market_data` Python 接口读取数据。适配器不能写出暂存目录，也不能要求共用运行器解释策略字段。

## 7. 运行身份与原子证据

`run_id` 由以下规范化摘要计算：

```text
snapshot manifest + project config + normalized scenario config + declared code/backend identity
```

策略成功证据位于：

```text
.local/quant-research/strategy-003/<run_id>/
└── backtests/
    └── <local_backtest_id>/
        ├── manifest.json
        ├── code.py
        ├── params.json
        ├── params_versions/<params_sha256>.json
        ├── performance.json
        └── data/
            ├── results.parquet
            ├── balances.parquet
            ├── positions.parquet
            ├── orders.parquet
            └── attribution_log-<sha256>.parquet（strategy-003 必需扩展）
```

从 `backtests/<local_backtest_id>/` 向内，文件位置、名称和数据集清单结构尽量镜像聚宽现有 `backtests/<backtest_id>/`。本地不存在真实聚宽详情页、远端响应和官方计算结果，因此不伪造 `raw/`、`research_response`、`collection_fence`、`official-summary.csv`、`risk.parquet` 或 `period_risks.parquet`；本地清单固定 `schema_version=local-backtest/1`、`object.kind=local_backtest`、`source.kind=local_vectorbt`、`authority=local_research`，并由独立本地 Schema 校验。项目先在同一文件系统的隐藏暂存目录生成同一场景的冷启动和预热产物并比较规范化结果摘要；通过后保留冷启动权威候选，删除预热副本和可丢弃暂存并确认清理，再把清理结果写入 `performance.json`、生成最终清单并校验全部摘要。运行器最后只使用原子目录替换固化已整理的一份权威 `<run_id>`，发布后不依赖写入或清理。失败尝试只保留紧凑 attempt manifest（尝试清单）和诊断，不留下可被误认为完成的运行目录。

同一 `run_id` 已有 `complete` 证据时先重新校验：全部通过则复用，任何输出摘要差异都视为确定性冲突并输出 `failed`。快照、项目配置、单场景配置、代码或后端身份任一变化都会生成新 `run_id`。失败重试使用新的 `attempt_id`，不覆盖前次尝试或既有完整运行。

独立策略分析证据不写回不可变本地研究运行目录，而保存在：

```text
.local/strategy-analysis/<analysis_id>/
```

每个 `run_id` 只绑定一个场景。调用前先在 `.local/strategy-analysis-preparations/<preparation_id>/` 固化分析计划、基准集、运行模板和七份单场景配置。主 agent（代理）分别调用 Skill 后，显式登记七组 `scenario_id -> run_id`；分析入口不得扫描目录猜测来源，必须校验运行唯一、同一快照、同一代码身份与执行后端，再由准备身份和全部来源摘要派生最终 `.local/strategy-analysis/<analysis_id>/source-results.json`。`analysis_id` 绑定全部基础/稳健性来源、双基准集、分析配置和可选 Vibe-Trading（氛围量化）审计身份，同计划下的另一批来源不得覆盖旧证据。分析完成、失败或证据不足都不得修改任何来源 `run_id`、结果或本地流程状态。后续人工决定绑定 `analysis_id`、完整报告和 `recommendation.json` 摘要，属于独立策略分析流程的门禁。

## 8. 海龟项目模块与 vectorbt 边界

项目层保留海龟规则，vectorbt 只接管模拟与账户记账：

| 模块 | 责任 | 主要不变量 |
|---|---|---|
| `indicators.py` | 以连续总回报经济价格计算 TR（真实波幅）、N 值、突破通道、收益率和协方差输入 | 通道排除当日；协方差收益使用连续 `close / pre_close - 1`；原始行情只作审计 |
| `vectorbt_inputs.py` | 把 DuckDB 查询结果、原始行情事实、公司行动核对元数据、累计连续因子、指标和 55 日入场/20 日退出/0.5N 加仓规则转为日期 × ETF 的只读 NumPy 数组 | 因子只依赖生效日可见行情；晚公布元数据只标记事后核对；不回写事件前历史；收盘信息显式错位；不含成交额或流动性参数；稳定类型与排序 |
| `vectorbt_callbacks.py` | 唯一的 Numba 数值状态、A1、风险和四类官方回调实现 | `nopython`（无 Python 模式）；不创建账户或交易记录；不保留 Python 兼容实现 |
| `vectorbt_engine.py` | 固定 vectorbt 参数并调用官方 `Portfolio.from_order_func()` | 单一共享现金组；普通订单函数；不直接调用私有内核 |
| `vectorbt_adapter.py` | 把 `Portfolio`（组合）记录转换为 `results`、`balances`、`positions`、`orders`、本地清单和海龟必需归因日志 | 清单声明连续经济价格、经济单位、隐含再投资及不能精确对账聚宽；不生成聚宽官方风险摘要；不计算分析结论 |

`vectorbt_inputs.py` 输出不可变 `SimulationInputs`，至少包含日期、证券、原始开高低收、原始前收盘、连续经济开高低收、连续前收盘、累计连续因子、公司行动有效掩码与摘要、成交量、停牌、涨跌停、已错位信号、N 值、协方差、资产组和成本数组。它不得包含成交额、最低成交额、订单参与率或任何流动性参数。输入构造可以使用 Pandas（数据处理）与 NumPy，但回调只接收连续数值数组、标量和预分配状态，不接收 DataFrame（数据表）、Decimal（高精度小数）、字典或 Python 对象。

公司行动在进入 vectorbt 前由输入构造器处理。拆分与现金分红都通过从应用日开始生效的连续总回报因子进入连续经济 OHLC，因子幅度只来自当时可见的原始 `close/pre_close`；经济单位在 vectorbt 内保持稳定；现金分红等价于除权日隐含再投资，不在支付日增加共享现金，避免重复计入。归因日志记录事件标识、类型、应用日、官方比例或每份现金、`evidence_timing=point_in_time|retrospective_reconciliation`、累计连续因子和来源摘要，但不伪造订单、现金流或真实份额变更。代码身份与本地清单必须固定 `corporate_action_mode=point_in_time_total_return_approximation`、`continuity_factor_basis=raw_previous_close_over_current_pre_close`、`corporate_action_metadata_timing=audit_only_may_be_retrospective`、`price_basis=continuous_economic_price`、`quantity_basis=economic_units`、`cash_dividend_mode=implicit_reinvestment_on_ex_date`、`pay_date_cash_supported=false` 和 `exact_joinquant_reconciliation=false`。这是研究级收益近似，不是聚宽账户复刻，也不声称全部事件元数据在生效日已知。

海龟策略完全不执行流动性判断。共享行情中的 `money`（成交额）可以被 ETF 池筛选或其他策略使用，但海龟配置、输入、回调和原因码均不读取它；成交额为空、极低或极高时，相同价格与状态输入必须生成完全相同的订单和结果。

项目固定 vectorbt 1.1.0。其兼容要求会推动项目 `.venv` 中 NumPy 与 Pandas 的小版本升级，因此实施第一步必须锁定依赖并运行全仓回归。代码身份增加 `execution_backend=vectorbt.from_order_func`、vectorbt/Numba/NumPy/Pandas 版本、回调摘要和输出适配器版本。依赖审计同时记录 vectorbt 的 Apache 2.0 + Commons Clause（商业销售限制条款）许可；本变更只用于仓库内部研究，不把 vectorbt 打包为对外销售的回测产品或服务。通用 Skill、行情中心、标准数据契约、量化分析和非海龟夹具不导入 vectorbt。

实施过程中先从现有代码提取已经确认的信号、A1、风险和状态规则，迁入 `vectorbt_inputs.py` 与 `vectorbt_callbacks.py`，再由规格夹具直接声明预期订单、成交、状态和风险结果。新路径通过后，删除旧 `execution.py`、`state.py`、`signals.py`、`risk.py`、`allocation.py`、直接耦合分析算法的 `reporting.py`，以及只服务这些模块的测试和公开导出；不得保留兼容模块、别名、双引擎开关、运行时回退或无效代码。`indicators.py` 仅在新输入路径实际使用时保留。

## 9. 官方回调生命周期、每日状态流与 A1

所有 ETF 设置为一个 vectorbt 组合组并启用 `cash_sharing=True`（共享现金）。现行规则每只 ETF 每个交易日最多一笔最终订单，使用普通 `Portfolio.from_order_func()`；不启用 `flexible=True`（灵活多订单）。官方回调生命周期固定为：

```text
prepare_simulation_inputs
→ 把 T 日收盘信号和滚动统计错位到 T+1 执行行
→ pre_sim_func_nb：分配批次、止损、原因码、候选和订单临时数组
→ pre_segment_func_nb：读取当日开盘前状态，分类退出、强制减仓和买入候选，设置调用顺序
→ order_func_nb：先返回退出和风险减仓订单
→ 第一笔买单前：按实际卖出后的现金与持仓只运行一次 allocate_a1_nb
→ order_func_nb：为各买入候选返回最多一笔订单
→ post_order_func_nb：只按实际成交更新海龟状态和审计
→ vectorbt：固化订单、交易、持仓、现金、费用和权益记录
→ vectorbt_adapter：转换为四类共同执行事实、本地清单和海龟必需归因日志
```

`pre_segment_func_nb` 可以查看整个共享现金组并改写当日 `call_seq`（调用顺序），但不能提前假定卖单会成交。它只分类候选和排序。`order_func_nb` 处理完退出与风险减仓后，在首个买入列到达时读取 vectorbt 当前现金和持仓，调用一次 `allocate_a1_nb`，把所有买入数量写入预分配数组；后续买入列只读取结果。这样既保留“卖出实际成交后再分配”的原规则，也避免每个 ETF 重算 A1。

A1 仍执行同一完成比例、整手向下取整、Hamilton 余额补分和 ETF 代码同分规则。Numba 内核使用连续数组、预计算的当前敞口、资产组映射和协方差矩阵复用不变量；不得通过修改风险公式或假定组合波动率单调来换取速度。每个候选补一手时仍复核资金、单 ETF、资产组、组合计划风险和目标波动率硬门槛。单 ETF 或资产组因价格变化被动超过资金上限时采用“不得恶化”判断：不强制卖出，不冻结其他证券，只禁止增加同一超限证券或同一超限组；退出、止损和风险降低订单始终允许。

市场可交易性由项目回调判断：停牌不下单；买入开盘触及不可买上限时拒绝；卖出开盘触及不可卖下限时拒绝；退出取消同一 ETF 当日买入。订单价格为次日开盘价，费用和滑点交给 vectorbt 官方订单处理。`post_order_func_nb` 读取 `order_result`（订单结果），只有实际成交才更新固定信号日 N、理论档位、共同止损和批次审计；拒单、无订单和未成交不推进状态。

停牌是正常市场状态，不伪造成交。未满 60 个样本只禁止对应 ETF 新增风险。任一持仓 ETF 缺少可用价格或风险输入时暂停整个组合新增风险，不填零、不使用陈旧协方差；其他可交易 ETF 仍允许退出和强制减仓。

执行语义直接按 OpenSpec（开放规格）场景和固定小型合成夹具验证逐日订单、成交、现金、持仓、批次、共同止损、风险原因和最终摘要。所有夹具通过后，`cli.py` 每次只把传入的一个场景交给 `run_vectorbt_simulation`，拒绝候选数组和内部循环，随后删除旧执行与报告模块和专用测试。删除后以全仓扫描和公开入口 E2E（端到端）证明 `process_day`、旧 `_simulate`、旧对象导入、分析算法导入和兼容路径均不存在。

## 10. 聚宽原生分析数据与独立策略分析验证

### 10.1 现有归档是物理基准

2026-07-14 对仓库的只读核对覆盖 120 个 `gate.status=pass` 的聚宽回测清单：全部拥有同一组数据集。125 份核心 Parquet 中，`results` 与 `period_risks` 结构完全一致；其他表的差异只涉及列顺序、`cancel_time` 的空类型/字符串表现，以及少量风险数值的整数/浮点表现。基于该事实，核心分析输入固定为聚宽现有六表：

```text
data/results.parquet
data/balances.parquet
data/positions.parquet
data/orders.parquet
data/risk.parquet
data/period_risks.parquet
```

`results` 提供策略和聚宽单基准的累计收益；`balances` 提供总资产、净值和现金；`positions` 与 `orders` 提供持仓、成交、费用和完整往返交易重建输入；`risk` 与 `period_risks` 提供聚宽官方风险摘要。只读实测确认聚宽 `results` 为 `time:string`、`returns:double`、`benchmark_returns:double`。统一读取器把时间规范化为 Asia/Shanghai（亚洲/上海）交易日，并按相邻累计净值比在查询期派生单日收益；双基准文件保存单日人民币总回报，只在共同有效交易日比较单日序列，不得把来源累计收益直接与基准单日收益比较。权益、收益、双基准、完整往返交易和事件等分析表只作为 DuckDB（嵌入式分析数据库）查询期派生视图，不再要求物理八表。

### 10.2 聚宽结果 0 改动直读

聚宽标准输入就是现有 `joinquant/strategies/<strategy_id>/backtests/<backtest_id>/`。读取器先验证原 `manifest.json`、对象身份、`gate.status=pass`、数据集状态、文件摘要与行数，然后直接读取现有 Parquet。不得新增 `analysis-data-manifest.json`、转换后目录、八表副本或回写字段；聚宽回测与归档流程不调用本变更的新接口。

清单中 `positions` 或 `orders` 合法声明 `status=complete`、`rows=0`、`verified_empty=true` 时，可以没有对应 Parquet；读取器在内存建立固定字段空视图。物理列顺序、`cancel_time` 的 Arrow（列式内存格式）空类型/字符串差异，以及兼容风险数值类型只在 DuckDB 内存查询中按字段名规范化，源文件和摘要保持不变。

`data/official-summary.csv` 继续作为聚宽页面展示口径的交叉校验证据，不替代六表高精度明细，也不进入核心分析计算。`attribution_log-<sha256>.parquet`、`records`、日志、性能剖析和 `raw/` 保持可选扩展或归档证据，不要求所有策略共享字段。

### 10.3 本地结果向聚宽口径适配

本地 vectorbt 适配器承担全部兼容成本：每个场景写入 `.local/quant-research/strategy-003/<run_id>/backtests/<local_backtest_id>/`，从该层向内镜像聚宽现有 `manifest.json`、`code.py`、`params.json`、`params_versions/` 与 `data/` 布局。`results`、`balances`、`positions`、`orders` 的文件位置、字段名称、时间含义、方向、数量、价格、费用、现金和持仓语义对齐现有聚宽结果。

本地清单沿用聚宽顶层概念和数据集条目的组织方式，但使用独立 `local-backtest-manifest.schema.json`，固定 `schema_version=local-backtest/1`、`object.kind=local_backtest`、`source.kind=local_vectorbt`、`authority=local_research`。读取器按 `schema_version` 严格选择聚宽或本地 Schema，未知版本、混合字段或失败后回退一律拒绝。清单把 `risk` 与 `period_risks` 标为 `required=false`、`status=missing_at_source`、`reason=computed_by_strategy_analysis`，避免本地流程计算 Alpha/Beta、Sharpe、回撤或分期风险。本地 `results.parquet` 保留与聚宽一致的三个物理字段：`returns` 为累计净收益，`benchmark_returns` 为全空但保持 `double` 的来源缺失参考；清单记录 `missing_at_source/independent_benchmark_set`，禁止填零或选择任一双基准冒充。海龟专属状态、风险原因、公司行动应用记录和事件必须写入 `data/attribution_log-<sha256>.parquet`；其 `event_id` 为唯一主键，字段和 `turtle-etf-attribution/2` 原因码受项目契约约束，缺失或无法覆盖实际订单、风险变化及公司行动应用时阻止完成。没有真实来源的聚宽 URL、远端原始响应、围栏、官方摘要、官方风险文件和 `raw/` 一律不生成。

### 10.4 单场景 Skill 与主代理复数调用

本地研究 Skill 每次只运行一个明确场景，并产生一份聚宽口径兼容结果与单场景冷/热性能证据；完成后输出 `complete` 与 `next_action=return_to_caller`。它不接收候选数组、不知道七方案数量、不生成聚合清单，也不生成绩效、Alpha/Beta（超额收益/市场暴露）、归因、稳健性矩阵、挑战筛选、报告、推荐或人工决定。

冻结基线和六项预设挑战先在 `preparation_id` 下展开，再由主 agent 从策略自有 `analysis-plan.json` 读取后分别调用 Skill 七次。主 agent 显式登记七个 `run_id`；分析入口验证同一代码摘要、同一 `snapshot_id`、同一 vectorbt 执行后端、七个独立场景身份、结果摘要和性能证据，再派生独立 `analysis_id` 并生成 `source-results.json`。增加、删除或重排场景只改变分析计划和主 agent 调用列表，不改变 Skill 契约。

一次本地研究调用至少输出：

```text
backtests/<local_backtest_id>/
  manifest.json + code.py + params.json + params_versions/ + data/<四类共同事实 Parquet>
```

### 10.5 本次独立确定性分析、Vibe 安全边界与人工确认

本变更由 `strategy-003/research/analysis-plan.json` 以 `strategy-analysis-plan/1` 机器可读定义一个冻结基线、六个挑战和完整稳健性矩阵；通用 `quant_analysis` 只按 `analysis-plan.schema.json` 校验和展开，不解析 Markdown、不导入海龟模块、不硬编码海龟资产、参数、分组、数量和门槛。本地研究 Skill 不读取该计划。主 agent 先创建 `preparation_id`，从计划读取七个基础场景并分别调用 Skill；七份来源明确登记并通过共享身份校验后才创建最终 `analysis_id`。固定时期与季度滚动窗口使用基线既有路径切片；逐 ETF 和逐资产组删除使用贡献删除且不重新分配资金；成本和延迟使用一阶订单级敏感性；区块抽样、历史压力、持仓冲击和 CVaR 从来源事实确定性计算。所有七个来源只通过 `source-results.json` 聚合，不写回单场景运行。

双基准独立保存在 `.local/market-data/benchmark-sets/<benchmark_set_id>/`，只包含沪深300人民币总回报和纳斯达克100人民币总回报，并记录币种、汇率、来源、日期和摘要；聚宽 `results.benchmark_returns` 仅作平台单基准参考。确定性分析完成收益、回撤、仓位与风险、Alpha/Beta、多维归因、完整稳健性、挑战结果、反对证据、不确定性、报告和推荐。Vibe-Trading（氛围量化）的研究目标和证据登记只作审计编排；加载 `performance-attribution`（绩效归因）、`risk-analysis`（风险分析）和 `report-generate`（报告生成）方法文档不等于实际分析。只允许调用无已知缺陷的单体公开入口；禁止 `run_swarm`（运行群体分析）、Vibe 回测和存在前视偏差风险的组合优化器。

若安全单体 Vibe 入口只能读取 CSV（逗号分隔文件），独立分析步骤只能从六表内存查询按明确字段和日期范围临时物化，记录查询版本和字节摘要；确认读取后立即删除。临时 CSV 不形成第二事实源。Vibe 单体能力不可用时记录 `evidence_insufficient`，不得转用群体分析，也不得反向改变本地 `run_id`、来源清单、报告或推荐状态。

独立策略分析至少输出：

```text
.local/strategy-analysis-preparations/<preparation_id>/
├── analysis-scenarios.json
├── preparation.json
└── scenario-configs/<scenario_id>/
    ├── params.json
    └── run.json

.local/strategy-analysis/<analysis_id>/
├── analysis-scenarios.json
├── preparation.json
├── source-results.json
├── deterministic-analysis.json
├── evidence-matrix.parquet
├── local-strategy-analysis-report.md
├── vibe-evidence.json
└── recommendation.json
```

`vibe-evidence.json` 记录 Vibe 研究目标标识、实际调用能力、安全边界、证据状态和临时 CSV 清理结果。任何群体分析调用必须标记 `valid_evidence=false` 和 `excluded_from_conclusions=true`。`recommendation.json` 的建议值固定为 `proceed_to_joinquant`、`revise_and_reassess` 或 `stop_evidence_insufficient`，并列出推荐基线行动、挑战关注项、确定性理由、反对证据、不确定性、阻断项及“不是聚宽正式回测或最终验收结论”的声明。

完整独立分析通过后输出 `next_action=human_confirmation_required`，等待用户人工确认。确认前不得启动聚宽、替换基线、修改参数、冻结策略或启动模拟交易。分析产物缺失或摘要不匹配不能改变已完成的本地数据包交付，只能把独立分析标记为 `evidence_insufficient` 或 `failed`。

## 11. 状态与错误处理

运行状态且只能是：

| 状态 | 使用条件 |
|---|---|
| `evidence_insufficient` | 真实策略身份、快照、来源、字段、范围或声明输入在项目执行前不完整 |
| `failed` | 已有文件摘要或内容不一致、结构/类型/重复键违规、批次冲突、项目进程异常、硬约束突破、同输入不同输出或远端临时文件清理不可确认 |
| `complete` | 本次单场景输入门禁、冷/热两次执行及摘要一致性、180秒门槛、`performance.json`、一份聚宽口径兼容结果、清单和原子固化全部通过 |

不得把部分结果标记为完成，也不得用零值、旧数据或默认口径继续。本地运行状态与后续策略分析状态相互独立，不得合并或反向改写。

## 12. 测试与验收

### 12.1 共用脚本单元测试

- 配置、仓库路径、参数数组和输出边界；
- CSV 暂存导入、Parquet 批次身份、逻辑与字节摘要、字段、日期、空值、重复键和布尔规范化；
- 公司行动结构、版本状态、公告与应用时间语义、知识截止日、来源摘要、原始价格/`pre_close`/连续因子勾稽，以及无法解释除权时关闭运行；
- 相同内容去重、冲突重叠拒绝、新标的追加和旧快照不变；
- 快照清单、内存 DuckDB 视图和规范化摘要；
- 现有聚宽 `manifest.json`、六类核心 Parquet、合法空表、文件摘要和跨表勾稽的 0 改动读取；
- 物理列顺序、`cancel_time` 空类型/字符串和兼容风险数值类型只在内存规范化，不改源文件；
- 本地 vectorbt 输出四类共同事实，官方风险参考显式缺失，字段和业务语义与聚宽现有结果兼容；
- 本地 `results.benchmark_returns` 为全空 `double` 且清单明确来源缺失；双基准只来自独立基准集；
- 通用分析计划 Schema 与 `strategy-003` 的机器可读计划可以校验、展开和复算，Skill 不读取计划；
- 海龟归因日志字段、唯一主键、原因码版本、覆盖范围和必需清单条目；
- 本地研究依赖图不导入 `quant_analysis` 算法或策略分析 Skill；
- `run_id`、原子固化、幂等复用、失败重试和确定性冲突；
- 三态唯一收口和敏感信息清理。

共用测试不得出现海龟资产、参数或规则常量。

### 12.2 海龟规则与不变量测试

- 55/20 日通道排除当日、TR、20 日 N、U0 和次日执行；
- 固定 0.5N 档位、每日一次、共同止损只上移；
- 资金、单 ETF、资产组、计划风险和波动率上限；
- 海龟输入、配置、回调和原因码不含成交额或流动性规则；不同成交额输入不改变任何海龟订单；
- 拆分与现金分红前后连续经济权益不产生机械跳变；经济单位保持稳定，现金分红按除权日隐含再投资且支付日不重复增加现金；
- 本地清单完整声明研究级近似，统一读取器和报告不得把经济单位、近似现金或订单路径描述成聚宽精确账户；
- 单 ETF 或资产组被动超限不冻结其他证券，只禁止同一证券或同一组继续增加；
- 退出、强制减仓、同级买入顺序；
- A1 同比例、整手、小数余额、代码同分和输入乱序；
- 现金不为负、硬上限不突破、相同输入输出摘要一致；
- 停牌、涨跌停、不可成交、60 样本冷启动和风险输入故障安全。

### 12.3 vectorbt 接线、规则夹具与性能测试

- 依赖锁定、代码身份和 vectorbt 1.1.0 兼容版本；
- `SimulationInputs` 的日期/证券对齐、稳定类型、只读数组、原始与连续经济 OHLC、公司行动核对时点、累计连续因子和 T 日到 T+1 显式错位，并证明因子不读取晚公布元数据且不存在成交额或流动性输入；
- `Portfolio.from_order_func()`、单一共享现金组和四类官方回调实际被调用；
- 回调在 Numba `nopython`（无 Python 模式）下编译，不回落对象模式；
- 退出、风险减仓、卖出实际成交后 A1、买入和 `post_order_func_nb` 成交回填顺序；
- 普通订单函数每 ETF 每日最多一单，未启用 `flexible=True`；
- 逐日订单、成交、现金、持仓、批次、共同止损、风险原因和摘要满足规格夹具的明确预期；
- vectorbt 官方记录转换后满足四类共同事实字段语义、海龟必需归因日志、清单证据与跨表勾稽，不预计算策略分析指标；
- 旧 `execution.py`、`state.py`、`signals.py`、`risk.py`、`allocation.py`、`reporting.py`、专用测试和公开导出已经删除；全仓不存在 `process_day`、旧 `_simulate`、旧模块导入、兼容层、双引擎开关、回退或无效代码；
- 分别记录 vectorbt 冷启动 JIT 和预热执行时间，不运行旧完整流程作为性能比较。

日常小夹具测试不以微秒级波动判定失败。真实性能验收在同一暂存区保存 vectorbt 的环境、准备后输入摘要、首次 JIT、预热执行、四类共同事实、海龟归因日志校验和冷/热结果摘要；主 agent 的每次真实单场景调用都在全新进程先测冷启动，再在同一已编译进程测预热。两次均从已准备输入进入 vectorbt 开始，到交易执行、四类共同事实、海龟必需归因日志及其结构/摘要/勾稽校验完成时停止，均不得超过 180 秒且规范化结果摘要必须一致。停止计时后先删除预热副本和可丢弃暂存并确认清理，再把清理结果写入 `performance.json`、生成最终清单并校验全部摘要；最后只原子发布已整理的权威结果目录，发布后不依赖写入或清理。复数调用整体耗时和独立分析时间另记，但不替代单次门禁。

### 12.4 完整 E2E

本地流程完整 E2E（端到端）从 Skill 用户入口启动，使用固定小型日线夹具，经过 CSV 暂存、Parquet 批次、快照、内存 DuckDB、通用运行器、`strategy-003` 适配器、vectorbt 官方回调、收盘信号、次日订单、持仓/现金/风险、一份聚宽口径兼容结果、`next_action=return_to_caller` 和不可变证据收口。测试明确断言 Skill 不接收候选数组、不内部循环，也未调用绩效归因、稳健性、报告、Vibe 或人工决定。主 agent 集成 E2E 另行调用七次并在分析目录聚合。另以仓库现有聚宽回测目录执行只读 E2E，断言运行前后 Git（版本管理）状态与全部文件摘要不变。

另用不含海龟词汇和资产的最小项目适配器执行同一 E2E，证明 Skill、行情中心和运行器未反向依赖海龟。常规自动测试不访问网络、不加载历史 `.local` 数据，并在临时目录结束后清理。

### 12.5 真实集成验收

使用已经验证的 11 只 ETF 全历史 Parquet 快照；只有数据或公司行动不完整时才重新调用聚宽研究环境导出。先验收固定字段、原始未复权口径、公司行动来源/元数据核对时点/勾稽、连续因子只依赖当日行情事实、连续总回报经济价格、研究级近似声明、传输摘要、Parquet 共享批次、快照、内存视图、零海龟流动性规则、vectorbt 代码身份、单场景聚宽口径兼容结果和每次冷/热不超过 180 秒。随后由主 agent 在 `preparation_id` 下分别完成七个基础调用，显式登记并校验七份共享身份，在本地流程之外派生不可变 `analysis_id`，通过同一读取入口和双基准集完成确定性绩效、归因、稳健性挑战、报告和推荐；报告必须把收益与回撤视为研究级总回报近似，披露晚公布公司行动只用于事后核对，并把现金、仓位、订单和聚宽差异列为不确定性。最后确认 Vibe 安全边界证据、输入摘要与临时 CSV 清理结果，并停在人工确认门禁。

最终执行 `quick_validate.py`、仓库布局测试、Build and Verify（构建与验证）完整检查、OpenSpec 严格校验和公开仓库敏感数据扫描。

## 13. 实施顺序与回滚

1. 保留已验证的 `strategy-003` 身份、Parquet 行情和历史运行证据；先以现有聚宽清单和六表建立只读 `analysis_data` 契约，并固定聚宽目录 0 改动门禁。旧逐日实现仅在规则迁入期间作为只读来源，不执行完整对照。
2. 先用真实最小样例验证公司行动来源，并保存 vectorbt 1.1.0 开源公开接口不能原生处理拆分与现金分红的实测证据；按用户确认采用只依赖当日原始行情事实的连续总回报近似，公司行动元数据仅作时点可知或事后核对，不使用私有引擎、伪订单或价格阈值猜测。
3. 以 TDD（测试驱动开发）固定 vectorbt 1.1.0 和兼容依赖，建立不含任何成交额或流动性参数的 `SimulationInputs`、连续经济价格/经济单位、Numba 数值内核和官方四类回调，逐项通过公司行动、精度声明、前视偏差、顺序、A1、被动超限、风险和成交状态测试。
4. 将 vectorbt 记录适配为四类共同执行事实、本地清单、`performance.json` 和海龟必需归因日志，显式声明官方风险参考缺失；规则夹具全部通过后删除旧五个执行/规则模块、流动性执行规则、`reporting.py`、旧专用测试和公开导出，不建立兼容层。
5. 全仓扫描旧符号、旧导入、分析耦合、兼容开关和回退，确认项目只剩 vectorbt 唯一执行入口；从 Skill 公开入口执行单场景 E2E，并验证一次调用只产生一个结果且冷启动和预热都不超过 180 秒。
6. 建立通用分析计划 Schema 和策略自有 `analysis-plan.json`；主 agent 使用真实 11 只 ETF 快照读取计划并分别调用 Skill 七次。其他稳健性场景只从基线已有事实确定性计算，不再调用 Skill；七份回测来源和派生证据只在独立分析目录聚合，Skill 不读取计划且契约不包含数量或循环。
7. 在本地流程之外使用确定性分析处理上述数据包，生成独立报告、Vibe 安全边界证据和推荐，清理临时 CSV，等待人工确认。
8. 完成非海龟 E2E、仓库完整验证、独立前向验证、OpenSpec 严格校验和敏感数据扫描。

迁移失败时停止在当前分支并保留性能、语义或依赖失败证据，不通过提高超时、恢复旧源码、增加兼容层或维护双生产引擎掩盖问题。不得删除已验证不可变批次、快照或历史运行证据，不得修改或删除 `strategy-001`、`strategy-002`，不得把功能分支本地合入主干。未固化暂存和未引用文件只有在确认不属于任何清单后才可清理。

## 14. 官方接口依据

- vectorbt `Portfolio`（组合）官方文档：<https://vectorbt.dev/api/portfolio/base/>
- vectorbt Numba 回调官方文档：<https://vectorbt.dev/api/portfolio/nb/>
- vectorbt 1.1.0 官方许可：<https://github.com/polakowo/vectorbt/blob/v1.1.0/LICENSE.md>

官方文档把 `from_orders()`（预生成订单）、`from_signals()`（信号模拟）和 `from_order_func()` 列为三种主要模拟方式，并把 `from_order_func()` 定义为支持任意回调逻辑的最强模式。本设计只使用这些公开扩展点，不依赖未声明的私有实现。
