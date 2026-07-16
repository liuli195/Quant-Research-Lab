# 海龟 ETF 独立资产扩展实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用六只预先冻结的候选 ETF（交易型开放式指数基金）验证“增加独立风险来源”能否解决现有海龟系统趋势利用不足，并输出真实行情、最多 16 个本地回测结果包、完整分析报告和人工可确认的推荐结论。

**Architecture:** 保留现有单场景 `run-local-quant-research` Skill（本地量化研究技能）和标准结果包。共享行情层新增连续经济收益、候选筛选和跨快照重叠校验；海龟项目层新增候选场景矩阵和冻结订单延迟执行；通用分析层按每场景资产池消费真实删除、真实成本结果，并增加有效秩和趋势利用分析。所有场景使用相同不可变行情批次、截止日和口径，但使用与自身证券集合完全匹配的内容寻址快照。

**Tech Stack:** Python 3.12（编程语言）、vectorbt 1.1.0（向量化回测框架）、Numba（即时编译）、NumPy（数组计算）、Pandas（数据处理）、PyArrow/Parquet（列式存储）、DuckDB `:memory:`（内存数据库）、JSON Schema（结构契约）、Pytest（测试框架）、JoinQuant（聚宽）研究环境、Vibe-Trading 0.1.10（氛围量化单体审计）。

## Global Constraints

- 所有 Python 命令必须使用 `.\.venv\Scripts\python.exe`；不得使用系统 Python。
- 正式回测和模拟交易只在 JoinQuant（聚宽）云端运行；本计划只做本地探索性研究，不修改正式策略资产池。
- 候选固定为 `159980.XSHE`、`159981.XSHE`、`159985.XSHE`、`511260.XSHG`、`513030.XSHG`、`513800.XSHG`，按代码排序；读取回测收益后不得替换、增删或放宽筛选门槛。
- 行情截止日固定为 `2026-07-13`，`fq=None`、`skip_paused=False`，字段固定为 `MARKET_DATA_FIELDS`；必须同步公司行动证据。
- 成交额只用于资产池前置筛选；海龟配置、输入、回调、原因码和订单不得出现运行时流动性门槛或“单笔订单不超过成交额 1%”。
- 主场景显式使用 `commission_multiplier=1.0`、`one_way_slippage=0.0005`；不得依赖引擎当前的零滑点默认值。
- 本地研究 Skill 每次只运行一个场景并返回 `next_action=return_to_caller`；最多 16 个场景由主 Agent（主代理）逐次调用，Skill 内不得循环、聚合或分析。
- 单个场景冷启动和预热都必须不超过 180 秒；超时、摘要不一致或标准结果包不完整均为失败。
- 延迟压力采用“两段式冻结原即时订单”：原执行日完成 A1（统一完成比例）和风险门禁，冻结方向、目标数量、原因码与 N；延迟日只按开盘和成本机械成交，不读取延迟日新信号或新风险估计。
- 标准结果包物理结构不变；延迟证据写入现有订单时间字段和 attribution（归因）`details_json`。
- Vibe-Trading 只允许公开单体 `run` 入口做只读定性复核；禁止 `--swarm-run`、Vibe 回测和存在前视偏差的组合优化器；确定性数值和门槛始终是唯一裁判。
- 只保留 Parquet、清单、摘要、快照、标准结果包和分析交付；聚宽/本地传输 CSV、Vibe 临时提示文件、预热副本和失败临时目录必须在完成前清理。
- 当前工作树已有用户改动。每个提交步骤都是授权门禁：执行时没有用户新的明确提交授权，就停在未暂存状态，不得提交、切分支或覆盖用户改动。

---

### Task 1: 同步 Comet 与 OpenSpec 规划契约

**Files:**
- Modify: `.comet.yaml`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/proposal.md`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/design.md`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/tasks.md`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/specs/local-quant-research-workflow/spec.md`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/specs/standard-strategy-analysis-data/spec.md`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/specs/turtle-etf-local-research/spec.md`
- Reference: `docs/superpowers/specs/2026-07-16-turtle-independent-asset-expansion-design.md`
- Reference: `docs/superpowers/plans/2026-07-16-turtle-independent-asset-expansion.md`

**Interfaces:**
- Consumes: 已确认设计文档中的六只候选、最多 16 次真实场景、精确快照、有效秩和完整验收门槛。
- Produces: OpenSpec（开放规格）可严格校验的增量需求，以及指向本设计和本计划的 Comet（工作流）运行态。

- [ ] **Step 1: 记录更新前缺口**

Run:

```powershell
rg -n "159985|expanded-universe|最多 16|冻结订单|有效秩" openspec/changes/build-turtle-etf-local-research-workflow .comet.yaml
```

Expected: 当前 OpenSpec 不完整覆盖上述五项，命令不能同时命中全部契约。

- [ ] **Step 2: 更新提案、设计、规格与任务**

在三个 delta spec（增量规格）中分别增加：

```markdown
#### Scenario: 独立资产扩展使用真实单场景矩阵
- **WHEN** 主 Agent 已冻结候选前置筛选结果
- **THEN** 系统生成一个原 11 只基线、一个完整扩展、逐只删除、逐扩展切片删除和五个成本执行压力场景
- **AND** 每个场景由本地研究 Skill 独立运行一次并生成一个标准结果包
- **AND** 全部通过候选时场景总数为 16

#### Scenario: 不同资产池使用可比较的精确快照
- **WHEN** 两个场景的证券集合不同
- **THEN** 每个场景使用与自身证券集合完全匹配的快照
- **AND** 快照共享相同不可变批次、截止日、字段和价格口径
- **AND** 重叠证券行情及公司行动摘要必须完全一致
```

在 `tasks.md` 追加本计划 Task 2—12 的逐项检查框；不改写已完成任务的历史证据。

- [ ] **Step 3: 更新 Comet 运行态引用**

将 `.comet.yaml` 中两项改为：

```yaml
design_doc: docs/superpowers/specs/2026-07-16-turtle-independent-asset-expansion-design.md
plan: docs/superpowers/plans/2026-07-16-turtle-independent-asset-expansion.md
```

保留 `phase: build`、`build_mode: executing-plans`、`tdd_mode: tdd`、`review_mode: thorough` 和 `verify_mode: full`。

- [ ] **Step 4: 严格验证规划产物**

Run:

```powershell
openspec validate build-turtle-etf-local-research-workflow --strict
git diff --check
```

Expected: OpenSpec 输出有效且无错误；`git diff --check` 无输出并以 0 退出。

- [ ] **Step 5: 授权后提交规划同步**

```powershell
git add .comet.yaml `
  openspec/changes/build-turtle-etf-local-research-workflow/proposal.md `
  openspec/changes/build-turtle-etf-local-research-workflow/design.md `
  openspec/changes/build-turtle-etf-local-research-workflow/tasks.md `
  openspec/changes/build-turtle-etf-local-research-workflow/specs/local-quant-research-workflow/spec.md `
  openspec/changes/build-turtle-etf-local-research-workflow/specs/standard-strategy-analysis-data/spec.md `
  openspec/changes/build-turtle-etf-local-research-workflow/specs/turtle-etf-local-research/spec.md `
  docs/superpowers/specs/2026-07-16-turtle-independent-asset-expansion-design.md `
  docs/superpowers/plans/2026-07-16-turtle-independent-asset-expansion.md
git commit -m "规划：固化独立资产扩展研究契约"
```

Expected: 只有获得用户明确提交授权后执行；否则记录“未授权提交”并继续保留未暂存改动。

### Task 2: 提取共享连续经济收益能力

**Files:**
- Create: `scripts/research/market_data/economic_returns.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_inputs.py:125-337,459-573`
- Modify: `joinquant/strategies/strategy-003/research/code-identity.json`
- Create: `tests/local_quant_research/test_market_data_economic_returns.py`
- Modify: `tests/local_quant_research/test_turtle_vectorbt_inputs.py:270-480`

**Interfaces:**
- Consumes: `SnapshotView`、未复权行情、公司行动清单。
- Produces: `derive_continuous_prices(...) -> ContinuousPriceResult` 和 `snapshot_return_panel(...) -> pd.DataFrame`；海龟输入和通用分析共用同一实现。

- [ ] **Step 1: 写共享收益失败测试**

```python
def test_shared_continuous_returns_match_turtle_covariance_returns() -> None:
    result = derive_continuous_prices(
        _corporate_action_frame(),
        security="ETF-A",
        corporate_actions=[_corporate_action(event_type="cash_dividend")],
    )
    expected = result.frame["close"] / result.frame["pre_close"] - 1.0
    assert result.returns.to_numpy() == pytest.approx(expected.to_numpy())
    assert result.applications[0].security == "ETF-A"


def test_continuous_returns_reject_unreconciled_corporate_action() -> None:
    with pytest.raises(EconomicReturnError, match="cannot be reconciled"):
        derive_continuous_prices(
            _unexplained_basis_change_frame(),
            security="ETF-A",
            corporate_actions=[],
        )
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_economic_returns.py -q
```

