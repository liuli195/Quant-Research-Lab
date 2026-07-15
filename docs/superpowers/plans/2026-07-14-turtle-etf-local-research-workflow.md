---
change: build-turtle-etf-local-research-workflow
design-doc: docs/superpowers/specs/2026-07-14-turtle-etf-local-research-workflow-design.md
base-ref: 4400fec8149f02bc7d42f0294be65e9dacc9b639
---

# 海龟 ETF 本地研究流程、聚宽原生分析数据与 vectorbt 执行内核实施计划

> **给执行 Agent（代理）：** 必须按任务逐项使用 `superpowers:executing-plans`（计划执行），代码任务遵循 TDD（测试驱动开发）的 RED（失败）→ GREEN（通过）→ REFACTOR（重构）循环。不得执行或恢复已废弃的旧逐日方案。

## 目标与边界

- 本地研究 Skill（技能）每次只编排一个快照、一个场景、一份聚宽口径兼容结果和不可变证据，并以 `next_action=return_to_caller` 停止；它不接收候选数组，也不知道冻结基线和六个挑战这一组合。
- JoinQuant（聚宽）现有回测目录、清单和归档流程保持零改动；它们是标准物理基准。
- 本地 vectorbt（向量化回测框架）结果从 `<run_id>/backtests/<local_backtest_id>/` 向内尽量对齐聚宽目录，但使用独立本地 Schema（结构约束）明确身份。
- 本地物理落盘只有 `results`、`balances`、`positions`、`orders` 四类共同执行事实；`risk`、`period_risks` 只作为缺失的来源参考条目。统一读取器提供六类逻辑视图。
- 策略分析 Skill 另立变更。本变更只在本地 Skill 外使用通用确定性分析脚本完成一次真实、完整的独立验收，不创建策略分析 Skill；Vibe-Trading（氛围量化）仅记录安全单体能力与边界审计，禁止群体分析。
- 单次回测性能门槛为180秒。主 agent（代理）每调用一次 Skill，都必须为该单场景提供冷启动与预热证据；两次执行和摘要一致性在暂存区通过后才发布一份权威结果。
- 聚宽正式复核、聚宽回测 Skill、策略分析 Skill、规则冻结、模拟交易和实盘不在本变更范围。

仍有效的现有基础只有三项：真实 `strategy-003` 身份、共享 Parquet（列式文件）行情中心、通用本地研究 Skill 与三态证据。旧逐日执行模块、旧八表物理契约、流程内分析/报告/人工门禁不属于当前方案，不能作为任务、兼容接口或验收依据保留。

## 固定输出

本地基础研究：

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

共享分析基准：

```text
.local/market-data/benchmark-sets/<benchmark_set_id>/
├── manifest.json
└── benchmark-returns.parquet
```

独立分析验收：

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

## 全局约束

- 所有本地 Python（编程语言）命令使用 `.\.venv\Scripts\python.exe`；依赖变更必须先明确记录并经用户授权，不得静默安装。
- 完整行情和研究结果只写入已忽略的 `.local/`；不得提交行情值、账号、Token（访问令牌）或 Cookie（浏览器凭证）。
- `market-data.parquet` 是行情事实源；DuckDB（嵌入式分析数据库）只连接 `:memory:`。
- 共享批次以原始 `market-data.parquet` 和 `corporate-actions.parquet` 共同构成行情事实；连续信号价格只在内存派生。无法解释的除权差异必须关闭运行。
- 海龟策略层完全不读取成交额，不设置最低成交额、单笔成交额占比、订单参与率或其他流动性规则；共享成交额字段只供上游 ETF 池筛选和其他策略使用。
- vectorbt 只属于 `strategy-003` 项目执行层；共享行情、统一读取、通用运行器和分析算法不得依赖 vectorbt 对象。
- 所有 T 日收盘信号和滚动输入必须显式错位到 T+1 执行行；海龟状态只按实际成交更新。
- 当日顺序固定为退出、强制风险减仓、A1 买入；卖出实际成交后才能按最新现金和持仓计算一次 A1。
- 冻结基线与六个预设挑战全部保留，由主 agent 从策略自有机器可读 `analysis-plan.json` 读取并分别调用 Skill 七次；Skill 不读取该计划，本身不得包含数量、顺序或循环逻辑。
- `preparation_id` 只绑定分析计划、基准集、运行模板和七份待执行配置；七次调用完成后，主 agent 必须显式登记 `scenario_id -> run_id`，校验七份结果共享快照、代码和执行后端，再由全部来源摘要派生不可变 `analysis_id`。不得扫描目录猜测来源或覆盖同计划下的旧分析。
- 旧实现通过新路径验收后直接删除；不运行旧完整流程，不新旧双跑，不保留兼容模块、导入别名、双引擎开关、回退或死代码。
- 确定性指标、稳健性数值、证据挑战、报告和推荐均由统一读取与 `quant_analysis`（量化分析）完成；Vibe 不执行回测、不替代数值裁判。加载方法文档不算实际分析，已知缺陷的群体分析不得调用或进入结论。

