---
comet_change: refactor-local-research-three-layer-architecture
role: technical-design
canonical_spec: openspec
---

# 本地研究三层架构技术设计

## 1. 设计目的

本设计深化 OpenSpec（开放规格）已经确认的三个 capability（能力）：共享本地研究运行时、后端中立结果包、自包含档案晋升。OpenSpec delta spec（增量规格）仍是行为要求的唯一事实源；本文只定义如何实现这些要求。

当前摩擦来自三个位置：

- 海龟策略目录同时实现策略规则、vectorbt（向量化回测框架）接线、单场景、性能、结果格式和 CLI（命令行入口）。
- 即时路径把 vectorbt 作为账本，延迟路径却先维护 Python（编程语言）手工账本，再用 `from_orders()` 复核。
- `.local` 运行包缺少完整策略源码、统一结果契约和可直接晋升的档案结构。

重构后只有 Strategy Module（策略模块）随策略变化；共享能力和 vectorbt Adapter（适配器）对新策略保持稳定。

## 2. 依赖方向与 Module

依赖方向固定为：

```text
joinquant/.../research/<strategy>/strategy.py
             │
             │ StrategyModule Interface
             ▼
scripts/research/local_quant_research/
             │
             │ OrderProgram Interface
             ▼
scripts/research/local_quant_research/vectorbt_runtime.py
             │
             ▼
vectorbt.Portfolio.from_order_func
```

规则如下：

1. 只有 `vectorbt_runtime.py` 可以导入 vectorbt 的 `Portfolio`、callback context（回调上下文）、订单枚举和 record dtype（记录结构）。
2. Strategy Module 只依赖 `local_quant_research.contracts`、共享行情 `SnapshotView`、NumPy（数值数组）和实现策略所需的通用数值库。
3. `result_package.py` 依赖 `ExecutionLedger` 和 `ResultExtension`，不得导入具体策略。
4. `analysis_data` 继续拥有后端中立的 manifest（清单）读取、校验和查询视图；本地 writer（写入器）不反向进入读取层。
5. `.agents/skills/run-local-quant-research/SKILL.md` 只描述公开命令、前提和停止状态，不保存生产代码、行情或结果。

删除任一共享 Module 后，其复杂度都会重新出现在所有策略调用方，因而这些 Module 具有足够 Depth（深度）；策略私有文件则通过唯一公开 Module 保持 Locality（局部性）。

## 3. 共享 Python Interface

以下定义是实施时必须保持的公开形状；具体私有字段可在不影响调用者的前提下调整。

### 3.1 Strategy Module

```python
from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True, slots=True)
class StrategyDescriptor:
    strategy_id: str
    contract_version: str
    extension_names: tuple[str, ...]
    accounting: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PreparedStrategy:
    ledger_input: "LedgerInput"
    primary_program: "OrderProgram"
    context: object


class StrategyModule(Protocol):
    descriptor: StrategyDescriptor

    def prepare(
        self,
        snapshot: "SnapshotView",
        config: Mapping[str, object],
    ) -> PreparedStrategy: ...

    def followup_program(
        self,
        prepared: PreparedStrategy,
        primary_run: "ExecutionRun",
    ) -> "OrderProgram | None": ...

    def build_extensions(
        self,
        prepared: PreparedStrategy,
        execution: "ExecutionBundle",
    ) -> tuple["ResultExtension", ...]: ...
```

不提供独立 `validate_config()`。`prepare()` 必须先完成纯配置校验，只有通过后才能读取行情数组或分配大型矩阵。缺少声明证据时抛出显式 `StrategyEvidenceError(code, message)`；普通 `ValueError` 不得自动映射为证据不足，以免掩盖实现缺陷。

父进程先在受限 `strategy_root` 内解析声明的 module，再以该 module 的顶层包目录（单文件 module 则为文件所在目录）作为源码边界，静态发现其中全部普通 `.py` 文件；该排序后的源码集合是唯一权威来源，同时驱动：