Expected: FAIL，提示 `scripts.research.market_data.economic_returns` 不存在。

- [ ] **Step 3: 实现共享接口并迁移原算法**

`economic_returns.py` 固定公开接口：

```python
@dataclass(frozen=True)
class ContinuousPriceResult:
    frame: pd.DataFrame
    returns: pd.Series
    applications: tuple[CorporateActionApplication, ...]


def derive_continuous_prices(
    frame: pd.DataFrame,
    *,
    security: str,
    corporate_actions: Sequence[Mapping[str, object]],
) -> ContinuousPriceResult:
    normalized, applications = _derive_forward_only_continuity(
        frame, security=security, corporate_actions=corporate_actions
    )
    returns = normalized["close"] / normalized["pre_close"] - 1.0
    return ContinuousPriceResult(
        frame=normalized,
        returns=returns.astype("float64"),
        applications=tuple(applications),
    )


def snapshot_return_panel(
    snapshot: SnapshotView,
    securities: Sequence[str] | None = None,
) -> pd.DataFrame:
    selected = tuple(sorted(securities or {str(row["security"]) for row in snapshot.rows}))
    columns = {}
    for security in selected:
        frame = pd.DataFrame(row for row in snapshot.rows if row["security"] == security)
        actions = [row for row in snapshot.corporate_actions if row["security"] == security]
        result = derive_continuous_prices(
            frame, security=security, corporate_actions=actions
        )
        columns[security] = pd.Series(
            result.returns.to_numpy(),
            index=pd.to_datetime(result.frame["date"]).dt.normalize(),
        )
    return pd.DataFrame(columns).sort_index()
```

把 `CorporateActionApplication` 数据类和私有连续价格算法一并迁入 `economic_returns.py`；`vectorbt_inputs.py` 只导入共享类型和函数，既有结果适配器继续通过同名属性读取。其协方差仍使用共享结果的 `continuous_close / continuous_pre_close - 1`。

- [ ] **Step 4: 验证海龟输入行为没有漂移**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_economic_returns.py tests\local_quant_research\test_turtle_vectorbt_inputs.py -q
```

Expected: PASS；拆分、现金分红、停牌延后、未来信息和协方差收益既有测试全部通过。

- [ ] **Step 5: 更新代码身份并授权后提交**

重新计算 `economic_returns.py`、`vectorbt_inputs.py` 及依赖摘要，写入 `code-identity.json` 后运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_strategy_identity.py -q
git add scripts/research/market_data/economic_returns.py `
  joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_inputs.py `
  joinquant/strategies/strategy-003/research/code-identity.json `
  tests/local_quant_research/test_market_data_economic_returns.py `
  tests/local_quant_research/test_turtle_vectorbt_inputs.py
git commit -m "实现：共享公司行动连续收益口径"
```

Expected: 测试通过；提交仍受用户明确授权门禁控制。

### Task 3: 增加候选筛选和跨快照重叠校验

**Files:**
- Create: `scripts/research/market_data/candidate_screen.py`
- Modify: `scripts/research/market_data/query.py:19-170`
- Modify: `scripts/research/market_data/joinquant_export.py:18-260`
- Create: `tests/local_quant_research/test_market_data_candidate_screen.py`
- Modify: `tests/local_quant_research/test_market_data_query.py`
- Modify: `tests/local_quant_research/test_joinquant_export.py`

**Interfaces:**
- Consumes: 候选批次行情、公司行动、聚宽导出证券元数据、共享连续经济收益。
- Produces: `screen_candidates(...) -> CandidateScreenResult`、`write_candidate_screen(...) -> Path`、`validate_snapshot_overlap(...) -> SnapshotOverlapEvidence`，以及带上市/覆盖证据的 `export_result`。

- [ ] **Step 1: 写候选筛选失败测试**

```python
RULE = CandidateScreenRule(
    min_valid_days=750,
    money_lookback_days=20,
    min_median_money=100_000_000.0,
)


def test_screen_counts_only_valid_non_paused_days_and_freezes_all_results() -> None:
    result = screen_candidates(
        rows=_candidate_rows(valid_days=750, median_money=100_000_000.0),
        corporate_actions=(),
        official_security_metadata=_official_metadata(),
        requested_securities=("159985.XSHE",),
        as_of_date="2026-07-13",
        rule=RULE,
    )
    assert result.results[0].status == "pass"
    assert result.passed_securities == ("159985.XSHE",)


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        (_candidate_rows(valid_days=749), "valid_days_below_750"),
        (_candidate_rows(median_money=99_999_999.0), "median_money_below_100000000"),
        (_candidate_rows(illegal_ohlc=True), "illegal_ohlc"),
    ],
)
def test_screen_rejects_each_fixed_gate(rows, reason: str) -> None:
    result = screen_candidates(
        rows=rows,
        corporate_actions=(),
        official_security_metadata=_official_metadata(),
        requested_securities=("159985.XSHE",),
        as_of_date="2026-07-13",
        rule=RULE,
    )
    assert result.results[0].reason_codes == (reason,)
```

- [ ] **Step 2: 写跨快照摘要失败测试**

```python
def test_snapshot_overlap_accepts_subset_and_rejects_action_drift(store_root: Path) -> None:
    evidence = validate_snapshot_overlap(BASELINE_ID, EXPANDED_ID, root=store_root)
    assert evidence.securities == ("510300.XSHG",)
    assert evidence.market_digest
    assert evidence.corporate_actions_digest

    _mutate_action_in_fixture(store_root, EXPANDED_ID)
    with pytest.raises(MarketDataIntegrityError, match="overlap.*corporate"):
        validate_snapshot_overlap(BASELINE_ID, EXPANDED_ID, root=store_root)
```

- [ ] **Step 3: 运行测试确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_candidate_screen.py tests\local_quant_research\test_market_data_query.py -q
```

Expected: FAIL，缺少 `candidate_screen` 和 `validate_snapshot_overlap`。

- [ ] **Step 4: 实现筛选和重叠证据**

```python
@dataclass(frozen=True)
class CandidateScreenRule:
    min_valid_days: int = 750
    money_lookback_days: int = 20
    min_median_money: float = 100_000_000.0


@dataclass(frozen=True)
class CandidateScreenResult:
    screen_id: str
    as_of_date: str
    requested_securities: tuple[str, ...]
    passed_securities: tuple[str, ...]
    results: tuple[SecurityScreenResult, ...]


def write_candidate_screen(result: CandidateScreenResult, *, root: Path) -> Path:
    target = root / "screens" / f"{result.screen_id}.json"
    return write_json_exclusive(target, candidate_screen_document(result))


def validate_snapshot_overlap(
    left_snapshot_id: str,
    right_snapshot_id: str,
    *,
    root: Path,
) -> SnapshotOverlapEvidence:
    left = open_snapshot(left_snapshot_id, root=root)
    right = open_snapshot(right_snapshot_id, root=root)
    _require_same_snapshot_semantics(left_snapshot_id, right_snapshot_id, root=root)
    overlap = tuple(sorted(_securities(left) & _securities(right)))
    left_market = normalized_digest(row for row in left.rows if row["security"] in overlap)
    right_market = normalized_digest(row for row in right.rows if row["security"] in overlap)
    left_actions = corporate_actions_digest(row for row in left.corporate_actions if row["security"] in overlap)
    right_actions = corporate_actions_digest(row for row in right.corporate_actions if row["security"] in overlap)
    if (left_market, left_actions) != (right_market, right_actions):
        raise MarketDataIntegrityError("snapshot overlap evidence differs")
    return SnapshotOverlapEvidence(overlap, left_market, left_actions)
```

`SecurityScreenResult` 除硬门槛证据外还必须输出 `instrument_risk_notes`：`513030.XSHG`、`513800.XSHG` 固定说明境内外休市错位、汇率和折溢价风险；三个商品期货 ETF 固定说明移仓、期限结构和保证金外资产收益；`511260.XSHG` 固定说明分红现金和不复权跳变。上述字段只作风险披露，不改变通过/失败判定。测试逐类断言披露齐全。

扩展 `render_export_program()` 的 `export_result`，逐证券返回 `official_start_date`、`first_market_date`、`last_market_date` 和 `market_rows`；不改变两个 CSV 的字段或新增第三个传输文件。

- [ ] **Step 5: 跑行情层回归并授权后提交**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_candidate_screen.py tests\local_quant_research\test_market_data_query.py tests\local_quant_research\test_joinquant_export.py tests\local_quant_research\test_market_data_storage.py -q
git add scripts/research/market_data/candidate_screen.py `
  scripts/research/market_data/query.py `
  scripts/research/market_data/joinquant_export.py `
  tests/local_quant_research/test_market_data_candidate_screen.py `
  tests/local_quant_research/test_market_data_query.py `
  tests/local_quant_research/test_joinquant_export.py