---

## Task 1：锁定 vectorbt 依赖、输入与官方回调

**对应 OpenSpec：** 2.1—2.4

**Files：**

- Modify: `pyproject.toml` 或仓库现有依赖锁定文件
- Create: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_inputs.py`
- Create: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_callbacks.py`
- Create: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py`
- Create: `tests/local_quant_research/test_turtle_vectorbt_inputs.py`
- Create: `tests/local_quant_research/test_turtle_vectorbt_callbacks.py`
- Create: `tests/local_quant_research/test_turtle_vectorbt_engine.py`
- Modify: `joinquant/strategies/strategy-003/research/code-identity.json`
- Modify only if affected mapping requires: `.build-and-verify/config.json`

**接口：**

- `prepare_simulation_inputs(frames, config) -> SimulationInputs`
- `run_vectorbt_simulation(inputs, config) -> VectorbtSimulationResult`
- 回调固定使用 `pre_sim_func_nb`、`pre_segment_func_nb`、`order_func_nb`、`post_order_func_nb`

### Step 1：RED

先写失败测试，覆盖依赖版本与许可记录、稳定数组类型、T→T+1 错位、无未来数据、单一共享现金组、普通订单函数、卖出优先、成交后 A1、成交后状态更新、停牌/涨跌停/拒单和 `nopython`（无 Python 模式）。

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_inputs.py tests\local_quant_research\test_turtle_vectorbt_callbacks.py tests\local_quant_research\test_turtle_vectorbt_engine.py -q
```

Expected: FAIL，原因是 vectorbt 新入口尚未形成唯一执行路径。

### Step 2：GREEN

在获得依赖变更授权后，使用项目 `.venv` 固定实测兼容版本。所有 ETF 使用一个 `cash_sharing=True`（共享现金）组；不得启用 `flexible=True`（灵活多订单）或私有模拟函数。实现已确认的海龟状态、共同止损、风险门槛与 A1，并用固定合成夹具直接验证订单、成交、现金、持仓、批次和原因码。

### Step 3：回归

运行本任务测试和现有海龟规则测试。测试预期只来自规格夹具，不调用旧 `process_day` 或旧 `_simulate` 生成答案。

---

## Task 2：建立双 Schema、统一读取和双基准契约

**对应 OpenSpec：** 3.1—3.4

**Files：**

- Create: `scripts/research/analysis_data/schemas/local-backtest-manifest.schema.json`
- Create: `scripts/research/analysis_data/__init__.py`
- Create: `scripts/research/analysis_data/manifest.py`
- Create: `scripts/research/analysis_data/views.py`
- Create: `scripts/research/analysis_data/derived.py`
- Create: `scripts/research/analysis_data/cli.py`
- Create: `scripts/research/market_data/benchmark_sets.py`
- Modify: `scripts/research/quant_analysis/benchmarks.py`
- Delete: `scripts/research/quant_analysis/contracts.py`
- Create: `tests/local_quant_research/test_analysis_manifest_schemas.py`
- Create: `tests/local_quant_research/test_analysis_data_contract.py`
- Create: `tests/local_quant_research/test_benchmark_set_contract.py`
- Read only fixture: `joinquant/strategies/strategy-001/backtests/111/`
- Read only fixture: `joinquant/strategies/strategy-001/backtests/109/`
- Read only fixture: `joinquant/strategies/strategy-002/backtests/9/`

**接口：**

- `open_analysis_source(result_dir: Path) -> AnalysisSource`
- `validate_analysis_source(source: AnalysisSource) -> ValidationResult`
- `register_core_views(connection, source) -> CoreViews`
- `build_derived_views(connection, views) -> DerivedViews`
- `build_benchmark_set(definitions, source_snapshots, output_root) -> BenchmarkSet`

### Step 1：RED——本地清单 Schema

本地清单必须使用：