- 运行身份摘要；
- 档案 `code/` 源码快照；
- Strategy Module 代码变更检测。

`StrategyDescriptor` 不再维护第二份 `source_files` 清单。源码发现不得越过当前 module 包去扫描 `research/archives/` 或其他相邻目录。每个 `_execute` 都是只加载一个策略的全新子进程，子进程把已冻结的策略根放到 `sys.path` 首位并使用标准 `importlib.import_module()`；不建立 UUID（唯一标识）私有命名空间、全局导入锁或手工 `sys.modules`（模块缓存）生命周期。

### 3.2 执行 contracts

```python
@dataclass(frozen=True, slots=True)
class LedgerInput:
    dates: np.ndarray
    symbols: tuple[str, ...]
    close: np.ndarray
    initial_cash: float
    group_ids: np.ndarray
    cash_sharing: bool
    frequency: str


@dataclass(frozen=True, slots=True)
class OrderBuffer:
    enabled: np.ndarray
    side: np.ndarray
    size: np.ndarray
    price: np.ndarray
    fixed_fees: np.ndarray
    size_granularity: np.ndarray
    allow_partial: np.ndarray
    priority: np.ndarray


@dataclass(frozen=True, slots=True)
class OrderProgram:
    program_id: str
    prepare_segment_nb: object
    after_fill_nb: object
    after_segment_nb: object | None
    inputs: tuple[object, ...]
    params: tuple[object, ...]
    state: tuple[object, ...]
    trace: Mapping[str, np.ndarray]
    orders: OrderBuffer
```

`OrderBuffer` 是 structure-of-arrays（列式数组集合），长度等于证券列数。它在模拟前分配一次，每个 segment（分段）原地重置和写入。策略不创建 vectorbt Order；共享 callback 读取当前列并调用 `nb.order_nb`。

项目自有数值枚举固定在 contracts：

```text
SIDE_NONE = 0
SIDE_BUY  = 1
SIDE_SELL = -1

FILL_IGNORED  = 0
FILL_ACCEPTED = 1
FILL_REJECTED = 2
```

策略 action/reason code（动作与原因码）不进入共享枚举，继续保留在策略私有 `_kernel.py`。

### 3.3 Numba 视图

共享 runtime 向策略函数传递项目自有 `namedtuple`（命名元组）：

```python
SegmentView(
    row,
    group,
    from_col,
    to_col,
    cash,
    value,
    positions,
    valuation_prices,
)

FillEvent(
    row,
    column,
    status,
    side,
    size,
    price,
    fees,
    cash_after,
    position_after,
)
```

这些结构只引用 vectorbt 当前上下文已有的标量和数组，不复制持仓或估值矩阵。策略 kernel 不得访问 `call_seq_now`、`last_cash`、`last_position`、`OrderStatus` 或 vectorbt record dtype。

### 3.4 执行结果

```python
@dataclass(frozen=True, slots=True)
class ExecutionRun:
    ledger: "ExecutionLedger"
    trace: Mapping[str, np.ndarray]


@dataclass(frozen=True, slots=True)
class ExecutionBundle:
    primary: ExecutionRun
    final: ExecutionRun
    stages: tuple[str, ...]
```

`ExecutionLedger` 私有持有原始 Portfolio，只公开：

```text
orders
assets
cash
value
trades
positions
returns
```

每个属性第一次访问时计算并缓存，之后返回同一个只读对象。若 vectorbt 已返回独占、连续数组，只设置只读标志；只有 vectorbt 返回临时或非连续视图时允许一次明确复制。测试必须记录每个属性的计算次数和 `np.shares_memory` 证据。

## 4. vectorbt Adapter 生命周期

共享 runtime 提供唯一入口：

```python
run_vectorbt(ledger_input: LedgerInput, program: OrderProgram) -> ExecutionRun
```

生命周期为：

