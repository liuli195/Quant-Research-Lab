# Turtle ETF Full-Position Redistribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `strategy-003` 的本地研究基线改成经典海龟逐单位 N 风险模型与事件驱动的全量仓位再分配，并产出一个可由 Vibe-Trading（AI 研究助理）真实读取的新基线结果和研究报告。

**Architecture:** 行情和信号仍由共享 Parquet（列式存储）快照、DuckDB（内存数据库）和现有 `vectorbt`（向量化回测库）输入层提供；`strategy-003` 内部用固定大小的 Numba（即时编译器）数组保存最多四个逻辑单位，并在入场、加仓、止损或退出事件出现时统一计算 4/6/12 风险缩放、现金缩放和整手目标。标准四表不变，海龟归因扩展增加单位和缩放证据；独立 Vibe 分析只读标准结果包，不进入交易回调。

**Tech Stack:** Python 3.12、vectorbt 1.1.0、Numba 0.66.0、NumPy 2.4.6、Pandas 3.0.3、PyArrow（列式数据）、DuckDB（内存查询）、Pytest（测试框架）、OpenSpec（开放规格）

## Global Constraints

- 只改本计划列出的 `strategy-003` 本地研究、标准结果适配、独立分析兼容、规格和测试；不改聚宽正式策略、正式回测或模拟交易。
- 所有 Python 命令使用 `.\.venv\Scripts\python.exe`；不安装或升级依赖。
- 当前工作区已有未提交改动。每次提交前执行 `git diff --cached --name-only`，只暂存本任务文件；不得覆盖、清理或提交无关改动。
- 不运行旧基线对照，不运行 17 ETF 扩展，不运行稳健性矩阵；本次只运行一个 11 ETF 新基线。
- 不重写、不迁移、不删除既有 `.local/` 研究证据；新运行由现有不可变运行目录生成。
- 删除旧 A1、资金仓位上限、计划风险上限、协方差交易门槛和目标波动率交易路径，不保留开关、极大值、`null`、兼容分支或回退实现。
- 不增加成交额 1%、流动性门槛或任何未经确认的策略外规则。
- `run-local-quant-research` Skill（技能）仍只编排一次单场景；不得把 7 个场景、海龟规则、Vibe 分析或报告写入 Skill。
- 每个生产改动先有失败测试，再做最小实现；行为测试通过后再提交。
- 完整业务验收必须从 `scripts/research/local_quant_research/cli.py run --config joinquant/strategies/strategy-003/research/project-run.json` 用户入口执行，单元测试不能替代。

## File Map

- `joinquant/strategies/strategy-003/research/baseline.json`：唯一新基线机器契约。
- `joinquant/strategies/strategy-003/research/analysis-plan.json`：保留 7 个单因子声明，但本次不执行；移除已失效协方差变体。
- `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_inputs.py`：只准备行情、信号、交易约束和分组，不再计算交易用协方差。
- `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_callbacks.py`：逻辑单位、4/6/12 缩放、现金缩放、事件目标和成交后状态的唯一实现。
- `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py`：严格解析新参数、分配固定状态数组、连接官方 vectorbt 回调。
- `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_delayed.py`：保留已存在的额外延迟研究能力，只同步新的再分配买卖动作；基线仍为次日开盘。
- `joinquant/strategies/strategy-003/research/turtle_etf/result_adapter.py`：保持标准四表，输出逐单位与全量再分配归因证据。
- `scripts/research/quant_analysis/unified_analysis.py`、`scripts/research/quant_analysis/reporting.py`：移除对旧仓位上限和目标波动率字段的硬依赖，使现有 Vibe 分析可读取新结果。
- `tests/local_quant_research/`、`tests/quant_analysis/`：规则、结果包、分析和完整 E2E（端到端）回归。
- `openspec/changes/build-turtle-etf-local-research-workflow/`、`docs/research/2026-07-13-turtle-etf-system-final-plan.md`：同步最新规则和范围。
- `joinquant/strategies/strategy-003/research/code-identity.json`：最后更新实际执行文件摘要。
- `.local/quant-research/strategy-003/`：新基线不可变运行证据，仅运行时写入，不提交。
- `docs/research/2026-07-16-turtle-full-position-redistribution-baseline-report.md`：真实新基线研究报告。

---

### Task 1: 冻结唯一基线配置并删除失效挑战契约

**Files:**
- Modify: `tests/local_quant_research/test_contract_fixtures.py`
- Modify: `joinquant/strategies/strategy-003/research/baseline.json`
- Modify: `joinquant/strategies/strategy-003/research/analysis-plan.json`
- Delete: `joinquant/strategies/strategy-003/research/challenge-analysis-plan.json`
- Delete: `docs/superpowers/specs/2026-07-16-turtle-volatility-rearm-design.md`
- Delete: `docs/superpowers/plans/2026-07-16-classic-turtle-unit-challenge.md`

- [ ] **Step 1: 先把配置测试改成新契约**

将基线断言改为以下精确值：