```text
schema_version = "local-backtest/1"
object.kind = "local_backtest"
source.kind = "local_vectorbt"
authority = "local_research"
```

Schema 必须固定代码、参数、运行、场景、快照、引擎版本、六类数据集条目、`performance.json` 路径/字节数/SHA256（文件摘要）、文件摘要、行数、空表和门禁。四类共同事实必须 `complete`；`risk` 与 `period_risks` 必须 `required=false`、`status=missing_at_source`、`reason=computed_by_strategy_analysis`。本地清单禁止出现聚宽 URL、`research_response`、`research_lineage`、`collection_fence` 或 `official_summary`。

测试还要覆盖未知版本、`local_backtest` 冒充聚宽版本、聚宽字段混入本地清单、本地字段混入聚宽清单、Schema 失败后回退另一分支等拒绝场景。

### Step 2：RED——聚宽零改动与六类逻辑视图

读取器只按顶层 `schema_version` 选择契约：整数 `1` 使用现有聚宽 Schema；字符串 `local-backtest/1` 使用本地 Schema；其他值直接拒绝。聚宽路径验证原 `manifest.json`、对象、来源、门禁、文件摘要和合法空表，运行前后目录摘要必须一致。

聚宽来源读取六类物理数据集；本地来源读取四类物理事实，并为 `risk`、`period_risks` 建立带来源缺失状态的空参考视图。两个来源最终都暴露六类逻辑视图；权益、完整往返交易和事件只在查询期派生。

真实只读核对已确认聚宽 `results` 的 `time:string`、`returns:double`、`benchmark_returns:double` 均表示累计序列。本地 `results.parquet` 也固定这三个字段：`returns` 为从初始资金起算的累计净收益，`benchmark_returns` 全列为空但物理类型必须保持 `double`，清单记录 `source_benchmark_returns.status=missing_at_source`、`reason=independent_benchmark_set` 和空值行数。禁止填零或任选双基准之一冒充聚宽单基准。

统一读取器把 `results.time` 规范化为 Asia/Shanghai（亚洲/上海）交易日，并按 `(1 + cumulative_return_t) / (1 + cumulative_return_t-1) - 1` 在查询期派生策略单日收益；首样本累计收益不为零时因缺少前值而排除。双基准文件保存单日人民币总回报，分析只能在共同有效交易日比较单日序列，不得把来源累计收益直接与基准单日收益比较。

### Step 3：RED——双基准集

固定仅有：

- `CSI300_CNY_TOTAL_RETURN`：沪深300人民币总回报；
- `NASDAQ100_CNY_TOTAL_RETURN`：纳斯达克100人民币总回报，包含美元兑人民币变化。

`benchmark-returns.parquet` 至少包含 `time`、`benchmark_id`、`returns`。清单记录币种、总回报定义、汇率公式、源标识、实际日期范围、底层快照与文件摘要。实现前必须对配置的数据源做真实最小可行性验证；覆盖不足、来源不明或汇率口径不完整时输出 `evidence_insufficient`，禁止使用 ETF 代理或零收益补齐。

聚宽 `results.benchmark_returns` 只注册为 `source_benchmark_returns` 官方参考；除非其清单能证明与目标基准身份和口径完全一致，否则不能代替上述两条分析基准。

### Step 4：GREEN 与回归

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_analysis_manifest_schemas.py tests\local_quant_research\test_analysis_data_contract.py tests\local_quant_research\test_benchmark_set_contract.py tests\local_quant_research\test_analysis_contracts.py -q
```

Expected: PASS；聚宽目录零改动，本地清单有独立可执行契约，双基准可复算且不污染来源回测目录。

---

## Task 3：适配本地结果、切换唯一入口并删除旧方案

**对应 OpenSpec：** 4.1—4.4

**Files：**

- Create: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_adapter.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/cli.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/__init__.py`
- Modify: `scripts/research/local_quant_research/contract.py`
- Modify: `scripts/research/local_quant_research/runner.py`
- Create: `tests/local_quant_research/test_turtle_vectorbt_contract_execution.py`
- Create: `tests/local_quant_research/test_turtle_attribution_contract.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/execution.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/state.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/signals.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/risk.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/allocation.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/reporting.py`
- Delete old-only tests after replacement coverage exists

**接口：**

- `to_joinquant_facts(inputs, simulation, scenario_id) -> LocalExecutionFacts`
- `write_local_result(backtest_dir, manifest, performance, facts, attribution) -> LocalResultPackage`
- `run_candidate_with_vectorbt(frames, config, candidate_id, output_dir) -> LocalResultPackage`