git commit -m "实现：增加资产候选筛选与快照重叠校验"
```

Expected: 全部 PASS；存储格式、不可变批次和 DuckDB 内存查询既有测试无回归；提交受授权门禁控制。

### Task 4: 实现冻结原订单的真实延迟执行

**Files:**
- Create: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_delayed.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_inputs.py:529-572`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py:22-228`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/result_adapter.py:308-590`
- Modify: `joinquant/strategies/strategy-003/research/code-identity.json`
- Create: `tests/local_quant_research/test_turtle_vectorbt_delayed.py`
- Modify: `tests/local_quant_research/test_turtle_vectorbt_inputs.py`
- Modify: `tests/local_quant_research/test_turtle_vectorbt_engine.py`
- Modify: `tests/local_quant_research/test_turtle_result_adapter.py`
- Modify: `tests/local_quant_research/test_turtle_vectorbt_performance.py`

**Interfaces:**
- Consumes: 即时路径生成的原始有效订单计划和 `execution.additional_delay_days`。
- Produces: `freeze_order_plan(...) -> FrozenOrderPlan`、`run_delayed_execution(...) -> VectorbtSimulationResult`；`delay=0` 保持即时路径逐事实不变。

- [ ] **Step 1: 先锁定 delay=0 现有行为**

```python
def test_zero_delay_keeps_existing_execution_facts_digest(real_baseline_fixture) -> None:
    result = run_vectorbt_simulation(
        real_baseline_fixture.inputs,
        real_baseline_fixture.config,
    )
    facts = to_joinquant_facts(simulation=result, inputs=real_baseline_fixture.inputs, scenario_id="baseline")
    assert execution_facts_digest(facts) == (
        "7e77fbdc318569828c90cdec61911c929308ddc7ae256f1ad7ac72420ac4ba1e"
    )
```

此测试使用当前零滑点配置先锁定引擎兼容；Task 5 再按已确认契约显式加入 0.05% 基准滑点并产生新研究身份。

- [ ] **Step 2: 写延迟语义失败测试**

```python
def test_delayed_execution_freezes_original_action_target_reason_and_signal_n() -> None:
    immediate = _run_fixture(delay_days=0)
    delayed = _run_fixture(delay_days=1, mutate_delay_day_signal=True)
    assert delayed.action_codes[2, 0] == immediate.action_codes[1, 0]
    assert delayed.reason_codes[2, 0] == immediate.reason_codes[1, 0]
    assert delayed.planned_quantities[2, 0] == immediate.planned_quantities[1, 0]
    assert delayed.planned_row_indices[2, 0] == 1


def test_delayed_buy_only_uses_lot_cash_truncation() -> None:
    delayed = _run_fixture(delay_days=1, delayed_open=200.0)
    assert delayed.execution_adjustment_codes[2, 0] == ADJUST_CASH_TRUNCATED
    assert delayed.filled_quantities[2, 0] % 100 == 0
    assert delayed.filled_quantities[2, 0] < delayed.planned_quantities[2, 0]


def test_delayed_queue_executes_before_today_orders_enter_next_day_queue() -> None:
    delayed = _run_collision_fixture(delay_days=1)
    assert delayed.execution_sequence[2].tolist() == ["queued-from-row-1"]
    assert delayed.planned_row_indices[3, 1] == 2
    assert delayed.execution_sequence[3].tolist() == ["queued-from-row-2"]
```

- [ ] **Step 3: 运行测试确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_delayed.py tests\local_quant_research\test_turtle_vectorbt_inputs.py -q
```

Expected: FAIL，延迟仍会移动信号/协方差且缺少冻结执行模块。

- [ ] **Step 4: 固定输入只做 T 到 T+1 错位**

删除 `vectorbt_inputs.py` 中 `additional_delay_days` 对 `signal_*` 和协方差 source row 的影响，固定：

```python
shift = 1
for execution_row in range(shift, row_count):
    source_row = execution_row - shift
    signal_source_index[execution_row] = source_row
    signal_close[execution_row] = continuous_close[source_row]
```

延迟只在交易执行层处理。

- [ ] **Step 5: 实现两段式冻结计划与真实回放**

```python
@dataclass(frozen=True)
class FrozenOrderPlan:
    planned_row_indices: np.ndarray
    action_codes: np.ndarray
    reason_codes: np.ndarray
    requested_quantities: np.ndarray
    target_quantities: np.ndarray
    signal_n: np.ndarray


def freeze_order_plan(
    inputs: SimulationInputs,
    immediate: VectorbtSimulationResult,
) -> FrozenOrderPlan:
    valid = immediate.filled_quantities > 0
    planned_rows = np.where(valid, np.arange(valid.shape[0])[:, None], -1)
    return FrozenOrderPlan(
        planned_row_indices=planned_rows.astype(np.int64),
        action_codes=np.where(valid, immediate.action_codes, ACTION_NONE).astype(np.int16),
        reason_codes=np.where(valid, immediate.reason_codes, REASON_NONE).astype(np.int16),
        requested_quantities=np.where(valid, immediate.requested_quantities, 0).astype(np.int64),
        target_quantities=np.where(valid, immediate.planned_quantities, 0).astype(np.int64),
        signal_n=np.where(valid, inputs.signal_n, np.nan).astype(np.float64),
    )
```

`run_vectorbt_simulation()` 保持签名不变：`delay_days=0` 调 `_run_immediate()`；`delay_days=1` 先生成即时计划，再由 `vectorbt_delayed.py` 使用下一交易日开盘真实回放。每个执行日先按原策略优先级执行前一日延迟队列，再把当日新生成订单冻结到下一交易日队列；同一队列内按原策略优先级和证券代码稳定排序，当日新订单不得提前成交。不可交易、现金不足、持仓不足和样本末尾分别记录独立 `execution_adjustment_code`，不覆盖冻结原因码，不做二次 A1 分配。

- [ ] **Step 6: 保持标准表结构并写入延迟证据**

`VectorbtSimulationResult` 墯加：

```python
planned_row_indices: np.ndarray
execution_adjustment_codes: np.ndarray
execution_delay_days: int
```

`result_adapter.py` 映射固定为：

```python
order["entrust_time"] = planned_date + " 09:30:00"
order["match_time"] = execution_date + " 09:30:00" if filled else None
order["finish_time"] = order["match_time"]
order["time"] = execution_date + " 09:30:00"
order["amount"] = frozen_target
order["filled"] = actual_fill
details.update(
    planned_date=planned_date,
    execution_date=execution_date,
    delay_days=execution_delay_days,
    frozen_reason=frozen_reason,
    frozen_target_amount=frozen_target,
    execution_adjustment=adjustment_name,
)
```

样本末尾无法执行的订单必须输出 attribution-only（仅归因）`horizon_expired` 证据。

- [ ] **Step 7: 跑延迟、适配和性能测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_vectorbt_delayed.py tests\local_quant_research\test_turtle_vectorbt_engine.py tests\local_quant_research\test_turtle_result_adapter.py tests\local_quant_research\test_turtle_vectorbt_performance.py -q
```

Expected: PASS；`delay=0` 摘要不变；延迟冷启动和预热均小于等于 180 秒。

- [ ] **Step 8: 更新代码身份并授权后提交**

```powershell
git add joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_delayed.py `
  joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_inputs.py `
  joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py `
  joinquant/strategies/strategy-003/research/turtle_etf/result_adapter.py `
  joinquant/strategies/strategy-003/research/code-identity.json `
  tests/local_quant_research/test_turtle_vectorbt_delayed.py `
  tests/local_quant_research/test_turtle_vectorbt_inputs.py `
  tests/local_quant_research/test_turtle_vectorbt_engine.py `
  tests/local_quant_research/test_turtle_result_adapter.py `
  tests/local_quant_research/test_turtle_vectorbt_performance.py
git commit -m "实现：增加冻结订单延迟执行压力"
```

Expected: 提交受用户明确授权门禁控制。

### Task 5: 生成资产扩展真实场景矩阵和精确快照

**Files:**
- Create: `joinquant/strategies/strategy-003/research/asset-expansion-plan.json`
- Create: `joinquant/strategies/strategy-003/research/turtle_etf/asset_expansion.py`
- Modify: `joinquant/strategies/strategy-003/research/baseline.json`
- Modify: `scripts/research/quant_analysis/schemas/analysis-plan.schema.json`
- Modify: `scripts/research/quant_analysis/analysis_plan.py:89-150`
- Modify: `scripts/research/quant_analysis/orchestration.py:64-190`
- Create: `tests/local_quant_research/test_turtle_asset_expansion.py`
- Modify: `tests/quant_analysis/test_analysis_plan.py`
- Create: `tests/quant_analysis/test_orchestration.py`

**Interfaces:**
- Consumes: 冻结候选筛选结果、原基线配置、相同批次集合和快照选择模板。
- Produces: `build_asset_expansion_scenarios(...)`、`bind_exact_snapshots(...)`、`materialize_asset_expansion_plan(...)` 和 `strategy-analysis-plan/2` 每场景完整身份。

- [ ] **Step 1: 写 16 场景失败测试**

```python
def test_builds_sixteen_scenarios_when_all_candidates_pass() -> None:
    scenarios = build_asset_expansion_scenarios(
        _baseline_config(),
        _all_six_passed_candidates(),
    )
    assert len(scenarios) == 16
    assert [item.role for item in scenarios].count("baseline") == 1
    assert [item.role for item in scenarios].count("expanded") == 1
    assert [item.role for item in scenarios].count("security_deletion") == 6
    assert [item.role for item in scenarios].count("slice_deletion") == 3
    assert [item.role for item in scenarios].count("cost_execution") == 5