```python
assert baseline["signal"] == {
    "entry_days": 55,
    "exit_days": 20,
    "n_days": 20,
    "add_step_n": 0.5,
    "stop_n": 2.0,
    "max_units": 4,
}
assert baseline["risk"] == {
    "unit_risk_per_n": 0.01,
    "asset_group_unit_cap": 6.0,
    "portfolio_unit_cap": 12.0,
}
assert baseline["execution"] == {
    "additional_delay_days": 0,
    "order_priority": [
        "full_exit",
        "redistribution_sell",
        "entry_or_addition",
        "redistribution_buy",
    ],
    "allocation": "full_position_redistribution",
    "acceptance_fixture": {
        "same_security_exit_cancels_buys": True,
        "candidate_requires_net_buy_lot": True,
        "group_unit_cap": 6.0,
        "portfolio_unit_cap": 12.0,
        "cash_scaling": "uniform",
        "lot_rounding": "floor",
        "residual_cash_redistribution": False,
        "input_order_invariant": True,
    },
}
assert not (_research_dir(repo_root) / "challenge-analysis-plan.json").exists()
```

把原协方差两个单因子替换为尚不执行的 `group-unit-cap-5` 和 `portfolio-unit-cap-10`，其覆盖分别为 `{"risk": {"asset_group_unit_cap": 5.0}}`、`{"risk": {"portfolio_unit_cap": 10.0}}`；保持总场景声明为 7，删除挑战计划测试。

- [ ] **Step 2: 运行配置测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_contract_fixtures.py -q
```

Expected: FAIL，显示旧风险字段、`max_units=null`、旧分配名称和挑战文件仍存在。

- [ ] **Step 3: 最小修改配置与删除文件**

按失败测试修改两个 JSON（结构化配置）。保留 11 ETF、6 分组、行情口径、费用和 150 万初始资金不变；物理删除三个失效文件，不创建替代兼容文件。

- [ ] **Step 4: 重新运行配置测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_contract_fixtures.py -q
```

Expected: PASS。

- [ ] **Step 5: 只提交本任务文件**

```powershell
Test-Path joinquant/strategies/strategy-003/research/challenge-analysis-plan.json
Test-Path docs/superpowers/specs/2026-07-16-turtle-volatility-rearm-design.md
Test-Path docs/superpowers/plans/2026-07-16-classic-turtle-unit-challenge.md
git add -- tests/local_quant_research/test_contract_fixtures.py joinquant/strategies/strategy-003/research/baseline.json joinquant/strategies/strategy-003/research/analysis-plan.json
git diff --cached --name-only
git commit -m "配置：切换海龟N风险单位基线"
```

三个 `Test-Path` 必须都返回 `False`。这些被删除文件当前均未纳入 Git（版本管理），不得为提交记录重新创建空文件。

---

### Task 2: 删除交易用协方差输入并严格解析新参数

**Files:**
- Modify: `tests/local_quant_research/test_turtle_vectorbt_inputs.py`
- Modify: `tests/local_quant_research/test_turtle_vectorbt_engine.py`
- Modify: `tests/local_quant_research/test_turtle_result_adapter.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_inputs.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py`

- [ ] **Step 1: 写输入和参数失败测试**

将 `SimulationInputs` 的精确字段断言收敛为 `signal_n` 结束，不再包含 `covariance`、`covariance_eligible`。将引擎参数测试改为：

```python
assert CallbackParams._fields == (
    "lot_size",
    "unit_risk_per_n",
    "add_step_n",
    "stop_n",
    "max_units",
    "asset_group_unit_cap",
    "portfolio_unit_cap",
    "commission_multiplier",
    "one_way_slippage",
)
assert params.max_units == 4
assert params.asset_group_unit_cap == 6.0
assert params.portfolio_unit_cap == 12.0
```

增加参数化拒绝测试，逐个向 `risk` 注入以下字段并断言 `ValueError("legacy risk fields are not supported")`：

```python
LEGACY = (
    "security_risk_cap",
    "security_value_cap",
    "asset_group_risk_cap",
    "asset_group_value_cap",
    "portfolio_risk_cap",
    "portfolio_value_cap",
    "covariance",
    "target_volatility",
    "risk_reduction_target_volatility",
    "minimum_aligned_samples",
)
```

另断言 `max_units` 缺失、`null`、布尔值、非 4 正整数均被拒绝。

- [ ] **Step 2: 运行聚焦测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_inputs.py tests\local_quant_research\test_turtle_vectorbt_engine.py tests\local_quant_research\test_turtle_result_adapter.py -q
```

Expected: FAIL，旧协方差数组和旧 `CallbackParams` 仍存在。

- [ ] **Step 3: 从输入层物理删除协方差计算**

删除 `SimulationInputs.covariance`、`SimulationInputs.covariance_eligible`、`_covariance_matrix`、风险配置读取、滚动收益矩阵和相关 `math` 导入。`prepare_simulation_inputs` 仍输出排序稳定、连续只读的行情、公司行动、信号和分组数组；`additional_delay_days` 不得移动信号源行。

- [ ] **Step 4: 用新参数替换引擎解析**

在 `vectorbt_engine.py` 定义并调用：

```python
_LEGACY_RISK_FIELDS = frozenset({
    "security_risk_cap", "security_value_cap",
    "asset_group_risk_cap", "asset_group_value_cap",
    "portfolio_risk_cap", "portfolio_value_cap",
    "covariance", "target_volatility",
    "risk_reduction_target_volatility", "minimum_aligned_samples",
})