1. 校验全部轴、shape（形状）、dtype（数据类型）、只读输入和现金分组。
2. 根据固定 hook identity（回调身份）获取或创建一次 callback binding；同一进程内相同策略的不同参数不得产生新 wrapper。
3. `pre_sim_func_nb` 只初始化策略 state、trace 和订单缓冲。
4. `pre_segment_func_nb` 构造 `SegmentView`，调用策略 `prepare_segment_nb`，然后按 `(priority, original_column)` 原地生成稳定 `call_seq_now`。
5. `order_func_nb` 从 `OrderBuffer` 读取订单意图并转换为 `nb.order_nb`；禁用订单返回 `nb.NoOrder`。
6. `post_order_func_nb` 把 vectorbt 成交结果转换为 `FillEvent`，调用策略 `after_fill_nb`。策略只能在真实成交后推进单位、止损和其他成交状态。
7. `post_segment_func_nb` 调用可选策略 hook，完成该日审计快照。
8. 返回私有 Portfolio 包装后的 `ExecutionLedger` 和策略 trace。

runtime 固定配置：

```text
cash_sharing=True
group_by=True
call_pre_segment=True
update_value=True
ffill_val_price=True
use_numba=True
max_logs=0
```

保留 vectorbt（向量化回测框架）默认的持仓记录能力，`trades/positions` 必须直接复用 Portfolio（组合）的公开 accessor（访问器），不得为了关闭记录而在共享层重建交易或持仓。`max_logs=0` 与缓冲预分配只有在结果等价和真实性能门禁通过后保留；没有实测瓶颈时不增加其他性能开关。

`pre_segment_func_nb` 不得创建行级临时数组。稳定排序使用预分配索引缓冲或原地插入排序；证券数当前为 11–17，禁止为此引入并行分块。共享现金和跨资产风险也禁止按证券独立分块。

## 5. 即时与延迟两阶段执行

共享 scenario（场景）编排固定如下：

```python
prepared = module.prepare(snapshot, config)
primary = run_vectorbt(prepared.ledger_input, prepared.primary_program)
followup = module.followup_program(prepared, primary)

if followup is None:
    execution = ExecutionBundle(primary, primary, ("primary",))
else:
    final = run_vectorbt(prepared.ledger_input, followup)
    execution = ExecutionBundle(primary, final, ("primary", "followup"))
```

不设计通用 N 阶段状态机：当前真实需求只有无延迟的一阶段和延迟的两阶段，更多阶段属于 YAGNI（暂不需要）。

海龟 `additional_delay_days=0` 时，primary 即最终账本。延迟为正时：

1. primary 运行保持当前即时语义，产生完整计划和策略 trace；
2. `_delayed.py` 从 primary trace 冻结计划日、执行日、动作、目标数量、原因、信号 N 和稳定顺序；
3. followup program 在执行日只复核可交易性，并按实际 final ledger 做现金和持仓机械截断；
4. 到样本末尾仍未执行的订单进入 horizon-expired（期限到期）证据；
5. 核心四表只读取 final ledger；归因扩展同时连接 primary 计划和 final 成交。

followup 不重新计算突破、退出、加仓、资产组风险或组合风险，也不得根据延迟日行情重新分配目标。

## 6. Strategy Module 内部结构

海龟目录收敛为：

```text
turtle_etf/
├── __init__.py
├── strategy.py
├── _kernel.py
├── _attribution.py
└── _delayed.py
```

- `strategy.py`：唯一公开 `MODULE`，组合配置、行情准备、primary/followup program 和扩展。
- `_kernel.py`：指标、海龟状态、Numba hooks、动作码和原因码。
- `_attribution.py`：海龟扩展 Schema、事实构造、覆盖率和盈亏勾稽。
- `_delayed.py`：冻结计划、执行日调整和到期证据，不维护账户。
- `__init__.py`：保持轻量，不导入 PyArrow、vectorbt 或执行大数组准备。

迁移映射：