def test_cost_matrix_matches_five_confirmed_variants() -> None:
    costs = _cost_scenarios(build_asset_expansion_scenarios(_baseline_config(), _all_six_passed_candidates()))
    assert [(x.commission, x.slippage, x.delay_days) for x in costs] == [
        (2.0, 0.0005, 0),
        (1.0, 0.0010, 0),
        (2.0, 0.0010, 0),
        (1.0, 0.0005, 1),
        (2.0, 0.0010, 1),
    ]
```

- [ ] **Step 2: 写精确快照和不同资产池计划失败测试**

```python
def test_every_scenario_binds_an_exact_snapshot(tmp_path: Path) -> None:
    scenarios = build_asset_expansion_scenarios(_baseline_config(), _all_six_passed_candidates())
    mapping = bind_exact_snapshots(
        scenarios,
        batch_ids=(OLD_BATCH_ID, NEW_BATCH_ID),
        selection_template=_selection(),
        market_data_root=tmp_path,
    )
    for scenario in scenarios:
        view = open_snapshot(mapping[scenario.scenario_id], root=tmp_path)
        assert {row["security"] for row in view.rows} == set(scenario.universe)
```

- [ ] **Step 3: 运行测试确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_asset_expansion.py tests\quant_analysis\test_analysis_plan.py -q
```

Expected: FAIL，缺少资产扩展计划器，Schema 仍要求所有场景使用同一基线资产池。

- [ ] **Step 4: 显式修正主场景成本口径**

在 `baseline.json` 增加：

```json
"costs": {
  "commission_multiplier": 1.0,
  "one_way_slippage": 0.0005
}
```

添加测试断言主场景必须显式包含两项，禁止依赖默认值。

此修改会形成新的主场景研究身份。后续所有验收比较必须使用重新运行的 `baseline-11`；旧报告中的 5.87% 年化收益只保留为问题诊断历史，不得用作扩展方案对照基准。

- [ ] **Step 5: 实现场景构建器**

```python
@dataclass(frozen=True)
class ExpansionScenario:
    scenario_id: str
    dimension: str
    role: str
    parent_scenario_id: str | None
    target: str | None
    universe: Mapping[str, str]
    config: Mapping[str, object]


def build_asset_expansion_scenarios(
    baseline_config: Mapping[str, object],
    passed_candidates: Sequence[CandidateSpec],
) -> tuple[ExpansionScenario, ...]:
    accepted = tuple(sorted(passed_candidates, key=lambda item: item.security))
    baseline = _scenario("baseline-11", "baseline", baseline_config)
    expanded = _expanded_scenario(baseline_config, accepted)
    security_deletions = tuple(_delete_security(expanded, item.security) for item in accepted)
    slice_deletions = tuple(
        _delete_slice(expanded, slice_name)
        for slice_name in ("developed_non_us_equity", "commodity_futures", "treasury_duration")
        if _slice_has_accepted_candidate(accepted, slice_name)
    )
    costs = _five_cost_execution_scenarios(expanded)
    return (baseline, expanded, *security_deletions, *slice_deletions, *costs)
```

`asset-expansion-plan.json` 固定六只候选、三个扩展切片、筛选阈值、五个成本执行场景和设计文档全部结论门槛；不保存回测结果或通过者。

项目定义同时固定压力映射，不允许未知资产组默认为零：`developed_non_us_equity` 在每个既有冲击中复制 `cross_border_tech_equity` 的冲击值，`commodity_futures` 复制 `gold` 的冲击值，`511260.XSHG` 继续属于已有 `treasury_bond` 并使用其冲击值。若来源键或新资产组键缺失，计划校验失败。新增测试逐个压力场景断言所有实际持仓组都有显式键。

- [ ] **Step 6: 扩展分析计划为每场景完整身份**

项目专属构建器先在 `.local/asset-expansion-plans/{matrix_id}/` 写入场景配置和带真实快照的具体计划。具体 `strategy-analysis-plan/2` 场景项固定由以下代码生成：

```python
{
    "scenario_id": "expanded-universe",
    "dimension": "primary_comparison",
    "role": "expanded",
    "parent_scenario_id": "baseline-11",
    "target": None,
    "project_config": (
        f".local/asset-expansion-plans/{matrix_id}/"
        "scenario-configs/expanded-universe/params.json"
    ),
    "snapshot_id": expanded_snapshot_id,
}
```

`matrix_id`、配置摘要和快照身份都由实际内容生成，不接受调用者自定义随机名称。`strategy-analysis-plan/1` 保持既有参数挑战行为。

对场景配置做规范化深比较：仅允许 `scenario_id`、`universe`、新增固定资产组、对应 `snapshot_id` 以及五个压力场景声明的成本/延迟字段不同；初始资金、55/20 日信号、0.5N 加仓、2N 止损、A1、单标的/资产组/组合上限、10% 目标波动率、无杠杆和执行时点必须逐字段相等。测试分别篡改每类不变量并断言计划拒绝。

- [ ] **Step 7: 内容寻址创建并绑定精确快照**

```python
def bind_exact_snapshots(
    scenarios: Sequence[ExpansionScenario],
    *,
    batch_ids: Sequence[str],
    selection_template: SnapshotSelection,
    market_data_root: Path,
) -> Mapping[str, str]:
    by_universe: dict[tuple[str, ...], str] = {}
    result: dict[str, str] = {}
    for scenario in scenarios:
        securities = tuple(sorted(scenario.universe))
        if securities not in by_universe:
            snapshot = create_snapshot(
                batch_ids=batch_ids,
                selection=replace(selection_template, securities=securities),
                root=market_data_root,
            )
            by_universe[securities] = snapshot.snapshot_id
        result[scenario.scenario_id] = by_universe[securities]
    return result
```

五个成本执行场景复用扩展资产池的同一个快照 ID；不人为制造重复快照。

再实现：

```python
def materialize_asset_expansion_plan(
    *,
    baseline_config_path: Path,
    definition_path: Path,
    screen_report_path: Path,
    batch_ids: Sequence[str],
    selection_template: SnapshotSelection,
    market_data_root: Path,
    output_root: Path,
) -> Path:
    scenarios = build_asset_expansion_scenarios(
        _load_baseline(baseline_config_path),
        _load_passed_candidates(screen_report_path, definition_path),
    )
    snapshot_ids = bind_exact_snapshots(
        scenarios,
        batch_ids=batch_ids,
        selection_template=selection_template,
        market_data_root=market_data_root,
    )
    return _write_content_addressed_v2_plan(
        scenarios, snapshot_ids=snapshot_ids, output_root=output_root
    )
```

`asset_expansion.py` 同时提供 `materialize` 命令行入口，参数固定为 `--repo-root`、`--definition`、`--baseline`、`--screen-report`、可重复的 `--batch-id`、`--start-date`、`--end-date`、`--market-data-root` 和 `--output-root`；标准输出只返回包含 `matrix_id`、`plan_path`、`scenario_count` 和 `snapshot_ids` 的 JSON。测试必须从该公开入口真实生成一个最小计划，不能只调用内部函数。

- [ ] **Step 8: 验证矩阵和授权后提交**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_asset_expansion.py tests\quant_analysis\test_analysis_plan.py tests\quant_analysis\test_orchestration.py -q
git add joinquant/strategies/strategy-003/research/asset-expansion-plan.json `
  joinquant/strategies/strategy-003/research/baseline.json `
  joinquant/strategies/strategy-003/research/turtle_etf/asset_expansion.py `
  scripts/research/quant_analysis/schemas/analysis-plan.schema.json `
  scripts/research/quant_analysis/analysis_plan.py `
  scripts/research/quant_analysis/orchestration.py `
  tests/local_quant_research/test_turtle_asset_expansion.py `
  tests/quant_analysis/test_analysis_plan.py `
  tests/quant_analysis/test_orchestration.py