def _reject_legacy_risk_fields(risk: Mapping[str, object]) -> None:
    found = sorted(set(risk) & _LEGACY_RISK_FIELDS)
    if found:
        raise ValueError(
            "legacy risk fields are not supported: " + ", ".join(found)
        )
```

`_params` 只构造 `lot_size`、`unit_risk_per_n`、`add_step_n`、`stop_n`、固定为 4 的 `max_units`、`asset_group_unit_cap`、`portfolio_unit_cap`、佣金倍数和滑点。`CallbackInputs` 只传交易所需数组和 `asset_group_ids`。

- [ ] **Step 5: 运行聚焦测试并确认通过**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_inputs.py tests\local_quant_research\test_turtle_vectorbt_engine.py tests\local_quant_research\test_turtle_result_adapter.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交输入和引擎契约**

```powershell
git add -- tests/local_quant_research/test_turtle_vectorbt_inputs.py tests/local_quant_research/test_turtle_vectorbt_engine.py tests/local_quant_research/test_turtle_result_adapter.py joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_inputs.py joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py
git diff --cached --name-only
git commit -m "重构：删除海龟交易用协方差门槛"
```

---

### Task 3: 先实现可独立验证的 4/6/12 与现金缩放内核

**Files:**
- Modify: `tests/local_quant_research/test_turtle_vectorbt_callbacks.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_callbacks.py`

- [ ] **Step 1: 写缩放公式失败测试**

直接调用 Numba 函数的 `.py_func`，至少增加以下四组断言：

```python
group_scales, portfolio_scale = _risk_scales_nb.py_func(
    np.asarray([4, 4, 4]),
    np.asarray([0, 0, 1]),
    2,
    6.0,
    12.0,
)
assert group_scales.tolist() == pytest.approx([0.75, 1.0])
assert portfolio_scale == pytest.approx(1.0)

group_scales, portfolio_scale = _risk_scales_nb.py_func(
    np.asarray([4, 4, 4, 4]),
    np.asarray([0, 1, 2, 3]),
    4,
    6.0,
    12.0,
)
assert group_scales.tolist() == pytest.approx([1.0, 1.0, 1.0, 1.0])
assert portfolio_scale == pytest.approx(0.75)
```

再构造基础数量 `[1000, 2000]`、相同开盘价、现金不足和每笔最低佣金场景，断言：目标都是 100 的整数倍、预计成交后现金非负、两者使用同一现金比例向下取整、没有把剩余现金按代码补给任一标的。最后对输入列做排列并还原，断言目标、组缩放、组合缩放和现金缩放完全一致。

- [ ] **Step 2: 运行缩放测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_callbacks.py -k "group_unit_scale or portfolio_unit_scale or uniform_cash_scale or permutation" -q
```

Expected: FAIL，新函数尚不存在。

- [ ] **Step 3: 实现纯缩放函数并删除 A1/Hamilton（最大余数）函数**

在 `vectorbt_callbacks.py` 实现以下精确接口：