### Step 1：RED

测试目录、清单 Schema、代码与参数摘要、`performance.json`、四类物理事实字段和跨表勾稽。对 `strategy-003` 强制 `attribution_log-<sha256>.parquet`，固定 `event_id` 唯一主键、字段、`turtle-etf-attribution/2` 原因码版本和最小原因码集合，并记录公司行动应用；缺失、摘要错误、未知原因码或无法覆盖实际订单、风险状态变化及公司行动应用时拒绝完成。明确断言不存在 `data/risk.parquet`、`data/period_risks.parquet`、`data/equity.parquet`、`data/trades.parquet` 或本地 `raw/` 伪证据。

### Step 2：GREEN

每次调用只执行传入的一个场景，通过 vectorbt 唯一入口把一份结果写入 `<run_id>/backtests/<local_backtest_id>/`。完成后输出 `next_action=return_to_caller`，不得读取其他候选、循环调用自身、生成候选/聚合清单或调用 `quant_analysis`、Vibe、报告和推荐。冻结基线与六个挑战由主 agent 分别调用七次。

### Step 3：删除旧方案

新规则夹具、适配和公开入口通过后，在同一任务中删除旧模块、旧导出与旧专用测试。扫描必须确认不存在：

```text
process_day
def _simulate
旧 execution/state/signals/risk/allocation/reporting 导入
旧八表物理契约
流程内 quant_analysis 或 Vibe 调用
兼容层、双引擎和回退
```

### Step 4：回归

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_contract_execution.py tests\local_quant_research\test_analysis_manifest_schemas.py tests\local_quant_research\test_analysis_data_contract.py tests\local_quant_research\test_runner.py -q
```

---

## Task 4：逐场景验证单次回测不超过180秒

**对应 OpenSpec：** 5.1—5.3

**Files：**

- Create: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_benchmark.py`
- Create: `tests/local_quant_research/test_turtle_vectorbt_performance_contract.py`
- Create: `tests/local_quant_research/test_turtle_vectorbt_e2e.py`
- Runtime failure evidence: `.local/quant-research/strategy-003/.attempts/<attempt_id>.json`

**接口：**

- `benchmark_scenario(scenario_config, prepared_inputs, output_dir) -> PerformanceEvidence`

### Step 1：固定计时方法

每个场景通过一个全新子进程启动。子进程中：

1. 读取已经准备好的输入，并创建一个尚不可见为完成结果的暂存区；
2. 第一次执行 vectorbt、写四类事实与海龟归因日志并完成校验，记为 `cold_seconds`；
3. 在同一已编译进程再次执行同一场景到暂存子目录，完成相同校验，记为 `warm_seconds`；
4. 比较冷/热规范化结果摘要，要求完全一致且两次均不超过180秒；
5. 保留冷启动权威候选目录，先删除预热副本和所有可丢弃暂存并验证清理结果；
6. 把已验证清理结果写入 `performance.json`，再生成并校验最终本地清单和全部摘要；
7. 只把已整理好的权威候选目录原子发布；发布后不再依赖任何写入或清理动作。任一前置门禁失败只留下 attempt（尝试）证据。

每次计时都从“准备后输入交给 vectorbt 执行内核”开始，到“交易执行、四类共同事实、海龟必需归因日志及其结构、摘要和跨表勾稽校验完成”时停止。停止计时后才写入并校验 `performance.json` 和最终本地清单；二者属于原子完成门禁，但不计入 `cold_seconds`、`warm_seconds`。行情导出、输入准备和独立策略分析同样不计入单次回测门槛。

### Step 2：逐场景证据

主 agent 只对冻结基线和六个挑战分别调用 Skill，共七次；每次调用都必须保存环境、依赖、输入、代码、参数、场景、冷/热结果摘要，以及 `cold_seconds`、`warm_seconds`。摘要不一致或任一数值超过180秒即失败，不得使用多次调用的整体墙钟时间替代，也不得先发布 `complete` 目录后补性能或清理证据。

### Step 3：公开入口 E2E

从 Skill 文档命令启动一个单场景完整 E2E，验证只产生一份结果、一次运行证据、逐场景性能证据、原子固化、`next_action=return_to_caller` 和临时产物清理，并明确断言没有候选数组或内部循环。另由主 agent 的集成 E2E 连续调用七次并在独立分析目录生成 `source-results.json`；聚宽归档只读 E2E 前后全部文件摘要与 Git 状态必须一致。