| 当前文件 | 目标 |
|---|---|
| `indicators.py` | `_kernel.py` |
| `vectorbt_inputs.py` 的策略信号与公司行动证据 | `strategy.py` / `_kernel.py` |
| `vectorbt_callbacks.py` 的策略规则 | `_kernel.py` |
| `vectorbt_callbacks.py` 的 vectorbt 接线 | 共享 `vectorbt_runtime.py` |
| `vectorbt_engine.py` | 共享 runtime + `strategy.py` |
| `vectorbt_delayed.py` 的冻结语义 | `_delayed.py` |
| `vectorbt_delayed.py` 的手工账本 | 删除 |
| `result_adapter.py` 的四表/清单/文件逻辑 | 共享 `result_package.py` |
| `result_adapter.py` 的海龟归因 | `_attribution.py` |
| `single_scenario.py` / `vectorbt_benchmark.py` / `vectorbt_cli.py` | 共享 scenario/performance/cli |

旧文件只在测试迁移期间存在；生产 `project-run.json` 在一个切换提交中改到共享入口，并在同一提交删除旧生产入口，不提供 feature flag（功能开关）或兼容分支。

## 7. 固定共享 CLI 与配置 v2

公开命令：

```powershell
.\.venv\Scripts\python.exe scripts\research\local_quant_research\cli.py run --config <project-run.json>

.\.venv\Scripts\python.exe scripts\research\local_quant_research\cli.py promote `
  --strategy-id <strategy_id> `
  --run-id <run_id> `
  --analysis-id <analysis_id>
```

`project-run.json` v2 示例：

```json
{
  "schema_version": 2,
  "project_id": "strategy-003",
  "strategy": {
    "root": "joinquant/strategies/strategy-003/research",
    "module": "turtle_etf.strategy",
    "symbol": "MODULE"
  },
  "snapshot_id": "<sha256>",
  "snapshot_requirements": {},
  "scenario_config": "joinquant/strategies/strategy-003/research/baseline.json",
  "declared_inputs": [
    "joinquant/strategies/strategy-003/manifest.json"
  ]
}
```

以下字段不再由项目配置：`command`、`project_entry`、`code_identity`、`required_outputs`、`output_root` 和 `stop_states`。它们改为共享运行时固定合同：

- Python 必须是仓库 `.venv`；
- 子进程入口固定为共享 CLI 的私有 `_execute` 动作；
- 唯一输出是 archive-ready package；
- 输出根固定为 `.local/quant-research`；
- 停止状态固定为 `complete/evidence_insufficient/failed`。

runner 使用当前 CLI 文件重新启动同一 `.venv` 子进程：

```text
cli.py run
  → 校验并冻结配置、策略源码和行情输入
  → .venv python cli.py _execute <保留参数>
  → 子进程完成一次冷启动和一次预热
  → 父进程验证输出、补全来源证据并原子发布
```

`_execute` 不公开给用户配置，所有参数均由 runner 生成。父进程静态发现并冻结策略根下全部普通 `.py` 文件；子进程临时把冻结策略 root 放到 import path 首位，并验证实际 module file 位于 root 内。

仓库不再手工维护逐文件 hash 的输入型 `code-identity.json`。父进程根据唯一静态发现源码集合、固定共享运行时文件集和已安装依赖生成档案中的 `config/code-identity.json` 与 `evidence/runtime-lock.json`，减少每次策略改动后的机械维护。

## 8. Archive-ready Result Package

`.local/quant-research/<strategy_id>/<run_id>/` 本身就是可晋升包：

```text
<run_id>/
├── manifest.json
├── code/
│   └── <strategy source files>
├── config/
│   ├── scenario.json
│   ├── project-run.json
│   └── code-identity.json
├── data/
│   ├── results.parquet
│   ├── balances.parquet
│   ├── positions.parquet
│   └── orders.parquet
├── extensions/
│   └── <extension-name>/...
├── evidence/
│   ├── market-snapshot.json
│   ├── runtime-lock.json
│   ├── performance.json
│   └── environment.json
└── report/
    ├── execution-summary.md
    └── metrics.json