```python
@njit
def _risk_scales_nb(
    unit_counts: np.ndarray,
    asset_group_ids: np.ndarray,
    group_count: int,
    asset_group_unit_cap: float,
    portfolio_unit_cap: float,
) -> tuple[np.ndarray, float]:
    group_units = np.zeros(group_count, dtype=np.float64)
    for column in range(unit_counts.shape[0]):
        group_units[asset_group_ids[column]] += unit_counts[column]
    group_scales = np.ones(group_count, dtype=np.float64)
    for group in range(group_count):
        if group_units[group] > asset_group_unit_cap:
            group_scales[group] = asset_group_unit_cap / group_units[group]
    effective_units = 0.0
    for column in range(unit_counts.shape[0]):
        effective_units += (
            unit_counts[column] * group_scales[asset_group_ids[column]]
        )
    portfolio_scale = 1.0
    if effective_units > portfolio_unit_cap:
        portfolio_scale = portfolio_unit_cap / effective_units
    return group_scales, portfolio_scale

@njit
def _targets_for_scale_nb(
    unit_base_quantities: np.ndarray,
    unit_counts: np.ndarray,
    asset_group_ids: np.ndarray,
    group_scales: np.ndarray,
    portfolio_scale: float,
    cash_scale: float,
    locked_quantities: np.ndarray,
    lot_size: int,
) -> np.ndarray:
    targets = np.zeros(unit_counts.shape[0], dtype=np.int64)
    for column in range(unit_counts.shape[0]):
        if locked_quantities[column] >= 0:
            targets[column] = locked_quantities[column]
            continue
        raw_quantity = 0
        for unit in range(unit_counts[column]):
            raw_quantity += unit_base_quantities[column, unit]
        scaled = (
            raw_quantity
            * group_scales[asset_group_ids[column]]
            * portfolio_scale
            * cash_scale
        )
        targets[column] = int(scaled // lot_size) * lot_size
    return targets

@njit
def _cash_after_targets_nb(
    row: int,
    targets: np.ndarray,
    positions: np.ndarray,
    cash: float,
    inputs: CallbackInputs,
    params: CallbackParams,
) -> float:
    projected_cash = cash
    for column in range(targets.shape[0]):
        current = int(round(positions[column]))
        if targets[column] >= current:
            continue
        quantity = current - targets[column]
        price = _sell_price(
            inputs.execution_open[row, column], params.one_way_slippage
        )
        projected_cash += price * quantity - _commission(
            price, quantity, params.commission_multiplier
        )
    for column in range(targets.shape[0]):
        current = int(round(positions[column]))
        if targets[column] <= current:
            continue
        quantity = targets[column] - current
        price = _buy_price(
            inputs.execution_open[row, column], params.one_way_slippage
        )
        projected_cash -= price * quantity + _commission(
            price, quantity, params.commission_multiplier
        )
    return projected_cash

@njit
def _cash_feasible_targets_nb(
    row: int,
    raw_unit_base_quantities: np.ndarray,
    unit_counts: np.ndarray,
    positions: np.ndarray,
    cash: float,
    group_scales: np.ndarray,
    portfolio_scale: float,
    locked_quantities: np.ndarray,
    inputs: CallbackInputs,
    params: CallbackParams,
) -> tuple[np.ndarray, float]:
    full_targets = _targets_for_scale_nb(
        raw_unit_base_quantities,
        unit_counts,
        inputs.asset_group_ids,
        group_scales,
        portfolio_scale,
        1.0,
        locked_quantities,
        params.lot_size,
    )
    if _cash_after_targets_nb(
        row, full_targets, positions, cash, inputs, params
    ) >= -1e-9:
        return full_targets, 1.0
    lower = 0.0
    upper = 1.0
    best = _targets_for_scale_nb(
        raw_unit_base_quantities,
        unit_counts,
        inputs.asset_group_ids,
        group_scales,
        portfolio_scale,
        lower,
        locked_quantities,
        params.lot_size,
    )
    for _ in range(64):
        candidate_scale = (lower + upper) / 2.0
        candidate = _targets_for_scale_nb(
            raw_unit_base_quantities,
            unit_counts,
            inputs.asset_group_ids,
            group_scales,
            portfolio_scale,
            candidate_scale,
            locked_quantities,
            params.lot_size,
        )
        if _cash_after_targets_nb(
            row, candidate, positions, cash, inputs, params
        ) >= -1e-9:
            lower = candidate_scale
            best = candidate
        else:
            upper = candidate_scale
    return best, lower
```

实现要求：`_risk_scales_nb` 先组后组合；`_targets_for_scale_nb` 只统一乘比例并按整手向下取整；`locked_quantities` 以 `-1` 表示可调整，以非负实际持仓固定不可交易标的；`_cash_feasible_targets_nb` 把可成交卖出净收入、买入价、滑点和每笔佣金纳入现金，若比例 1 不可行则在 `[0, 1]` 上二分 64 次，返回最大共同可行比例对应目标。禁止最大余数分配和残余现金补仓。

同时物理删除 `_feasibility_mask_nb`、`_hamilton_quantities_nb`、`_maximum_hamilton_allocation_nb`、`_allocate_a1_nb`、旧掩码常量和组合波动率函数。

- [ ] **Step 4: 运行缩放测试并确认通过**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_callbacks.py -k "group_unit_scale or portfolio_unit_scale or uniform_cash_scale or permutation" -q
```

Expected: PASS。

- [ ] **Step 5: 提交纯分配内核**

```powershell
git add -- tests/local_quant_research/test_turtle_vectorbt_callbacks.py joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_callbacks.py
git diff --cached --name-only
git commit -m "实现：增加海龟全量风险缩放内核"
```

---

### Task 4: 用逐单位状态实现事件驱动全量仓位再分配

**Files:**
- Modify: `tests/local_quant_research/test_turtle_vectorbt_callbacks.py`
- Modify: `tests/local_quant_research/test_turtle_vectorbt_engine.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_callbacks.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py`

- [ ] **Step 1: 写逐单位状态与止损失败测试**

增加测试覆盖以下精确行为：

1. 入场候选数量为 `floor_to_100(signal_equity * 0.01 / signal_n)`；成交后保存单位的 `signal_n`、`base_quantity` 和实际成交价。
2. 首次成交价 10、冻结 N=1 时初始止损为 8；后续加仓实际成交价 13、该单位冻结 N=2 时候选止损为 9，共同止损变为 9。
3. 后续每日 N 改为 999 且没有新单位成交时，共同止损仍为 9。
4. 固定档位只使用首次实际成交价和首次 N；同日跨越多个档位仍只新增一个单位；第四单位后不再产生候选。
5. 停牌、涨停、整手不足、现金缩放为零或订单拒绝时，单位数、下一档和共同止损均不变化。
6. 再分配买卖改变实际持仓，但不改变单位数、单位数组、下一档或共同止损。

- [ ] **Step 2: 写全量再分配失败测试**

增加以下组合测试：

- 三个早期标的各有 4 单位、组合已达 12 单位；第四个标的产生 1 单位候选后，组合缩放为 `12/13`，三个早期目标同时按相同比例下降，晚到标的获得至少一手，证明不按时间占用预算。
- 同组两只标的合计 8 单位时组缩放为 `6/8`；组外标的不受组缩放影响。
- 同日多个候选使用统一临时单位集合计算；交换证券列顺序后还原，订单方向和目标数量一致。
- 没有入场、加仓、止损或退出事件时，即使权益、价格或 N 变化，也没有再平衡订单。
- 同一 ETF 同日退出与加仓同时满足时只保留完整退出；完整退出和再分配卖出先于所有买入。
- 候选因统一目标不能形成至少一手净买入时被移除，重新计算后其状态不推进；直到候选集合稳定。

- [ ] **Step 3: 运行行为测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_callbacks.py tests\local_quant_research\test_turtle_vectorbt_engine.py -k "unit or stop or redistribution or late_signal or no_event or candidate" -q
```