---

## Task 5：在本地 Skill 外完成完整稳健性、归因与确定性报告

**对应 OpenSpec：** 6.1—6.5

**Files：**

- Modify: `scripts/research/quant_analysis/benchmarks.py`
- Modify: `scripts/research/quant_analysis/robustness.py`
- Modify: `scripts/research/quant_analysis/cvar.py`
- Modify: `scripts/research/quant_analysis/evidence.py`
- Create: `scripts/research/quant_analysis/analysis_plan.py`
- Create: `scripts/research/quant_analysis/orchestration.py`
- Create: `scripts/research/quant_analysis/unified_analysis.py`
- Create: `scripts/research/quant_analysis/reporting.py`
- Create: `scripts/research/quant_analysis/schemas/analysis-plan.schema.json`
- Create: `joinquant/strategies/strategy-003/research/analysis-plan.json`
- Create: `tests/quant_analysis/test_analysis_plan.py`
- Create: `tests/quant_analysis/test_unified_analysis.py`
- Create: `tests/quant_analysis/test_reporting.py`
- Create: `tests/quant_analysis/test_statistics.py`
- Runtime only: `.local/strategy-analysis-preparations/<preparation_id>/`、`.local/strategy-analysis/<analysis_id>/`

### Step 1：生成完整且封闭的场景矩阵

`strategy-003/research/analysis-plan.json` 是策略自有的机器可读分析定义，固定 `schema_version=strategy-analysis-plan/1`。顶层包含 `strategy_id`、`baseline_config`、七个 `scenarios`、`universe`、`analyses`、`expected` 和 `thresholds`；每个基础场景包含唯一 `scenario_id`、`dimension` 和结构化 `overrides`，其余分析包含日期、资产、成本、抽样、冲击、门槛或固定种子等确定性配置。通用 `quant_analysis` 只按 `analysis-plan.schema.json` 校验并展开 `analysis-scenarios.json`，不得解析 Markdown、导入海龟模块或硬编码海龟资产、参数、分组、数量和门槛。本地研究 Skill 不读取该文件。

展开后的 `analysis-scenarios.json` 必须记录计划摘要和版本，逐项分类：

| 分析维度 | 执行方式 |
|---|---|
| 冻结基线与六个参数邻域 | 引用主 agent 七次独立调用结果 |
| 三个固定时期 | 基线既有路径切片 |
| 三年滚动窗口、每季度移动 | 基线既有路径滚动切片 |
| 逐只删除11只 ETF | 删除对应收益贡献，不重新分配资金 |
| 逐组删除资产组 | 删除对应收益贡献，不重新分配资金 |
| 成本与延迟执行场景 | 一阶订单级敏感性估算 |
| 5/20/60日区块抽样各10,000条 | 从基线收益确定性计算 |
| 五个历史压力窗口 | 从基线收益、持仓和事件视图计算 |
| 四个持仓冲击 | 从每日实际持仓确定性计算 |
| 95%/99%及5日 CVaR | 从基线收益确定性计算 |

`analysis-plan.json` 与场景矩阵必须给出七个来源的期望数量、实际数量、参数摘要和输入范围，以及每项派生稳健性的计算方法。缺一项或摘要不一致即 `evidence_insufficient`，不得用 Vibe 补齐。

### Step 2：执行七个基础场景

独立分析准备入口只校验策略分析计划并在 `preparation_id` 下生成七份通用单场景配置，不导入海龟模块或直接循环本地运行器。主 agent 对每份配置调用一次本地研究 Skill；配置由 `strategy-003` 项目入口解释并使用同一 vectorbt 回调执行，结果保存在新的 `.local/quant-research/strategy-003/<scenario_run_id>/backtests/<local_backtest_id>/`。七次调用后，主 agent 显式传入七组 `scenario_id=run_id`；分析入口逐项校验唯一运行、参数摘要、结果摘要、性能证据、同一 `snapshot_id`、同一代码身份及结果清单后端。所有来源 `run_id` 保持不可变，`source-results.json` 只保存路径、摘要引用、登记表摘要和共享执行身份。

七个场景都执行Task 4的冷/热性能门禁。其余稳健性不再调用 Skill；报告必须声明时期是既有路径切片、资产删除不重新分配资金、成本和延迟是一阶估算。

### Step 3：确定性分析