```

`manifest.json` 使用 `local-research-package/2`，至少声明：

- `object.kind=local_research`、status、strategy_id、run_id、scenario_id；
- authority=`local_research`，backend=`vectorbt`；
- 四张核心数据集和全部扩展的 Schema、行数、时间范围、状态、文件与 SHA256；
- 策略代码、配置、运行时、行情、性能、环境和报告引用；
- gate checks 与 exceptions。

核心事实构造只读取 final ledger：

- `orders` 从 vectorbt filled records 加策略计划/拒单 trace 构造；
- `positions` 从 `assets()`、估值价格和一次共享成本基础 pass 构造；
- `balances` 从 `cash()`、`value()` 构造；
- `results` 从 value returns 和显式 benchmark 状态构造。

策略扩展通过以下对象交给 writer：

```python
@dataclass(frozen=True, slots=True)
class ResultExtension:
    name: str
    schema_version: str
    table: pa.Table
    unique_key: tuple[str, ...]
    evidence: Mapping[str, object]
```

策略可以构造 Arrow（列式内存）表，但公共契约只接受扁平 `string/bool/int64/float64`（字符串/布尔/整数/浮点）列。浮点缺失值必须使用 Arrow null（空值），不得使用 NaN（非数值）；dictionary/list/struct/map/union/run-end encoded（字典/列表/结构/映射/联合/游程编码）及其他类型统一在冷/热比较前以 `ResultContractError` 拒绝。策略不得选择路径、压缩、文件名或直接写 Parquet。

共享层使用 PyArrow（列式计算库）现成的 `Table.validate(full=True)` 校验表结构，先比较精确 Schema，再用 `Table.equals(check_metadata=True)` 比较冷/热扩展表；核心 vectorbt 事实继续使用 NumPy（数值数组）摘要。共享层不实现递归 Arrow 类型解码或任意类型逻辑哈希。完成包写入后，完整性只使用实际 Parquet 文件 SHA256。

writer 只执行一次核心/扩展 Parquet 物化和一次回读。内部写入路径复用该次回读事实完成 Schema、唯一键、勾稽、报告和 manifest（清单），并在观测边界一次写入性能证据与清单；不得调用带 `preloaded_*` 逃生参数的外部 validator（校验器），也不得构造 provisional/final（临时/最终）两套完整包。公开 `validate_result_package()` 保持纯文件读取，只供复用、晋升和外部查询。

机械执行报告只呈现包内可复核事实：身份、参数、范围、表行数、成交/持仓摘要、净值摘要、性能和完整性门禁。它不得使用“推荐”“稳健性通过”“适合实盘”等判断语句。

## 9. 晋升协议

`promote` 按下列顺序执行：

1. 由 strategy_id 和 run_id 定位 `.local` 源包，验证 `manifest.json` 为 complete。
2. 从源 manifest 取得策略目录；拒绝调用者提供任意目标路径。
3. 校验 analysis_id 满足 `[a-z0-9][a-z0-9._-]{0,63}`。
4. 目标固定为 `joinquant/strategies/<strategy_id>/research/archives/<analysis_id>/`。
5. 若目标存在，逐文件验证：完全一致返回 `reused=true`；任一差异返回 conflict，不修改目标。
6. 若目标不存在，在 `archives/` 下创建 `.<analysis_id>.<uuid>.tmp`。
7. 晋升开始前用一次 `lstat/rglob` 扫描拒绝 hardlink（硬链接）、symlink（符号链接）、目录连接和非普通文件；使用标准 `shutil.copy2` 复制，再对目标树逐文件复核长度和 SHA256。
8. 复核目标暂存包清单和分析视图；调用 `os.replace` 原子发布。本地仓库与同一用户进程属于可信边界，不实现针对扫描后敌对并发替换的文件描述符/inode（文件节点）状态机。
9. 失败时只删除本次暂存目录，源包和既有档案保持不变。

analysis_id 只存在于目标目录名，包内 run_id 和所有字节不改变。查询返回二者：analysis_id 是人选档案别名，run_id 是内容身份。

晋升模块不得导入策略、vectorbt 或 PyArrow writer；测试必须把这些调用替换为“调用即失败”，证明晋升没有重算。

## 10. 错误模型

| 阶段 | 错误类型 | 最终状态 |
|---|---|---|
| 通用配置/路径/身份缺失 | `ConfigurationError` | `evidence_insufficient` |
| 策略声明输入缺失 | `StrategyEvidenceError` | `evidence_insufficient` |
| 行情快照缺失或不匹配 | `MarketDataError` 的证据分支 | `evidence_insufficient` |
| 策略、Numba、vectorbt 执行异常 | `ExecutionError` | `failed` |
| 结果 Schema、勾稽、摘要不一致 | `ResultContractError` | `failed` |
| 性能超限 | `PerformanceGateError` | `failed` |
| 原子发布或清理失败 | `EvidenceError` | `failed` |
| vectorbt 正常订单拒绝 | 执行事实 | 不改变运行状态 |

只允许显式错误类型映射为 `evidence_insufficient`。未知异常统一进入 `failed`，输出稳定 reason code，不把 traceback（堆栈）或敏感环境写入清单。

不支持的策略扩展类型和扩展中的 NaN 必须在 cold/warm digest（冷/热摘要）前固定映射为 `failed + result_contract_failed`，不得退化为 `execution_digest_mismatch` 或未知异常。

晋升返回独立 `ArchiveResult(status, reused, source, target, reasons)`，不修改研究运行的三种停止状态。

## 11. 性能与内存设计

正常一次 `run` 仍只在一个全新子进程中执行一次冷启动和一次预热，不把 3/5 重复采样加到日常路径。发布验证使用独立 performance command（性能命令）：

- 冷启动：3 个全新进程，中位数；
- 预热：同一进程 5 次，中位数；
- 场景：3,432 日 × 11 ETF、3,432 日 × 17 ETF、延迟 1 日；
- 正确性：Schema、行数、成交、费用、现金、持仓、净值和逻辑摘要零差异；
- 相对门禁：时间、峰值进程内存和同逻辑核心/扩展 Parquet payload（列式数据载荷）体积不超过基线 5%；固定代码、配置、证据与报告开销单独报告，不与旧 v1 整包直接比较；
- 绝对门禁：单次冷、热仍不得超过 180 秒。

阶段计时固定为：

```text
strategy_load
strategy_prepare
primary_vectorbt
followup_prepare
followup_vectorbt
core_facts
strategy_extensions
parquet_materialize
readback_validate
report_and_manifest
```

总计时覆盖上述全部阶段，不允许通过移动工作出计时范围来改善数字。共享 CLI 启动耗时另行记录，不混入预热引擎耗时。

Windows 峰值进程内存使用标准库 `ctypes` 调用 `GetProcessMemoryInfo`，由父进程轮询子进程 working set peak（峰值工作集），不新增 psutil 依赖。通用 CI 只执行 180 秒绝对门禁；5% 相对门禁只在同一固定机器的发布验证中执行。

## 12. 测试结构

### 12.1 Interface 测试

- `test_strategy_contract.py`：标准 importlib loader（导入加载器）、唯一静态源码身份、路径安全、两个策略 Adapter。
- `test_vectorbt_runtime.py`：稳定优先级、订单转换、成交回调、惰性缓存、共享内存和 callback 特化数量。
- `test_result_package.py`：四表、扩展、清单、单次物化、回读、跨表勾稽和机械报告措辞。
- `test_archive.py`：完整源、失败源、幂等、冲突、逐字节一致、无行情复制和失败清理。

测试只通过公开 Interface 验证共享 Module。策略私有 kernel 的纯数值边界可以有私有测试，但外部测试不得导入旧公开文件。

### 12.2 Characterization 测试

迁移前冻结：

- 所有订单 action/reason、计划量、成交量、价格和费用；
- 每日 cash/assets/value；
- 单位数、冻结 N、共同止损、再分配 scale；
- 延迟计划日、执行日、调整码、稳定顺序和到期订单；
- 四表行数、逻辑摘要、归因覆盖及逐证券损益勾稽。

这些测试先针对旧 Interface 通过；迁移任务把同一 fixture（夹具）转向新公开 Interface，期望值不改。

### 12.3 E2E（端到端）

必须从发布形态运行：

```text
.venv python shared-cli run
  → minimal strategy complete
  → turtle strategy complete
  → analysis_data open
  → shared-cli promote
  → 删除 .local 源副本后的 archive query