Expected: FAIL，旧状态只有一个 `standard_unit/signal_n`，且只分配当日新增订单。

- [ ] **Step 4: 替换回调状态结构**

`CallbackState` 必须改为固定四单位数组，至少包含：

```python
(
    "unit_count",
    "unit_signal_n",
    "unit_base_quantities",
    "unit_fill_prices",
    "initial_fill_price",
    "initial_signal_n",
    "common_stop",
    "next_add_index",
    "candidate_signal_n",
    "candidate_base_quantity",
    "action_codes",
    "reason_codes",
    "requested_quantities",
    "planned_quantities",
    "filled_quantities",
    "fill_prices",
    "fees",
    "state_quantities",
    "state_common_stop",
    "state_next_add_index",
    "state_unit_counts",
    "event_group_scales",
    "event_portfolio_scales",
    "event_cash_scales",
    "day_equity",
    "allocation_ready",
)
```

其中单位数组形状固定为 `(columns, 4)`；候选和状态证据按 `(rows, columns)`；组合与现金缩放按 `rows`。删除旧 `standard_unit`、单一 `signal_n` 和批次数组。

- [ ] **Step 5: 实现事件规划的稳定重算**

在 `pre_segment_func_nb` 中按以下唯一顺序实现：先识别并冻结退出与至多一个单位候选；退出覆盖同标的候选；把可交易候选加入临时单位簿；调用 4/6/12 和现金缩放；删除不能形成至少一手净新增买入的候选并循环重算；生成每只 ETF 的目标差额和动作。

动作常量只保留：

```python
ACTION_NONE = 0
ACTION_FULL_EXIT = 1
ACTION_REDISTRIBUTION_SELL = 2
ACTION_ENTRY = 3
ACTION_ADDITION = 4
ACTION_REDISTRIBUTION_BUY = 5
```

调用顺序固定为完整退出、再分配卖出、入场或加仓、再分配买入。`order_func_nb` 只执行已统一计算的差额，不再二次分配。

- [ ] **Step 6: 实现真实成交后的状态隔离**

`post_order_func_nb` 必须满足：完整退出成交才清除全部单位；再分配买卖永不改单位和止损；入场或加仓真实买入成交才把候选写入下一空单位槽，并用 `actual_fill_price - 2 * frozen_signal_n` 只上移共同止损；拒绝或零成交不推进。每日最后一列调用后写入所有状态与缩放证据。

- [ ] **Step 7: 运行回调与引擎测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_callbacks.py tests\local_quant_research\test_turtle_vectorbt_engine.py -q
```

Expected: PASS；四个官方回调均产生 `nopython_signatures`，现金始终非负。

- [ ] **Step 8: 提交核心交易实现**

```powershell
git add -- tests/local_quant_research/test_turtle_vectorbt_callbacks.py tests/local_quant_research/test_turtle_vectorbt_engine.py joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_callbacks.py joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py
git diff --cached --name-only
git commit -m "实现：完成海龟全量仓位再分配"
```

---

### Task 5: 同步额外延迟执行器且不改变基线成交时点

**Files:**
- Modify: `tests/local_quant_research/test_turtle_vectorbt_delayed.py`
- Modify: `tests/local_quant_research/test_turtle_result_adapter.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_delayed.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py`

- [ ] **Step 1: 写新动作的延迟执行失败测试**

保留既有冻结计划测试，并增加：再分配卖出优先于入场/加仓和再分配买入；再分配买卖成交不改共同止损与下一档；只有入场/加仓成交使用冻结 N 更新止损；完整退出才清空状态。基线 `additional_delay_days=0` 时仍在信号次日开盘执行，不进入额外延迟后端。

- [ ] **Step 2: 运行延迟测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_delayed.py tests\local_quant_research\test_turtle_result_adapter.py -k "delayed or redistribution" -q
```

Expected: FAIL，延迟执行器仍引用 `ACTION_RISK_REDUCTION`。

- [ ] **Step 3: 最小同步新动作语义**

删除 `ACTION_RISK_REDUCTION`；卖出集合改为 `ACTION_FULL_EXIT`、`ACTION_REDISTRIBUTION_SELL`，买入集合包含 `ACTION_ENTRY`、`ACTION_ADDITION`、`ACTION_REDISTRIBUTION_BUY`。冻结目标、费用、整手现金截断和不可交易证据保持不变；再分配动作不能调用单位止损更新分支。

