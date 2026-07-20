# Turtle Live N Risk Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让海龟 ETF（交易型开放式指数基金）每次目标仓位同时满足单资产 4N、强相关组 6N、全组合 12N 的实际计划止损金额预算。

**Architecture:** 在现有组缩放、组合缩放与现金缩放之间增加一个共享止损风险截断。每个资产只新增“当前持仓成本”状态；风险预算继续来自存续单位冻结 N，风险截断不改变逻辑单位或公共止损。

**Tech Stack:** Python（编程语言）3.12、NumPy（数值数组）、Numba（即时编译）、vectorbt（向量回测框架）、pytest（测试框架）、DuckDB（嵌入式分析数据库）。

## Global Constraints

- 单资产原始单位不超过 4，强相关组有效单位不超过 6，全组合有效单位不超过 12。
- 三层计划止损金额预算必须同时成立；任一层超限都只能减小目标。
- 每个存续单位沿用自己的冻结 N；禁止用最新 N 重写历史单位。
- 现金缩放只能继续减小风险截断后的目标。
- 不恢复旧风险字段，不新增依赖，不改信号和交易频率。
- 只使用项目 `.venv`（虚拟环境）；本地结果不冒充 JoinQuant（聚宽）正式回测。
- 本次授权不包含 Git（版本管理）提交、分支或 PR（拉取请求）。

---

### Task 1: 共享 4-6-12 实际止损风险截断

**Files:**
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/_kernel.py`
- Test: `tests/local_quant_research/test_turtle_vectorbt_callbacks.py`

**Interfaces:**
- Consumes: 已有 `unit_signal_n`、`unit_base_quantities`、`common_stop`、组缩放、组合缩放、现金目标和 `FillEvent`（成交事件）。
- Produces: `_planned_loss_nb(...) -> float`、`_risk_capped_target_nb(...) -> int`，以及 trace（跟踪证据）中的 `event_risk_budgets`、`event_planned_losses`、`event_risk_cap_applied`。

- [ ] **Step 1: 写一个失败的根因回归测试**

在 `test_turtle_vectorbt_callbacks.py` 增加一个测试，直接构造三层预算和目标：

```python
def test_stop_risk_cap_enforces_asset_group_and_portfolio_budgets() -> None:
    cap = getattr(
        callbacks._risk_cap_targets_nb,
        "py_func",
        callbacks._risk_cap_targets_nb,
    )
    targets = np.asarray([400, 400, 400], dtype=np.int64)
    positions = np.asarray([300.0, 300.0, 300.0])
    costs = np.asarray([3000.0, 3000.0, 3000.0])
    prices = np.asarray([20.0, 20.0, 20.0])
    stops = np.asarray([8.0, 8.0, 8.0])
    budgets = np.asarray([800.0, 400.0, 400.0])
    groups = np.asarray([0, 0, 1], dtype=np.int64)
    locked = np.asarray([-1, -1, -1], dtype=np.int64)
    allowances = np.empty(3, dtype=np.float64)
    capped = np.zeros(3, dtype=np.bool_)

    cap(
        targets,
        positions,
        costs,
        prices,
        stops,
        budgets,
        groups,
        0,
        2,
        locked,
        100,
        allowances,
        capped,
    )

    losses = [
        callbacks._planned_loss_nb.py_func(
            int(targets[i]), int(positions[i]), costs[i], prices[i], stops[i]
        )
        for i in range(3)
    ]
    assert all(loss <= budget + 1e-9 for loss, budget in zip(losses, budgets))
    assert sum(losses[:2]) <= sum(budgets[:2]) + 1e-9
    assert sum(losses) <= sum(budgets) + 1e-9
    assert capped.any()
```

- [ ] **Step 2: 运行测试并确认它因共享截断不存在而失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/local_quant_research/test_turtle_vectorbt_callbacks.py::test_stop_risk_cap_enforces_asset_group_and_portfolio_budgets -q
```

Expected: `FAIL`，提示 `_risk_cap_targets_nb` 不存在。

- [ ] **Step 3: 用最小函数实现计划损失和整手安全数量**

在 `_kernel.py` 的目标计算函数旁增加：