git commit -m "实现：生成独立资产扩展真实场景矩阵"
```

Expected: 全部 PASS；提交受用户明确授权门禁控制。

### Task 6: 让统一分析按每场景资产池消费真实结果

**Files:**
- Create: `scripts/research/quant_analysis/scenario_matrix.py`
- Modify: `scripts/research/quant_analysis/unified_analysis.py:70-82,307-530,598-680,914-1050,1235-1415`
- Create: `tests/quant_analysis/test_asset_expansion_analysis.py`
- Modify: `tests/quant_analysis/test_unified_analysis.py`

**Interfaces:**
- Consumes: `strategy-analysis-plan/2`、显式来源登记 JSON 中的全部 `scenario_id -> run_id` 和逐场景精确快照证据。
- Produces: `evaluate_real_scenario_matrix(...)`、每场景绩效/归因、真实删除层和真实成本层门槛结果。

- [ ] **Step 1: 写不同资产池与真实矩阵失败测试**

```python
def test_analysis_accepts_distinct_universes_with_compatible_snapshots(tmp_path: Path) -> None:
    result = run_deterministic_analysis(
        tmp_path,
        _v2_preparation(tmp_path),
        _all_real_source_runs(),
    )
    assert result["primary_comparison"]["baseline_scenario_id"] == "baseline-11"
    assert result["primary_comparison"]["expanded_scenario_id"] == "expanded-universe"


def test_v2_uses_real_deletion_and_cost_runs_not_approximations(monkeypatch) -> None:
    monkeypatch.setattr(unified_analysis, "_deletion_sensitivity", _raise_if_called)
    monkeypatch.setattr(unified_analysis, "_cost_sensitivity", _raise_if_called)
    result = run_deterministic_analysis(ROOT, PREPARATION, SOURCES)
    assert result["robustness"]["asset_deletions"][0]["method"] == "real_backtest"
    assert result["robustness"]["cost_execution"][0]["method"] == "real_backtest"


def test_cli_requires_registry_to_cover_every_planned_scenario(tmp_path: Path) -> None:
    registry = _write_source_registry(tmp_path, omit="cost-delay-double")
    with pytest.raises(AnalysisInputError, match="missing scenario source"):
        load_source_registry(registry, preparation=_v2_preparation(tmp_path))
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_asset_expansion_analysis.py tests\quant_analysis\test_unified_analysis.py -q
```

Expected: FAIL；当前分析强制共享快照和全局资产池，并调用近似删除/成本方法。

- [ ] **Step 3: 给场景输入绑定自己的身份**

```python
@dataclass(frozen=True)
class ScenarioInput:
    scenario_id: str
    role: str
    parent_scenario_id: str | None
    target: str | None
    universe: Mapping[str, str]
    snapshot_id: str
    run_id: str
    result_dir: Path
    manifest: Mapping[str, object]
    performance: Mapping[str, object]
    returns: pd.Series
    balances: pd.DataFrame
    positions: pd.DataFrame
    orders: pd.DataFrame
    attribution: pd.DataFrame
```

`_position_facts()` 和 `_security_pnl_facts()` 改为只用 `scenario.universe`；未知证券继续硬失败。`_register_source_results()` 要求代码摘要、执行后端、批次集合、截止日、字段和价格口径相同，但允许 `snapshot_id` 不同，并调用 `validate_snapshot_overlap()` 校验重叠证券。

`unified_analysis` 命令行增加 `--source-registry PATH`；文件固定包含 `preparation_id`、`plan_digest` 和 `sources` 映射。v2 禁止与零散 `--source` 混用，缺少、多出或重复任一计划场景都硬失败；v1 的既有 `--source` 行为不变。

- [ ] **Step 4: 用真实场景评估层级门槛**

```python
def evaluate_real_scenario_matrix(
    metrics_by_scenario: Mapping[str, Mapping[str, object]],
    scenario_specs: Sequence[Mapping[str, object]],
    thresholds: Mapping[str, object],
) -> dict[str, object]:
    deletion_security = _layer("security_deletion", metrics_by_scenario, scenario_specs)
    deletion_slice = _layer("slice_deletion", metrics_by_scenario, scenario_specs)
    cost_execution = _layer("cost_execution", metrics_by_scenario, scenario_specs)
    return {
        "security_deletion": _evaluate_layer(
            deletion_security, cagr_min=0.0, drawdown_max=0.20,
            worst_calmar_ratio=0.50, average_calmar_ratio=0.75,
        ),
        "slice_deletion": _evaluate_layer(
            deletion_slice, cagr_min=0.0, drawdown_max=0.20,
            worst_calmar_ratio=0.50, average_calmar_ratio=0.75,
        ),
        "cost_execution": _evaluate_layer(
            cost_execution, cagr_min=0.0, drawdown_max=0.20,
            worst_calmar_ratio=0.50, average_calmar_ratio=0.75,
        ),
    }
```

三层 Calmar 比例都相对 `expanded-universe`。三个切片删除变体中至少两个还必须同时满足“CAGR 高于 `baseline-11`、Calmar 不低于 `baseline-11`”。

- [ ] **Step 5: 保留 v1，v2 禁止近似验收**

`strategy-analysis-plan/1` 保持现有参数研究报告行为；`strategy-analysis-plan/2` 只从真实场景构建 `asset_deletions` 和 `cost_execution`，近似 `_deletion_sensitivity()`、`_cost_sensitivity()` 不进入 v2 证据矩阵或推荐。

- [ ] **Step 6: 跑统一分析回归并授权后提交**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_asset_expansion_analysis.py tests\quant_analysis\test_unified_analysis.py tests\quant_analysis\test_statistics.py -q
git add scripts/research/quant_analysis/scenario_matrix.py `
  scripts/research/quant_analysis/unified_analysis.py `
  tests/quant_analysis/test_asset_expansion_analysis.py `
  tests/quant_analysis/test_unified_analysis.py
git commit -m "实现：统一分析消费真实跨资产池场景"
```

Expected: v1、v2 测试全部 PASS；提交受授权门禁控制。

### Task 7: 增加有效秩和趋势利用分析

**Files:**
- Create: `scripts/research/quant_analysis/independence.py`
- Create: `scripts/research/quant_analysis/trend_utilization.py`
- Modify: `scripts/research/quant_analysis/unified_analysis.py`
- Create: `tests/quant_analysis/test_independence.py`
- Create: `tests/quant_analysis/test_trend_utilization.py`

**Interfaces:**
- Consumes: 共享 `snapshot_return_panel()`、基线/扩展标准结果包、计划定义的原因码集合。
- Produces: `rolling_effective_rank(...)` 和 `analyze_trend_utilization(...)`，不导入海龟回调模块。

- [ ] **Step 1: 写有效秩失败测试**

```python
def test_effective_rank_uses_last_sixty_common_observations() -> None:
    returns = _return_panel(rows=61, include_missing_first_row=True)
    result = rolling_effective_rank(returns, window_days=60, variance_epsilon=1e-12)
    assert len(result) == 1
    assert result.iloc[0]["security_count"] == 3


def test_effective_rank_excludes_zero_variance_and_clips_eigenvalues(monkeypatch) -> None:
    # 夹具含四只证券，其中一只为常数列；剔除后相关矩阵恰有三个特征值。
    returns = _panel_with_constant_security()
    monkeypatch.setattr(np.linalg, "eigvalsh", lambda _: np.array([-1e-15, 0.5, 1.5]))
    result = rolling_effective_rank(returns)
    assert result.iloc[-1]["excluded_zero_variance"] == 1
    assert result.iloc[-1]["effective_rank"] == pytest.approx(1.6)


@pytest.mark.parametrize(
    "fixture_name",
    ("paused_day", "missing_listing_row", "fewer_than_60_common", "duplicate_action", "unreconciled_action"),
)
def test_effective_rank_obeys_shared_economic_return_evidence(fixture_name: str) -> None:
    _assert_expected_effective_rank_behavior(fixture_name)
```

- [ ] **Step 2: 写趋势利用失败测试**

```python
def test_low_exposure_opportunities_count_signal_fill_and_budget_block() -> None:
    result = analyze_trend_utilization(
        baseline=_baseline_scenario(invested_ratio=0.40),
        expanded=_expanded_scenario_with_new_events(),
        new_securities=("159985.XSHE",),
        entry_reason_codes=("entry_breakout",),
        budget_block_reason_codes=("allocation_constraint",),
    )
    assert result.low_exposure_days == 1
    assert result.new_breakout_events == 2
    assert result.new_filled_entries == 1
    assert result.new_budget_blocks == 1
```

- [ ] **Step 3: 运行测试确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_independence.py tests\quant_analysis\test_trend_utilization.py -q
```

Expected: FAIL，两个通用分析模块不存在。

- [ ] **Step 4: 实现固定有效秩算法**