- [ ] **Step 4: 运行延迟测试并确认通过**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_delayed.py tests\local_quant_research\test_turtle_result_adapter.py -k "delayed or redistribution" -q
```

Expected: PASS。

- [ ] **Step 5: 提交延迟执行同步**

```powershell
git add -- tests/local_quant_research/test_turtle_vectorbt_delayed.py tests/local_quant_research/test_turtle_result_adapter.py joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_delayed.py joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py
git diff --cached --name-only
git commit -m "修复：同步全量再分配延迟执行语义"
```

---

### Task 6: 保持标准四表并增加可分析的单位与缩放归因

**Files:**
- Modify: `tests/local_quant_research/test_turtle_result_adapter.py`
- Modify: `tests/quant_analysis/test_unified_analysis.py`
- Modify: `tests/quant_analysis/test_reporting.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/result_adapter.py`
- Modify: `scripts/research/quant_analysis/unified_analysis.py`
- Modify: `scripts/research/quant_analysis/reporting.py`

- [ ] **Step 1: 写结果包归因失败测试**

保持 `results`、`balances`、`positions`、`orders` 字段完全不变。把动作映射改为 `full_exit`、`redistribution_sell`、`entry`、`addition`、`redistribution_buy`；删除 `risk_reduction` 和 `target_volatility_reduction`。增加断言：

```python
details = json.loads(redistribution_event["details_json"])
assert details["unit_count_after"] == 4
assert details["group_scale"] == pytest.approx(0.75)
assert details["portfolio_scale"] == pytest.approx(12 / 13)
assert 0.0 < details["cash_scale"] <= 1.0
assert details["redistribution_state_changed"] is False
```

入场/加仓决策还要包含 `candidate_base_quantity`、`frozen_signal_n`、`actual_fill_price` 和成交后的共同止损。归因顶层原因码加入 `full_position_redistribution`，删除 `forced_risk_reduction` 和 `risk_gate_block` 的生产路径。

- [ ] **Step 2: 写 Vibe 风险指标失败测试**

将 `_risk_metrics` 测试配置替换为新风险字段，断言分析不再读取旧仓位上限或目标波动率，并输出：

```python
assert metrics["maximum_security_weight"] == pytest.approx(0.4)
assert metrics["maximum_asset_group_weight"] == pytest.approx(0.4)
assert metrics["maximum_planned_loss_ratio"] == pytest.approx(0.01)
assert metrics["maximum_effective_risk_units"] == pytest.approx(12.0)
assert metrics["maximum_portfolio_unit_utilization"] == pytest.approx(1.0)
assert metrics["redistribution_event_count"] == 1
```

没有海龟扩展字段的聚宽结果应返回 `None/0`，而不是失败，以保持现有聚宽结果零改动可读。

- [ ] **Step 3: 运行结果与分析测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_result_adapter.py tests\quant_analysis\test_unified_analysis.py tests\quant_analysis\test_reporting.py -q
```

Expected: FAIL，适配器和分析仍依赖旧风险字段。

- [ ] **Step 4: 更新结果适配器**

读取 `state_unit_counts`、`event_group_scales`、`event_portfolio_scales`、`event_cash_scales` 和候选基础数量，写入现有 `details_json`；不新增第五张标准表，不改变标准 Schema（结构约束）。再分配订单的买卖方向仅由新动作集合决定；再分配成交后的 `state_changed` 只表示实际持仓变化，另以 `redistribution_state_changed=False` 明确海龟单位状态未变。

- [ ] **Step 5: 把 Vibe 风险分析改成通用暴露与可选单位证据**

在 `_risk_metrics` 中删除对 `security_value_cap`、`asset_group_value_cap`、`portfolio_value_cap`、`portfolio_risk_cap`、`target_volatility` 的索引。保留实际平均仓位、现金、最大单标的/资产组权重、60 日已实现波动率、订单、换手、费用、退出收益和止损事件；把计划风险改成 `planned_loss / equity` 的 `maximum_planned_loss_ratio`。从归因 `details_json` 可选读取 `effective_risk_units`、`portfolio_unit_cap` 和再分配标记，生成上述三个单位指标；缺失时返回 `None/0`。

同步 `reporting.py` 的风险表标签，删除“超过旧上限”“超过目标波动率”“强制风险约束”行，增加“最高计划损失比例”“最高有效 N 风险单位”“组合单位预算最高利用率”“全量再分配事件”。

- [ ] **Step 6: 运行结果、分析和报告测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_result_adapter.py tests\quant_analysis\test_unified_analysis.py tests\quant_analysis\test_reporting.py -q
```

Expected: PASS，标准四表字段集合不变，聚宽形状夹具仍可读取。

- [ ] **Step 7: 提交结果与分析兼容**

```powershell
git add -- tests/local_quant_research/test_turtle_result_adapter.py tests/quant_analysis/test_unified_analysis.py tests/quant_analysis/test_reporting.py joinquant/strategies/strategy-003/research/turtle_etf/result_adapter.py scripts/research/quant_analysis/unified_analysis.py scripts/research/quant_analysis/reporting.py
git diff --cached --name-only
git commit -m "分析：支持海龟N风险单位归因"
```

---

### Task 7: 同步规格、研究方案和执行身份并清除旧生产路径

**Files:**
- Modify: `docs/superpowers/specs/2026-07-16-turtle-full-position-redistribution-design.md`
- Modify: `docs/research/2026-07-13-turtle-etf-system-final-plan.md`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/proposal.md`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/design.md`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/tasks.md`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/specs/turtle-etf-local-research/spec.md`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/specs/standard-strategy-analysis-data/spec.md`
- Modify: `joinquant/strategies/strategy-003/research/code-identity.json`