通过统一读取器和双基准集计算收益、CAGR（复合年增长率）、波动率、回撤、Sharpe（夏普比率）、Sortino（索提诺比率）、Calmar（卡玛比率）、Alpha/Beta、上下行捕获、持仓与现金分布、风险预算使用、按 ETF/资产组/时期/交易原因归因，以及最终方案规定的全部稳健性、压力、CVaR和挑战门槛。

分析输出必须区分：来源事实、确定性派生值、门槛判断和 Vibe 安全边界审计。所有数值和结论由本地算法固化，Vibe 不得重新定义计算口径。

### Step 4：Vibe 安全边界审计

最终 `analysis_id` 由 `preparation_id`、七份显式来源清单/结果摘要和共享执行身份共同派生，在其目录下记录来源、基准集、配置和确定性结果摘要；相同计划下的另一批运行得到不同身份，不能覆盖旧证据。Vibe 的研究目标和证据登记只作审计编排；只允许调用无已知缺陷的单体公开分析入口。禁止 `run_swarm`（运行群体分析）、Vibe 回测和有前视偏差风险的组合优化器；没有安全单体入口时记录 `evidence_insufficient`。若安全入口只能传 CSV（逗号分隔文件），必须由统一视图按明确字段与日期临时物化，确认读取后删除并记录清理结果。

### Step 5：完整报告与推荐

必须生成收益、回撤、Alpha/Beta、仓位与风险控制、归因、六个挑战、完整稳健性、压力和尾部风险、反对证据、不确定性、推荐和工具证据。输出 `next_action=human_confirmation_required`，等待用户人工确认；不得启动聚宽、改参数、替换基线、冻结或模拟交易。

---

## Task 6：全量验证与交付

**对应 OpenSpec：** 7.1—7.3

### Step 1：删除与边界扫描

扫描并确认没有旧执行符号、旧模块、旧八表物理契约、流程内分析调用、兼容层、双引擎、回退、死测试、聚宽归档修改或转换副本。历史原因只允许存在于说明本次迁移原因的非执行文档，不能形成任务、接口或验收要求。

### Step 2：完整业务回归

使用项目 `.venv` 运行：

- 全量单元与集成测试；
- 从 Skill 用户入口贯通一次单场景行情快照、兼容结果、性能和 `return_to_caller` 状态的完整 E2E；另由主 agent 连续调用七次，证明 Skill 不含七方案耦合并聚合为独立分析来源；
- 独立分析入口贯通场景矩阵、路径重跑、双基准、确定性报告、Vibe 安全边界审计和人工确认前停止状态的完整 E2E；
- OpenSpec（开放规格）严格校验；
- Build and Verify（构建与验证）完整门禁；
- 敏感数据、临时文件和持久 DuckDB 扫描；
- 现有聚宽回测归档零改动断言；
- 独立前向验证。

不能用几个单元测试拼接代替完整入口。

### Step 3：完成报告

逐项记录已验证、无法验证、性能结果、实际分析结果、Vibe 安全边界证据和临时产物清理。只有阻断项为零才进入完成审查；本任务不提交、不推送、不创建 PR（拉取请求），除非用户另行授权。

---

## Task 7：实现行情时点可知的公司行动近似核算并重建研究证据

**对应 OpenSpec：** 8.1—8.7；8.4、8.5 已完成，本任务只实施 8.2、8.3、8.6、8.7。

**Files：**

- Modify: `scripts/research/market_data/contracts.py`
- Modify: `scripts/research/market_data/storage.py`
- Modify: `scripts/research/market_data/query.py`
- Modify: `scripts/research/market_data/joinquant_export.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_cli.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_inputs.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/result_adapter.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/single_scenario.py`
- Modify: `scripts/research/analysis_data/schemas/local-backtest-manifest.schema.json`
- Modify: `scripts/research/analysis_data/manifest.py`
- Modify: `tests/local_quant_research/test_market_data_storage.py`
- Modify: `tests/local_quant_research/test_market_data_query.py`
- Modify: `tests/local_quant_research/test_joinquant_export.py`
- Modify: `tests/local_quant_research/test_turtle_vectorbt_inputs.py`
- Modify: `tests/local_quant_research/test_turtle_result_adapter.py`
- Modify: `tests/local_quant_research/test_turtle_single_scenario.py`
- Modify: `tests/local_quant_research/test_analysis_manifest_schemas.py`
- Runtime only: `.local/market-data/`、`.local/quant-research/strategy-003/`、`.local/strategy-analysis-preparations/`、`.local/strategy-analysis/`

**固定接口与口径：**