```python
@njit
def _planned_loss_nb(
    target: int,
    current: int,
    position_cost: float,
    buy_price: float,
    stop: float,
) -> float:
    if target <= 0:
        return 0.0
    if not _finite_positive(stop) or current < 0:
        return np.inf
    if current > 0 and (not np.isfinite(position_cost) or position_cost < 0.0):
        return np.inf
    if target <= current:
        projected_cost = position_cost * target / current
    else:
        if not _finite_positive(buy_price):
            return np.inf
        projected_cost = position_cost + (target - current) * buy_price
    return max(projected_cost - target * stop, 0.0)


@njit
def _risk_capped_target_nb(
    target: int,
    current: int,
    position_cost: float,
    buy_price: float,
    stop: float,
    allowance: float,
    lot_size: int,
) -> int:
    if target <= 0 or not np.isfinite(allowance) or allowance < 0.0:
        return 0
    if _planned_loss_nb(target, current, position_cost, buy_price, stop) <= allowance + 1e-9:
        return target
    average_cost = position_cost / current if current > 0 else buy_price
    existing_distance = max(average_cost - stop, 0.0)
    safe_existing = current
    if existing_distance > 0.0:
        safe_existing = int((allowance / existing_distance) // lot_size) * lot_size
    if safe_existing < current:
        return min(target, safe_existing)
    current_loss = _planned_loss_nb(current, current, position_cost, buy_price, stop)
    added_distance = max(buy_price - stop, 0.0)
    if added_distance <= 0.0:
        return target
    safe_added = int(((allowance - current_loss) / added_distance) // lot_size) * lot_size
    return min(target, current + max(safe_added, 0))
```

实现 `_risk_cap_targets_nb`：先复制每资产最终预算；若不可交易仓位占用超额组预算，则同比缩小组内可交易资产预算；再对全组合执行同样处理；最后逐资产调用 `_risk_capped_target_nb`。禁止把未使用预算借给资产突破自己的 4N 分配。

- [ ] **Step 4: 把共享截断接入每一次现金目标生成**

扩展 `CallbackState`：

```python
"position_costs",
"event_risk_budgets",
"event_planned_losses",
"event_risk_cap_applied",
"scratch_risk_budgets",
"scratch_risk_allowances",
"scratch_risk_stops",
"scratch_risk_cap_applied",
```

在每次组缩放和组合缩放后计算冻结金额预算。候选入场的预计止损为 `buy_price - stop_n × candidate_signal_n`；候选加仓取它与原公共止损的较高值；再分配使用原公共止损。`_cash_feasible_targets_nb` 每次生成目标后都调用 `_risk_cap_targets_nb`，因此现金二分不能重新放大已截断目标。

成交接受后维护成本：买入增加 `size × price`；卖出按剩余数量同比降低；清仓归零。再分配成交只改数量与成本，不改逻辑单位、冻结 N 和公共止损。

- [ ] **Step 5: 运行定向测试和整个回调测试文件**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/local_quant_research/test_turtle_vectorbt_callbacks.py -q
```

Expected: 全部 `PASS`；新增测试证明资产、组和组合三层金额不等式同时成立。

- [ ] **Step 6: 检查本任务改动，不提交 Git**

Run:

```powershell
git diff --check -- joinquant/strategies/strategy-003/research/turtle_etf/_kernel.py tests/local_quant_research/test_turtle_vectorbt_callbacks.py
```

Expected: 无输出；因用户未授权，跳过提交。

---

### Task 2: 延迟成交价重新校验

**Files:**
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/_delayed.py`
- Test: `tests/local_quant_research/test_turtle_vectorbt_delayed.py`

**Interfaces:**
- Consumes: Task 1 的 `_planned_loss_nb`、`_risk_capped_target_nb`、主运行 `event_risk_budgets`。
- Produces: 延迟成交按实际价格截断后的订单；卖出失败时不允许后续买入消耗同组或组合预算。

- [ ] **Step 1: 写延迟再分配买入的失败测试**

增加场景：主计划允许按 10 元买 200 份，延迟执行价升到 20 元，公共止损仍为 8 元，计划风险预算只能覆盖 100 份。断言实际成交 100 份、整手不破坏、调整码为风险截断，并且逻辑单位与公共止损不因再分配买入改变。