- [ ] **Step 1: 先写/更新文档契约测试或严格规格断言**

在现有 `test_contract_fixtures.py` 和 `test_turtle_vectorbt_engine.py` 中断言当前设计状态为“已确认”，OpenSpec 明确 11 ETF、55/20/20、0.5N、2N、4/6/12、事件驱动全量再分配、单场景小于 180 秒、无旧对照和无本次稳健性运行；执行身份文件中的回调摘要必须等于实际文件 SHA256（摘要）。

- [ ] **Step 2: 运行契约与 OpenSpec 校验并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_contract_fixtures.py tests\local_quant_research\test_turtle_vectorbt_engine.py -q
openspec validate build-turtle-etf-local-research-workflow --strict
```

Expected: 至少一项 FAIL，文档和摘要仍描述旧实现。

- [ ] **Step 3: 同步权威文档**

把设计状态改为“已确认”；原始研究方案和 OpenSpec 使用同一规则。明确通用 Skill 仍单次运行、策略分析独立、聚宽正式复核不在范围、历史 `.local` 不改、17 ETF 不进基线、此次只运行一个新基线。

- [ ] **Step 4: 核对旧生产符号已物理删除**

Run:

```powershell
rg -n "_allocate_a1_nb|_hamilton_quantities_nb|ACTION_RISK_REDUCTION|REASON_TARGET_VOLATILITY_REDUCTION|_portfolio_volatility_nb|mandatory_risk_reduction|a1_uniform_completion" joinquant/strategies/strategy-003/research/turtle_etf joinquant/strategies/strategy-003/research/baseline.json scripts/research/quant_analysis
```

Expected: 无输出。旧字段名称只允许出现在“拒绝旧字段”的测试常量和已确认设计的删除说明中，不得存在于生产配置、参数、回调、结果适配或分析执行分支。

- [ ] **Step 5: 更新执行身份摘要**

用 `Get-FileHash -Algorithm SHA256` 计算 `code-identity.json.files` 中每个实际文件，并用 `apply_patch` 更新对应摘要；`execution.callbacks_sha256` 必须等于 `vectorbt_callbacks.py` 摘要。删除已经不存在的文件条目，新增本变更实际进入执行身份但尚未登记的文件。

- [ ] **Step 6: 运行契约与严格规格校验**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_contract_fixtures.py tests\local_quant_research\test_turtle_vectorbt_engine.py -q
openspec validate build-turtle-etf-local-research-workflow --strict
```

Expected: PASS。

- [ ] **Step 7: 提交文档和执行身份**

```powershell
git add -- docs/superpowers/specs/2026-07-16-turtle-full-position-redistribution-design.md docs/research/2026-07-13-turtle-etf-system-final-plan.md openspec/changes/build-turtle-etf-local-research-workflow joinquant/strategies/strategy-003/research/code-identity.json tests/local_quant_research/test_contract_fixtures.py tests/local_quant_research/test_turtle_vectorbt_engine.py
git diff --cached --name-only
git commit -m "文档：同步海龟全量再分配规格"
```

---

### Task 8: 完整验证、真实 11 ETF 基线与 Vibe 研究报告

**Files:**
- Create: `tests/local_quant_research/test_turtle_e2e.py`
- Modify: `tests/local_quant_research/test_turtle_single_scenario.py`
- Modify: `tests/test_skill_layout.py`
- Modify: `.build-and-verify/config.json`
- Create: `docs/research/2026-07-16-turtle-full-position-redistribution-baseline-report.md`
- Runtime only: `.local/quant-research/strategy-003/` 下由运行器生成的新不可变 run 目录

- [ ] **Step 1: 扩充发布入口 E2E 断言**

新增 `test_turtle_e2e.py`：用真实共享行情存储接口在临时目录建立足够覆盖 55/20/20 窗口的小型 Parquet 快照，经通用 CLI 启动真实 `strategy-003` 入口，并断言只有一个场景、目标包包含标准四表和一份海龟归因、至少发生一次后到趋势触发的再分配、单位状态不被再分配改写、冷/热摘要一致、两次均小于 180 秒、基准工作目录已清理、最终 `next_action=return_to_caller`。保留 `test_generic_e2e.py` 作为非海龟通用前向验证，不把海龟规则写入其中。

同步 `.build-and-verify/config.json` 的 `verify.local-quant-research-e2e` 命令，使它同时运行 `test_generic_e2e.py` 与 `test_turtle_e2e.py`；先更新 `tests/test_skill_layout.py` 的精确命令断言。