- `normalize_corporate_action_rows(rows) -> list[dict[str, object]]` 固定事件字段、类型、主键、状态与日期语义；`corporate_action_digest(rows) -> str` 生成规范化内容摘要。
- `import_batch(csv_path, corporate_actions_csv_path, manifest, root) -> BatchRecord` 原子固化 `market-data.parquet` 与 `corporate-actions.parquet`；两类规范化内容摘要共同决定 `batch_id`，两类批次摘要共同决定 `snapshot_id`。
- `SnapshotView.corporate_actions` 与 `SnapshotView.corporate_actions_digest` 返回与行情同一快照身份的只读事件和摘要。
- `corporate-actions.parquet` 至少保存来源事件主键、证券、事件类型、公告日、登记日、除权日、生效日、支付日、状态、知识截止日、拆分比例、每份现金、来源身份和来源摘要；空事件集也必须以固定 Schema（结构约束）落盘。
- 导出器必须以取消日期相对 `snapshot_end_date` 重建事件状态；截止日后的取消在该快照中仍为有效，当前状态显示已取消但缺少取消日期时以 `evidence_insufficient` 停止，不得用当前 `process_id` 回写历史状态。
- `prepare_simulation_inputs(frames, config, corporate_actions) -> SimulationInputs` 只应用公告日在生效日之前或当日、状态有效且知识截止日完整的事件；以 `上一交易日原始 close / 当日原始 pre_close` 决定连续因子。公告日晚于应用日、事件取消或数值不能勾稽时停止为 `evidence_insufficient`。连续因子从应用日向未来累乘，绝不回写过去。
- 当公司行动能解释价格基准变化时，连续因子使用 `上一交易日原始 close / 当日原始 pre_close`；同一因子应用于当日及以后原始 OHLC（开高低收）与 `pre_close`。
- vectorbt（向量化回测框架）的信号、突破、N 值、协方差、风险、成交、估值全部使用连续经济价格与经济单位。现金分红按除权日隐含再投资，不在支付日另加现金。
- 本地结果只能声明时点可知的总回报近似，不得宣称真实拆分后份额、支付日现金、税费、真实再投资份额、零碎份额现金或聚宽订单路径精确一致。

### Step 1：8.2 RED——双事实批次与快照

先在 `test_market_data_storage.py` 添加失败测试，覆盖：

1. 有事件与空事件两种导入都生成固定 Schema 的 `corporate-actions.parquet`；
2. 事件行主键、日期先后、状态、知识截止日、拆分比例和每份现金校验；
3. 行情相同而公司行动不同会得到不同 `batch_id` 和 `snapshot_id`；
4. 两类 Parquet（列式文件）任一被篡改均拒绝读取；
5. DuckDB（嵌入式分析数据库）只用 `:memory:` 回读两类事实；
6. 没有对应有效事件授权的价格基准变化返回 `evidence_insufficient`，不使用价格阈值猜测；
7. 成功与失败路径都不遗留 CSV（逗号分隔文件）暂存或持久数据库。

Run：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_storage.py -q
```

Expected：RED，原因是当前批次只认识 `market-data.parquet`，批次与快照身份没有公司行动证据。

### Step 2：8.2 GREEN——实现双事实原子存储

在 `storage.py` 增加最小公司行动规范化、Arrow（列式内存格式）Schema、内容摘要、原子写入、完整性校验和快照引用。保留现有行情接口语义；需要兼容无公司行动的通用夹具时，调用方必须显式提供合法空事件集，不能静默假设“没有事件”。实现后重新运行 Step 1，并运行市场数据相关回归：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_storage.py tests\local_quant_research\test_market_data_query.py tests\local_quant_research\test_joinquant_export.py -q
```

### Step 3：8.3 RED——行情时点可知的连续经济价格

在输入、结果清单和统一读取测试中先加入失败用例：

- 512480 原始 `close=2.700`、次日原始 `pre_close=1.350` 的 1:2 拆分样例，连续因子从应用日开始为 2，过去行保持不变；
- 公告日晚于应用日时保留并标记为事后核对；取消事件不能授权价格基准变化，未知状态、关键字段缺失、重复冲突、知识截止日无效或没有有效事件解释的价格基准变化一律 `evidence_insufficient`；
- 当前已取消但取消日期晚于快照截止日时仍按截止日有效处理；已取消但缺少取消日期时关闭导出，防止把未来状态带回历史快照；
- 有效事件未发生价格基准变化时只记审计，不强制改变连续因子；官方拆分比例或每份现金必须在声明容差内与价格基准变化勾稽；
- 连续 OHLC、连续 `pre_close`、经济单位、突破、N 值和 `continuous_close / continuous_pre_close - 1` 协方差收益保持同一价格基准；
- 现金分红只通过除权日连续总回报隐含再投资，不在支付日增加现金或生成虚假订单；
- 公司行动前后权益连续，原始机械跳变不产生虚假突破、止损或风险放大；
- 本地清单缺少或篡改 `source.accounting` 时拒绝，统一读取器原样暴露精度限制。