```

另外覆盖 evidence_insufficient、执行失败、摘要冲突、性能超限、重复运行复用、重复晋升复用、analysis_id 冲突和中途失败。

## 13. TDD 迁移顺序

1. 建立现有行为和性能基线，不修改生产入口。
2. 用失败测试定义 contracts 和最小测试策略。
3. 抽取共享结果包并让旧海龟执行在测试中写新包。
4. 完成 archive-ready package 和 promote E2E。
5. 抽取共享 CLI、scenario 和 performance；新路径仍只由测试调用。
6. 收窄扩展表并把 writer 收敛为单次回读事实链，删除递归 Arrow 解释器和内部 validator 双路径。
7. 统一静态源码身份并改用标准 importlib，删除 descriptor 源码清单、UUID 命名空间和重复 fixture 内容。
8. 把晋升收敛为扫描、标准复制、摘要复核和原子发布，删除敌对并发树状态机。
9. 建立通用 vectorbt 唯一账本 runtime，只用通用 primary/follow-up fixture 证明共享接线。
10. 在海龟 Strategy Module 内同时迁移即时与延迟 OrderProgram，逐笔一致后删除手工账本。
11. 单次切换生产配置和旧入口，再执行完整 E2E、真实性能和 Build and Verify（构建与验证）。

每一步遵循 RED → GREEN → REFACTOR（失败测试→最小通过→整理），并形成可独立审查的提交。若某一步不能保持共享 CLI 可运行，则缩小该任务，不通过双生产 feature flag 绕过。

## 14. 被拒绝方案

### 策略直接暴露 vectorbt callback

迁移最少，但会把 vectorbt context、订单枚举和内部数组 shape 泄漏给所有策略，无法形成稳定 Seam，拒绝。

### 使用 `from_signals()`

不能完整表达共享现金、跨资产风险、全量再分配、确定性订单顺序、每单位冻结状态和实际成交后止损推进，拒绝。

### 预先生成完整订单矩阵后 `from_orders()`

无法在执行过程中根据实际现金、持仓和成交反馈推进策略状态，也无法正确表达最低佣金和延迟机械截断，拒绝。

### 把生产实现放进 Skill 目录

会把 Agent 发现资产与生产依赖、数据和测试绑定，并违反现有薄 Skill 契约，拒绝。

### 保留旧入口作为兼容模式

会形成两套账本、writer 和代码身份，继续制造原问题，拒绝。回滚依赖 Git 提交，不依赖运行时兼容开关。

## 15. 完成判据

实现只有同时满足以下条件才能进入验证阶段：

- 三个 OpenSpec capability 的所有场景有对应自动化证据；
- 海龟和最小策略都只通过共享 CLI 运行；
- 策略源码不导入 vectorbt；共享层不导入海龟私有文件；
- 即时和延迟最终账户变化都只来自 vectorbt；
- `.local` 包可逐字节晋升且删除源缓存后仍可查询；
- 结果零差异，固定机器相对性能、内存和同逻辑 Parquet payload 体积不超过 5% 噪声；
- 策略扩展只使用扁平标量 Arrow 类型，冷/热比较和 Parquet 完整性复用 PyArrow/DuckDB（列式计算库/分析数据库）现成能力，不存在递归 Arrow 类型解释器；
- 共享 writer 只有一次回读事实链，策略加载只有标准 importlib 和一份静态源码身份，晋升只有扫描、标准复制、摘要复核和原子发布；
- 旧 CLI、旧引擎、旧 writer、延迟手工账本和双生产路径已删除；
- 完整 Build and Verify 与发布形态 E2E 通过。