- [ ] **Step 2: 运行 E2E 并修到通过**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_generic_e2e.py tests\local_quant_research\test_turtle_e2e.py tests\local_quant_research\test_turtle_single_scenario.py tests\test_skill_layout.py -q
```

Expected: PASS。若失败，只修复本计划规则，不放宽断言、不切换回旧路径。

- [ ] **Step 3: 运行本地研究相关完整回归**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research tests\quant_analysis -q
.\.venv\Scripts\python.exe .build-and-verify\runtime\build_and_verify.py verify --project .
openspec validate --all --strict --no-interactive
```

Expected: 全部 PASS。`verify` 使用快速受影响检查；不运行未经用户额外授权的 `--full`。

- [ ] **Step 4: 从正式 Skill 用户入口运行唯一真实新基线**

确认 `project-run.json` 指向 11 ETF 快照和新 `baseline.json` 后运行：

```powershell
.\.venv\Scripts\python.exe scripts\research\local_quant_research\cli.py run --config joinquant\strategies\strategy-003\research\project-run.json
```

Expected: `complete`、`next_action=return_to_caller`；生成一个新不可变 run id（运行标识）；`performance.json` 显示 `result_match=true`、冷/热均小于 180 秒、临时目录全部删除。不得复用旧代码身份的历史结果，也不得运行旧基线对照。

- [ ] **Step 5: 用现有 Vibe 分析函数只读新标准结果包**

从新运行的 `backtests/local-baseline` 读取标准四表和海龟归因，调用 `scripts.research.quant_analysis.unified_analysis` 现有的收益、风险、基准、持仓和贡献计算函数；基准固定为 `CSI300_CNY_TOTAL_RETURN`（沪深300人民币总收益）和 `NASDAQ100_CNY_TOTAL_RETURN`（纳斯达克100人民币总收益）。分析产物写入该 run 的独立 `.local` 分析目录，至少包含：

- 累计收益、CAGR（复合年化收益率）、年化波动率、Sharpe（夏普比率）、Sortino（索提诺比率）、最大回撤、回撤持续期、Calmar（卡玛比率）；
- 对两个基准的累计超额、年化超额、Beta（贝塔）、Alpha（阿尔法）、相关性和共同样本说明；
- 平均/中位/最高仓位、低于半仓比例、接近满仓比例、现金比例、换手和费用；
- 逻辑单位、有效 N 单位、组/组合/现金缩放利用率、再分配次数、止损与趋势退出次数；
- 逐 ETF、逐资产组、逐时间段贡献和现金/费用残差；
- 最主要正面证据、反对证据、数据与公司行动近似限制；
- 明确写出“本次未运行稳健性矩阵，不能宣称稳健性通过”。

- [ ] **Step 6: 编写并核对完整研究报告**

用 `apply_patch` 创建报告，引用真实 run id、快照摘要、代码身份、参数摘要、性能门禁和结果包路径。所有数字必须来自新标准结果包或 Vibe 输出，不手算、不猜测；结论必须给出“推荐/不推荐继续进入聚宽复核”的明确建议，但最终状态为“等待人工确认”，不得代替聚宽正式裁决。

- [ ] **Step 7: 清理临时产物并复核工作区**

删除本次手工分析产生的临时脚本、临时 JSON 和非不可变工作目录；保留正式 `.local` run 和分析证据。运行：

```powershell
git status --short
Get-ChildItem -Recurse -Force -Include *.tmp,.benchmark-work -Path .local,joinquant\strategies\strategy-003\research -ErrorAction SilentlyContinue
```

Expected: 没有本次临时残留；既有无关脏文件保持原样。

- [ ] **Step 8: 提交 E2E 与研究报告**

```powershell
git add -- tests/local_quant_research/test_turtle_e2e.py tests/local_quant_research/test_turtle_single_scenario.py tests/test_skill_layout.py .build-and-verify/config.json docs/research/2026-07-16-turtle-full-position-redistribution-baseline-report.md
git diff --cached --name-only
git commit -m "报告：完成海龟全量再分配本地研究"
```

## Final Self-Review Checklist

- [ ] 逐条对照已确认设计的 1 至 10 节，没有遗漏 11 ETF、6 分组、55/20/20、0.5N、2N、4/6/12、次日开盘、一天一单位和只上移止损。
- [ ] 所有生产代码只存在一条全量再分配路径；没有 A1、Hamilton（最大余数）、资金仓位上限、协方差交易门槛或目标波动率交易残留。
- [ ] 计划中的每个测试先失败后通过，命令和预期均明确，每个代码片段都能直接实现。
- [ ] 回调、引擎、延迟执行器、结果适配器和分析器的动作名、字段名、数组形状一致。
- [ ] 标准四表和聚宽现有结果读取契约没有改变；海龟专用信息只在归因扩展中增加。
- [ ] 本地研究 Skill 仍只执行一个场景；实际只运行一个 11 ETF 新基线，没有旧方案对照、17 ETF 或稳健性矩阵。
- [ ] 冷/热运行都小于 180 秒且结果摘要一致；完整用户入口 E2E 已通过。
- [ ] 报告的收益、回撤、Alpha/Beta（阿尔法/贝塔）、仓位、风险预算和归因数字均能追溯到新 run 证据。
- [ ] `.local` 历史证据未被改写或删除，临时产物已删除，无关工作区改动未被提交。