Run：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_inputs.py tests\local_quant_research\test_turtle_result_adapter.py tests\local_quant_research\test_turtle_single_scenario.py tests\local_quant_research\test_analysis_manifest_schemas.py -q
```

Expected：RED，原因是当前输入直接使用原始未复权价格，清单没有核算口径。

### Step 4：8.3 GREEN——接入 vectorbt 与结果精度元数据

最小实现只修改输入准备和结果契约：

1. 从快照公司行动事实派生逐证券前向累计连续因子；
2. 用连续经济 OHLC、`pre_close` 和经济单位构造 `SimulationInputs`（模拟输入）；
3. 协方差日收益改为 `continuous_close / continuous_pre_close - 1`；
4. 不新增公司行动订单、支付日现金或私有回测引擎；
5. `manifest.json` 的 `source.accounting` 固定写入：
   - `corporate_action_mode=point_in_time_total_return_approximation`
   - `continuity_factor_basis=raw_previous_close_over_current_pre_close`
   - `corporate_action_metadata_timing=point_in_time_known`
   - `price_basis=continuous_economic_price`
   - `quantity_basis=economic_units`
   - `cash_dividend_mode=implicit_reinvestment_on_ex_date`
   - `pay_date_cash_supported=false`
   - `exact_joinquant_reconciliation=false`
   - 公司行动来源摘要与核算版本；
6. 归因日志记录事件身份、应用日、`evidence_timing=point_in_time`、连续因子与限制，不改变订单事实。

完成后重跑 Step 3，并加跑引擎与统一读取回归：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_inputs.py tests\local_quant_research\test_turtle_vectorbt_engine.py tests\local_quant_research\test_turtle_result_adapter.py tests\local_quant_research\test_turtle_single_scenario.py tests\local_quant_research\test_analysis_manifest_schemas.py tests\local_quant_research\test_analysis_data_views.py -q
```

### Step 5：8.6——重建七个场景与完整报告

先解析并打印将要清理的绝对路径，确认都位于本仓库 `.local` 且只属于本变更，再删除旧公司行动口径污染的共享快照、七份单场景结果和派生分析；不得触碰 `strategy-001/002` 聚宽归档。随后：

1. 用真实 11 只 ETF 行情和权威公司行动事实生成新不可变快照；
2. 主 agent 从 `analysis-plan.json` 生成七份配置，并复数调用单场景 Skill 七次；Skill 本身不包含七方案数量、循环或聚合；
3. 每个场景冷启动和预热都小于等于 180 秒，规范化结果摘要一致；
4. 显式登记七组 `scenario_id -> run_id`，重新生成收益、回撤、Alpha/Beta（超额收益/市场暴露）、归因、仓位、风险、六个挑战、全部稳健性、压力、CVaR（条件风险价值）、报告和推荐；
5. 检查报告明确披露近似核算限制，不保留兼容副本，不运行新旧方案对照。

### Step 6：8.7——完整用户入口与仓库门禁

依次运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_storage.py tests\local_quant_research\test_turtle_vectorbt_inputs.py tests\local_quant_research\test_turtle_vectorbt_engine.py tests\local_quant_research\test_turtle_result_adapter.py tests\local_quant_research\test_turtle_single_scenario.py tests\local_quant_research\test_analysis_manifest_schemas.py -q
.\.venv\Scripts\python.exe -m pytest -q
openspec validate build-turtle-etf-local-research-workflow --strict
```

再从公开 `run-local-quant-research` Skill 用户入口完成一次公司行动单场景 E2E（端到端），由主 agent 完成七次复数调用和独立分析 E2E，运行 Build and Verify（构建与验证）完整门禁、全仓旧精确记账/流动性规则扫描与全面代码审查。确认 `.local` 下没有 CSV 暂存、预热副本、测试临时产物或持久 DuckDB 文件；阻断项为零后才能进入 Comet verify（验证）阶段。