- [ ] **Step 2: 运行测试并确认延迟路径会多买而失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/local_quant_research/test_turtle_vectorbt_delayed.py::test_delayed_redistribution_buy_is_capped_by_frozen_risk_budget -q
```

Expected: `FAIL`，实际成交仍为 200 或风险调整码不存在。

- [ ] **Step 3: 复用 Task 1 的金额函数截断延迟买入**

向 `DelayedInputs` 传入主计划逐资产风险预算，向 `DelayedState` 增加持仓成本。新增 `ADJUST_RISK_TRUNCATED = 5`。在现金可买数量计算之前，以延迟实际买价、当前成本、当前或候选公共止损调用 `_risk_capped_target_nb`；取风险允许数量与现金允许数量的较小值。

同一事件仍按“退出、再分配卖、入场/加仓、再分配买”顺序。不可成交卖出保留其实际风险占用；若它耗尽对应预算，后续买入数量为零。

- [ ] **Step 4: 运行延迟测试文件**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/local_quant_research/test_turtle_vectorbt_delayed.py -q
```

Expected: 全部 `PASS`。

---

### Task 3: 审计证据、端到端回归和同快照重跑

**Files:**
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/_attribution.py`
- Test: `tests/local_quant_research/test_turtle_result_adapter.py`
- Verify: `tests/local_quant_research/test_turtle_e2e.py`

**Interfaces:**
- Consumes: Task 1/2 的逐资产风险预算、计划损失和截断标志。
- Produces: 归因 `details_json` 中可直接汇总验证的资产、组、组合风险字段。

- [ ] **Step 1: 写审计字段失败测试**

在决策事件的 `details_json` 断言存在：

```python
assert details["risk_budget_amount"] >= 0.0
assert details["projected_planned_loss"] <= details["risk_budget_amount"] + 1e-9
assert details["group_projected_planned_loss"] <= details["group_risk_budget_amount"] + 1e-9
assert details["portfolio_projected_planned_loss"] <= details["portfolio_risk_budget_amount"] + 1e-9
assert isinstance(details["risk_cap_applied"], bool)
```

- [ ] **Step 2: 运行测试并确认字段缺失而失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/local_quant_research/test_turtle_result_adapter.py -q
```

Expected: `FAIL`，缺少 `risk_budget_amount`。

- [ ] **Step 3: 只扩展现有归因明细**

从 trace（跟踪证据）读取逐资产预算、计划损失和截断标志；用 `asset_group_ids` 汇总同一行同一组与全组合金额，写入现有 `_details(...)`。不新增表、不改公共结果契约。

- [ ] **Step 4: 运行海龟相关测试和完整本地主流程**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/local_quant_research/test_turtle_vectorbt_callbacks.py tests/local_quant_research/test_turtle_vectorbt_delayed.py tests/local_quant_research/test_turtle_result_adapter.py tests/local_quant_research/test_turtle_e2e.py -q
```

Expected: 全部 `PASS`，端到端测试从策略入口生成并校验完整结果。

- [ ] **Step 5: 使用相同不可变快照重跑单场景**

Run:

```powershell
.\.venv\Scripts\python.exe scripts\research\local_quant_research\cli.py run --config joinquant\strategies\strategy-003\research\project-run.json
```

Expected: `status=complete`、新 `run_id`、快照仍为 `e88238cca420a8ae66b90adb6cda4dd6c38a07390a13b8ac2f471e534742e33e`，冷启动和预热摘要一致。

- [ ] **Step 6: 对比基线和新结果**

以基线 `d10991ad7e0ec53841a73d70accafc197871b563fb47b8ec5038a839b9b98e79` 为控制组，报告累计收益率、CAGR（复合年化收益率）、最大回撤、Calmar（卡玛比率）、换手率、最大计划损失比例、风险截断次数和资产贡献变化。直接扫描新归因明细，验证除无法成交的既有超额仓位外，所有事件均满足 4-6-12 三层金额不等式。

- [ ] **Step 7: 最终检查，不提交 Git**

Run:

```powershell
git diff --check
git status --short
```

Expected: 只有本计划声明的文件发生变化；因用户未授权，跳过提交、分支和 PR。