```python
def rolling_effective_rank(
    returns: pd.DataFrame,
    *,
    window_days: int = 60,
    variance_epsilon: float = 1e-12,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for date in returns.index:
        common = returns.loc[:date].dropna(how="any").tail(window_days)
        if len(common) < window_days:
            continue
        std = common.std(ddof=1)
        active = std > variance_epsilon
        selected = common.loc[:, active]
        if selected.shape[1] < 2:
            continue
        eigenvalues = np.clip(np.linalg.eigvalsh(selected.corr().to_numpy()), 0.0, None)
        effective = float(eigenvalues.sum() ** 2 / np.square(eigenvalues).sum())
        rows.append({
            "date": pd.Timestamp(date).normalize(),
            "effective_rank": effective,
            "normalized_effective_rank": effective / selected.shape[1],
            "security_count": int(selected.shape[1]),
            "excluded_zero_variance": int((~active).sum()),
        })
    return pd.DataFrame.from_records(rows)
```

- [ ] **Step 5: 实现趋势供给、成交和贡献分析**

分析器通过计划传入原因码，不硬编码海龟常量；固定输出：低于半仓日期、新资产突破/建仓/风险阻止次数、信号成交率、平均持仓、平均风险预算、盈利趋势占比、证券/切片/时期贡献，以及至少两个切片跨至少两个固定时期出现真实成交趋势的布尔证据。

“可执行突破”只统计结果包中 `entry_breakout` 决策已通过非价格门禁且下一执行日可交易的事件；“真实成交趋势”必须存在已成交建仓订单，并在随后至少一个交易日持仓大于零。比较有效秩时先从扩展资产池找到“最后一只通过候选累计满 60 个有效交易日”的首日，再把基线和扩展结果严格对齐到从该日到 `2026-07-13` 的同一日期集合；任何一侧缺日都失败，不做填充。

- [ ] **Step 6: 接入统一分析并验证门槛**

在 `deterministic-analysis.json` 增加：

```json
"independence": {
  "common_window": {"start": "YYYY-MM-DD", "end": "2026-07-13"},
  "baseline": {},
  "expanded": {},
  "delta": {},
  "gate": {"status": "pass|fail", "reasons": []}
},
"trend_utilization": {
  "low_exposure": {},
  "new_assets": [],
  "new_slices": [],
  "gate": {"status": "pass|fail", "reasons": []}
}
```

门槛严格采用设计文档：原始有效秩中位数增量至少 0.5、归一化中位数不下降、原始有效秩提高日占比超过 60%。

趋势利用门槛也必须逐项输出：基线低于半仓日存在新增资产可执行突破并获得仓位；平均投入仓位或风险预算利用率至少一项严格提高；所有既有硬上限违规次数为零；新增资产合计净贡献为正；至少两个新增资产组净贡献为正；至少两个不同扩展切片各自在至少两个固定时期出现真实成交趋势。任何一项缺证据都不能默认为通过。

- [ ] **Step 7: 跑分析测试并授权后提交**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_independence.py tests\quant_analysis\test_trend_utilization.py tests\quant_analysis\test_asset_expansion_analysis.py -q
git add scripts/research/quant_analysis/independence.py `
  scripts/research/quant_analysis/trend_utilization.py `
  scripts/research/quant_analysis/unified_analysis.py `
  tests/quant_analysis/test_independence.py `
  tests/quant_analysis/test_trend_utilization.py
git commit -m "实现：增加独立风险来源与趋势利用分析"
```

Expected: 全部 PASS；提交受授权门禁控制。

### Task 8: 扩展完整报告和推荐结论

**Files:**
- Modify: `scripts/research/quant_analysis/reporting.py:135-760`
- Modify: `tests/quant_analysis/test_reporting.py`

**Interfaces:**
- Consumes: v2 确定性分析中的主场景对照、独立性、趋势利用、真实删除/成本、双基准、归因和压力证据。
- Produces: 完整 `local-strategy-analysis-report.md` 和只等待人工确认的 `recommendation.json`。

- [ ] **Step 1: 写报告失败测试**

```python
def test_asset_expansion_report_contains_every_required_conclusion() -> None:
    report = render_analysis_report(_asset_expansion_analysis(), _recommendation(), _vibe())
    for heading in (
        "原 11 只与扩展资产池对照",
        "独立风险来源与有效秩",
        "趋势供给与利用",
        "逐只与逐切片真实删除",
        "五个真实成本与执行压力",
        "双基准与 Alpha/Beta",
        "反对证据与不确定性",
    ):
        assert heading in report
    assert "JoinQuant（聚宽）正式回测" in report
    assert "人工确认" in report


def test_single_asset_or_period_concentration_fails_recommendation() -> None:
    recommendation = build_recommendation(_analysis(single_asset_share=0.71))
    assert recommendation["decision"] == "do_not_expand"
    assert "single_asset_concentration" in recommendation["reasons"]
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_reporting.py -q
```

Expected: FAIL，报告缺少资产扩展专用章节和结论门槛。

- [ ] **Step 3: 实现明确的三类结论**

```python
def _asset_expansion_decision(analysis: Mapping[str, object]) -> tuple[str, list[str]]:
    failed = _failed_required_gates(analysis)
    if failed:
        return "do_not_expand", failed
    if _independence_passed(analysis) and not _return_risk_passed(analysis):
        return "independence_improved_allocation_insufficient", []
    return "recommend_asset_expansion_for_human_confirmation", []
```

`_failed_required_gates()` 必须覆盖独立性、趋势利用、主场景 CAGR/回撤/Calmar、五个历史窗口、全部持仓冲击、五个真实成本执行场景、逐只删除、逐切片删除、单资产 70% 和单时期 70% 门槛。报告完整展示所有失败门槛，不允许用高收益覆盖回撤、稳健性、独立性或单点依赖失败。Vibe 单体只放在审计章节，不改变上述函数结果。

分析状态只允许 `complete`、`evidence_insufficient`、`failed`。只有数据、全部实际场景、守恒校验和报告均完整时才为 `complete`；验收门槛不通过仍是 `complete` 并给出明确不推荐结论。证据缺失与运行失败分别按设计文档停止，不用猜测补齐。

- [ ] **Step 4: 跑报告回归并授权后提交**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_reporting.py tests\quant_analysis\test_unified_analysis.py -q
git add scripts/research/quant_analysis/reporting.py tests/quant_analysis/test_reporting.py
git commit -m "实现：输出资产扩展完整研究报告"
```

Expected: PASS；提交受授权门禁控制。

### Task 9: 从聚宽真实导出六只候选并完成前置筛选

**Files:**
- Runtime only: `.local/market-data/transfers/`
- Runtime only: `.local/market-data/batches/$NewBatchId/`
- Runtime only: `.local/market-data/snapshots/$CandidateScreenSnapshotId.json`
- Runtime only: `.local/market-data/screens/$ScreenId.json`
- No repository source edit.

**Interfaces:**
- Consumes: `ExportRequest`、已登录聚宽研究环境、现有 `import_verified_transfer()`。
- Produces: 一个只含六只候选的新不可变批次和冻结筛选报告；不复制原 11 只批次。

- [ ] **Step 1: 审计现有行情中心**

Run:

```powershell
.\.venv\Scripts\python.exe -m scripts.research.market_data.cli audit --root .local\market-data
```

Expected: `status=complete`；当前原批次仍为 `1923c902f5692d35bd84e2745620a06cb6c18666c4a4add724ce80d261d5f4e1`，传输目录为空。若身份已变化，记录实际审计结果并停止使用旧常量。

- [ ] **Step 2: 生成固定聚宽研究程序**

使用 `.\.venv\Scripts\python.exe` 调用：

```python
from scripts.research.market_data.contracts import MARKET_DATA_FIELDS
from scripts.research.market_data.joinquant_export import ExportRequest, render_export_program

request = ExportRequest(
    securities=(
        "159980.XSHE", "159981.XSHE", "159985.XSHE",
        "511260.XSHG", "513030.XSHG", "513800.XSHG",
    ),
    fields=MARKET_DATA_FIELDS,
    snapshot_end_date="2026-07-13",
)
print(render_export_program(request))
```

保存实际程序字节 SHA256；输出程序必须声明 `fq=None`、`skip_paused=False`，并生成两个 CSV 和逐证券覆盖元数据。

- [ ] **Step 3: 使用已登录浏览器真实执行和下载**

执行时先读取并使用 `chrome:control-chrome` Skill（浏览器控制技能）：

1. 打开聚宽研究页并确认登录身份。
2. 粘贴完全相同的生成程序并运行。
3. 保存 `export_result` JSON，核对两个远端文件路径、字节数、行数、SHA256 和六只证券覆盖。
4. 下载 `market-data.csv`、`corporate-actions.csv` 到 `.local/market-data/transfers/$TransferId/`。
5. 不在此步修改任何正式策略或启动正式回测。

Expected: 六只证券都在导出结果中；文件摘要与远端回读一致。

- [ ] **Step 4: 使用现有受验证导入器发布候选批次**

通过 `apply_patch` 在 `.local/market-data/transfers/$TransferId/` 临时创建 `import_candidate_batch.py`。脚本调用 `import_verified_transfer()`；其 `cleanup_remote` 回调先原子写入 `awaiting-remote-cleanup.json`，然后最多等待 120 秒，只有读到包含两个远端路径及浏览器核验时间的 `remote-cleanup-confirmed.json` 才返回 `True`。manifest 固定为 JoinQuant 研究环境、ETF、日线、完整字段、不复权和截止日。

Run:

```powershell
$ImportProcess = Start-Process -FilePath ".\.venv\Scripts\python.exe" `
  -ArgumentList @(".local\market-data\transfers\$TransferId\import_candidate_batch.py") `
  -PassThru -WindowStyle Hidden
```

等待 `awaiting-remote-cleanup.json` 出现后，由同一个已登录浏览器删除两个远端文件并重新列目录确认均不存在；再用 `apply_patch` 写入 `remote-cleanup-confirmed.json`。随后等待 `$ImportProcess` 正常退出并读取其结果 JSON。成功后删除临时脚本和两个握手文件；若超时、浏览器未确认或子进程非零退出，保持失败并保留 CSV 供诊断，不得绕过回调直接调用 `import_batch()`。

Expected: 新 `batch_id` 为 64 位小写十六进制；Parquet 和 DuckDB 回读一致；远端删除确认后本地两个 CSV 自动删除。

- [ ] **Step 5: 建候选筛选快照并冻结结果**

候选筛选快照只引用新候选批次并包含六只代码；逐只运行 `screen_candidates()`，再用 `write_candidate_screen()` 输出 `.local/market-data/screens/$ScreenId.json`。

Expected:

```text
requested_securities = 固定六只
passed_securities = 只由前置证据决定
每只均有 valid_days、20日成交额中位数、OHLC、覆盖和公司行动结论
没有收益、回撤、Calmar 或回测字段
```

- [ ] **Step 6: 复核清理和审计**

Run:

```powershell
.\.venv\Scripts\python.exe -m scripts.research.market_data.cli audit --root .local\market-data
Get-ChildItem .local\market-data\transfers -File -Recurse
Get-ChildItem .local -Filter *.csv -File -Recurse
```

Expected: 审计 complete；两次文件扫描均无输出。若清理失败，停止状态为 `failed` 并报告准确残留路径。

### Task 10: 准备并逐次运行全部真实场景

**Files:**
- Runtime only: `.local/asset-expansion-plans/$MatrixId/`
- Runtime only: `.local/strategy-analysis-preparations/$PreparationId/`
- Runtime only: `.local/quant-research/strategy-003/$RunId/`
- Runtime only: `.local/strategy-analysis-preparations/$PreparationId/source-registry.json`
- No repository source edit.

**Interfaces:**
- Consumes: 冻结筛选报告、原/新批次、双基准集、场景构建器和项目运行模板。
- Produces: 最多 16 个独立标准结果包和完整、内容可核验的 `scenario_id -> run_id` 来源登记。

- [ ] **Step 1: 冻结场景和精确快照**

先用项目专属命令将已冻结筛选结果物化为具体 v2 计划：

```powershell
$Matrix = & .\.venv\Scripts\python.exe `
  joinquant\strategies\strategy-003\research\turtle_etf\asset_expansion.py materialize `
  --repo-root . `
  --definition joinquant\strategies\strategy-003\research\asset-expansion-plan.json `
  --baseline joinquant\strategies\strategy-003\research\baseline.json `
  --screen-report $env:SCREEN_REPORT `
  --batch-id $env:BASELINE_BATCH_ID `
  --batch-id $env:CANDIDATE_BATCH_ID `
  --start-date 2012-05-28 `
  --end-date 2026-07-13 `
  --market-data-root .local\market-data `
  --output-root .local\asset-expansion-plans | ConvertFrom-Json
$env:CONCRETE_PLAN = $Matrix.plan_path
```

该入口内部调用 `build_asset_expansion_scenarios()` 和 `bind_exact_snapshots()`。所有快照固定：

```text
batch_ids = [原11只批次, 新候选批次]
start_date = 2012-05-28
end_date = 2026-07-13
fields = MARKET_DATA_FIELDS
price_semantics = {fq: null, skip_paused: false}
```

对所有不同证券集合调用 `validate_snapshot_overlap()`；五个成本执行场景复用扩展快照。

Expected: `$Matrix.scenario_count` 等于实际矩阵数量，全部通过时为 16；`$env:CONCRETE_PLAN` 指向内容寻址的 `strategy-analysis-plan/2`，而不是仓库中的定义模板。

- [ ] **Step 2: 生成不可变 preparation**

Run:

```powershell
$Preparation = & .\.venv\Scripts\python.exe -m scripts.research.quant_analysis.orchestration `
  --repo-root . `
  --plan $env:CONCRETE_PLAN `
  --run-template joinquant\strategies\strategy-003\research\project-run.json `
  --benchmark-set-id $env:BENCHMARK_SET_ID | ConvertFrom-Json
$env:PREPARATION_WORKSPACE = $Preparation.workspace
```

Expected: `expected_scenario_runs` 等于实际矩阵数量，全部通过候选时为 16；每个 run.json 只有一个场景、一个精确 snapshot_id 和一个 required output。

- [ ] **Step 3: 主 Agent 逐次调用单场景 Skill**

对 preparation 的 `scenario_run_configs` 按列表顺序执行：

1. 主 Agent 每次只把一份 `run.json` 交给 `run-local-quant-research` Skill。
2. 等待该次返回 `complete` 和 `next_action=return_to_caller` 后再进入下一份。
3. 记录 `scenario_id -> run_id`，不扫描历史目录猜测来源。
4. 每次核对 `cold_seconds <= 180`、`warm_seconds <= 180`、规范化结果摘要一致和标准结果包门禁通过。
5. 每次成功后原子更新 `$env:PREPARATION_WORKSPACE\source-registry.json`，固定写入 `schema_version`、`preparation_id`、`plan_digest` 和完整 `sources` 映射；只有来源数与计划场景数完全一致时才封存。

Expected: 每次 Skill 只生成一份本地回测；任何一次失败都停止矩阵，不跳过、不用旧结果代替。

- [ ] **Step 4: 验证最多 16 个来源身份**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_single_scenario.py tests\local_quant_research\test_turtle_vectorbt_performance.py -q
```

Expected: PASS；来源登记数量与 preparation 完全一致；场景间共享代码身份和执行后端，精确快照重叠证据一致。

### Task 11: 生成确定性分析、Vibe 单体复核和完整报告

**Files:**
- Runtime only: `.local/strategy-analysis/$AnalysisId/deterministic-analysis.json`
- Runtime only: `.local/strategy-analysis/$AnalysisId/evidence-matrix.parquet`
- Runtime only: `.local/strategy-analysis/$AnalysisId/vibe-evidence.json`
- Runtime only: `.local/strategy-analysis/$AnalysisId/local-strategy-analysis-report.md`
- Runtime only: `.local/strategy-analysis/$AnalysisId/recommendation.json`

**Interfaces:**
- Consumes: 全部显式真实结果、双基准和共享行情快照。
- Produces: 完整本地研究报告、反对证据和人工可确认推荐；Vibe 只作定性审计。

- [ ] **Step 1: 显式登记全部来源并运行确定性分析**

Run:

```powershell
$env:SOURCE_REGISTRY = Join-Path $env:PREPARATION_WORKSPACE "source-registry.json"
$Analysis = & .\.venv\Scripts\python.exe -m scripts.research.quant_analysis.unified_analysis `
  --repo-root . `
  --preparation-workspace $env:PREPARATION_WORKSPACE `
  --source-registry $env:SOURCE_REGISTRY | ConvertFrom-Json
$env:ANALYSIS_WORKSPACE = $Analysis.workspace
```

分析入口必须校验登记文件恰好覆盖 preparation 的所有场景；不得手工省略删除/成本来源，也不得扫描历史目录补齐。

Expected: 输出 `analysis_id`、`status=complete`；证据矩阵包含全部真实场景、时期、滚动、区块抽样、压力、冲击、CVaR（条件风险价值）、有效秩和趋势利用结果。

- [ ] **Step 2: 真实运行 Vibe 单体只读复核**

通过 `apply_patch` 在分析目录建立临时提示文件，内容固定要求：

```text
只读复核指定 deterministic-analysis.json。
只使用 performance-attribution、risk-analysis、report-generate 三项单体方法。
确定性文件的数值、状态、原因和门槛是唯一裁判。
禁止回测、获取新行情、优化、群体分析和修改文件。
返回 status、capabilities_loaded、key_findings、objections、limitations、recommendation_alignment、constraints。
```

Run:

```powershell
.\.venv\Scripts\vibe-trading.exe run --json --no-rich --max-iter 12 --prompt-file $env:VIBE_PROMPT_FILE
```

Expected: 公共单体运行完成，`constraints` 明确 `read_only=true`、`backtest_called=false`、`new_market_data_called=false`、`optimizer_called=false`、`swarm_called=false`、`deterministic_metrics_are_authoritative=true`。将运行身份和 trace 摘要写入 `vibe-evidence.json`；删除临时提示文件。禁止调用任何 swarm 命令。

- [ ] **Step 3: 生成报告和推荐**

Run:

```powershell
.\.venv\Scripts\python.exe -m scripts.research.quant_analysis.reporting --workspace $env:ANALYSIS_WORKSPACE
```

Expected: 报告完整给出收益、最大回撤、Calmar、Sharpe、仓位、风险预算、有效秩、趋势供给/利用、Alpha/Beta、归因、全部真实稳健性、压力、反对证据、限制和推荐；`next_action=human_confirmation_required`。

- [ ] **Step 4: 核对推荐门槛**

只允许三个结论：

```text
recommend_asset_expansion_for_human_confirmation
independence_improved_allocation_insufficient
do_not_expand
```

无论结论为何，都必须完整报告失败证据；不得自动修改正式资产池、进入聚宽正式复核或启动新的资金分配优化。

### Task 12: 完成端到端回归、全量验证和临时产物清理

**Files:**
- Modify only when affected mapping requires: `.build-and-verify/config.json`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/tasks.md`
- Runtime evidence: `docs/superpowers/reports/2026-07-16-turtle-independent-asset-expansion-verify.md`

**Interfaces:**
- Consumes: Task 1—11 的实现、真实行情、最多 16 个结果包和完整分析交付。
- Produces: 可复核的全量验证报告；停止在人工确认，不提交或归档除非用户另行授权。

- [ ] **Step 1: 运行定向回归**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_economic_returns.py tests\local_quant_research\test_market_data_candidate_screen.py tests\local_quant_research\test_turtle_vectorbt_delayed.py tests\local_quant_research\test_turtle_asset_expansion.py tests\quant_analysis\test_asset_expansion_analysis.py tests\quant_analysis\test_independence.py tests\quant_analysis\test_trend_utilization.py tests\quant_analysis\test_reporting.py -q
```

Expected: 全部 PASS。

- [ ] **Step 2: 运行本地研究和分析完整回归**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research tests\quant_analysis -q
```

Expected: 全部 PASS；无跳过关键端到端测试。

- [ ] **Step 3: 运行公开入口端到端**

端到端必须从 `run-local-quant-research` Skill 用户入口贯通一个普通场景和一个真实延迟场景，再由主 Agent 使用冻结 preparation 复数调用全部实际场景，最后贯通统一分析、Vibe 单体和报告。

Expected:

```text
每次 Skill 一场景
每次 <= 180 秒
标准结果包完整
显式来源数量等于计划数量
完整分析和报告生成
next_action = human_confirmation_required
```

- [ ] **Step 4: 运行全仓与 Build and Verify 完整门禁**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
openspec validate build-turtle-etf-local-research-workflow --strict
.\.venv\Scripts\python.exe .build-and-verify\runtime\build_and_verify.py verify --project . --full
git diff --check
```

Expected: 全仓 PASS；OpenSpec 严格通过；Build and Verify 输出 `status=passed` 且 `full-not-run=false`；`git diff --check` 无输出。

- [ ] **Step 5: 扫描禁止能力和临时产物**

Run:

```powershell
rg -n "minimum_turnover|participation_rate|order.*1%|run_swarm|swarm-run" joinquant/strategies/strategy-003/research scripts/research
Get-ChildItem .local\market-data\transfers -File -Recurse
Get-ChildItem .local -Filter *.csv -File -Recurse
Get-ChildItem .local -Directory -Recurse | Where-Object { $_.Name -in @('.attempts', '.benchmark-work') }
```

Expected: 生产代码中没有海龟流动性规则或群体分析调用；三个临时产物扫描无输出。测试夹具或文档中的禁止词必须逐项解释，不能误报为生产行为。

- [ ] **Step 6: 写验证报告并更新 OpenSpec 任务证据**

验证报告必须列出：真实候选筛选、批次/快照身份、实际场景数、逐场景耗时、全部测试命令与计数、分析 ID、Vibe 单体运行 ID、报告路径、清理证明、已验证与无法验证项，以及最终推荐结论。不得把本地结论写成正式回测结论。

- [ ] **Step 7: 授权后提交最终实现**

```powershell
$Allowed = @(
  ".comet.yaml",
  "openspec/changes/build-turtle-etf-local-research-workflow/proposal.md",
  "openspec/changes/build-turtle-etf-local-research-workflow/design.md",
  "openspec/changes/build-turtle-etf-local-research-workflow/tasks.md",
  "openspec/changes/build-turtle-etf-local-research-workflow/specs/local-quant-research-workflow/spec.md",
  "openspec/changes/build-turtle-etf-local-research-workflow/specs/standard-strategy-analysis-data/spec.md",
  "openspec/changes/build-turtle-etf-local-research-workflow/specs/turtle-etf-local-research/spec.md",
  "docs/superpowers/specs/2026-07-16-turtle-independent-asset-expansion-design.md",
  "docs/superpowers/plans/2026-07-16-turtle-independent-asset-expansion.md",
  "docs/superpowers/reports/2026-07-16-turtle-independent-asset-expansion-verify.md",
  "scripts/research/market_data/economic_returns.py",
  "scripts/research/market_data/candidate_screen.py",
  "scripts/research/market_data/query.py",
  "scripts/research/market_data/joinquant_export.py",
  "joinquant/strategies/strategy-003/research/asset-expansion-plan.json",
  "joinquant/strategies/strategy-003/research/baseline.json",
  "joinquant/strategies/strategy-003/research/code-identity.json",
  "joinquant/strategies/strategy-003/research/turtle_etf/asset_expansion.py",
  "joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_delayed.py",
  "joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_inputs.py",
  "joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py",
  "joinquant/strategies/strategy-003/research/turtle_etf/result_adapter.py",
  "scripts/research/quant_analysis/schemas/analysis-plan.schema.json",
  "scripts/research/quant_analysis/analysis_plan.py",
  "scripts/research/quant_analysis/orchestration.py",
  "scripts/research/quant_analysis/scenario_matrix.py",
  "scripts/research/quant_analysis/unified_analysis.py",
  "scripts/research/quant_analysis/independence.py",
  "scripts/research/quant_analysis/trend_utilization.py",
  "scripts/research/quant_analysis/reporting.py",
  "tests/local_quant_research/test_market_data_economic_returns.py",
  "tests/local_quant_research/test_market_data_candidate_screen.py",
  "tests/local_quant_research/test_market_data_query.py",
  "tests/local_quant_research/test_joinquant_export.py",
  "tests/local_quant_research/test_turtle_asset_expansion.py",
  "tests/local_quant_research/test_turtle_vectorbt_delayed.py",
  "tests/local_quant_research/test_turtle_vectorbt_inputs.py",
  "tests/local_quant_research/test_turtle_vectorbt_engine.py",
  "tests/local_quant_research/test_turtle_result_adapter.py",
  "tests/local_quant_research/test_turtle_vectorbt_performance.py",
  "tests/quant_analysis/test_analysis_plan.py",
  "tests/quant_analysis/test_orchestration.py",
  "tests/quant_analysis/test_asset_expansion_analysis.py",
  "tests/quant_analysis/test_unified_analysis.py",
  "tests/quant_analysis/test_independence.py",
  "tests/quant_analysis/test_trend_utilization.py",
  "tests/quant_analysis/test_reporting.py"
)
$Allowed | ForEach-Object { git diff -- $_ }
git add -- $Allowed
# 仅当 Step 4 证明 affected mapping 确实需要且已逐行复核时，再单独执行：
# git add -- .build-and-verify/config.json
git diff --cached --check
git diff --cached --name-only
git status --short
git commit -m "实现：完成独立资产扩展本地研究"
```

Expected: 提交前逐文件检查 diff（差异）和 staged（暂存）清单；若任一文件含无关用户改动，先按 hunk（差异块）拆分且不得覆盖。清单不得包含 `.local`、密钥、Cookie（浏览器凭证）或清单外路径；只有用户明确授权后提交。若前面已按任务提交，Git（版本管理）会自动忽略未变化文件。

---

## 实施顺序与审查门禁

1. Task 1 锁定文档契约。
2. Task 2—3 完成共享行情能力。
3. Task 4 完成真实延迟执行并验证 `delay=0` 不漂移。
4. Task 5 生成场景矩阵和精确快照。
5. Task 6—8 完成通用分析与报告。
6. Task 9 获取真实聚宽候选数据并冻结筛选。
7. Task 10 逐次运行全部实际场景。
8. Task 11 生成确定性分析、Vibe 单体审计和报告。
9. Task 12 完成全量验证、清理和人工确认前停止。

每个 Task 完成后进行一次规格一致性审查和一次代码质量审查；有阻断项时先修复并复审，不带阻断进入下一 Task。
