---
change: refactor-local-research-three-layer-architecture
design-doc: docs/superpowers/specs/2026-07-17-local-research-three-layer-architecture-design.md
base-ref: ea195d36501848d3ba677b1e97c1aba667da7e1e
---

# 本地研究三层架构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把本地量化研究重构为 vectorbt（向量化回测库）唯一账本底层、共享 Skill（技能）能力层和单一公开 Strategy Module（策略模块），并让每次运行结果可原样晋升为策略目录内的自包含档案。

**Architecture:** 共享 `vectorbt_runtime.py` 独占 vectorbt 导入并把 `Portfolio.from_order_func()` 包装为只读惰性 `ExecutionLedger`；共享 runner（运行器）、结果包和晋升模块只依赖项目自有 contracts（契约）。策略只通过 `strategy.py:MODULE` 暴露准备、后续订单程序和扩展表，私有 Numba（即时编译器）内核不接触 vectorbt 上下文；生产入口在一个提交中切换到配置 v2 并删除旧策略 CLI（命令行接口）与手工延迟账本。

**Tech Stack:** Python 3.12、vectorbt 1.1.0、Numba 0.66.0、NumPy、Pandas、PyArrow（列式内存）、DuckDB（内存分析数据库）、ctypes（系统调用接口）、Pytest（测试框架）、OpenSpec（开放规格）、Build and Verify（构建与验证）

## Global Constraints

- 正式回测和模拟交易仍只在 JoinQuant（聚宽）云端运行；本地结果只能表述为研究执行事实，不得声称推荐、稳健性通过或实盘准入。
- 最终态只有 `scripts/research/local_quant_research/vectorbt_runtime.py` 可导入 vectorbt 或其内部类型；策略、结果 writer（写入器）、archive（档案）和 Skill（技能）均不得导入 vectorbt。迁移期间新路径仅供测试，旧生产路径在 Task 8 的同一提交中切换并删除。
- `Portfolio.from_order_func()` 是即时与延迟执行的唯一成交、费用、现金、持仓和净值账本；不得保留 Python 手工账本、`Portfolio.from_orders()` 重放或第二套生产路径。
- 日常 `run` 只执行一个冷启动和一个预热；3 个冷进程与 5 次预热只属于发布性能验证。
- 固定机器相对门禁为时间、峰值进程内存和同逻辑结果包体积均不超过基线 5%；每次冷、热执行的绝对门禁均为 180 秒。
- 性能场景固定为 3,432 日 × 11 ETF、3,432 日 × 17 ETF、以及 `additional_delay_days=1`；正确性要求成交、费用、现金、持仓、净值、策略状态和逻辑摘要零差异。
- Windows（微软桌面系统）峰值内存使用标准库 ctypes 调用 `GetProcessMemoryInfo`，不得新增 `psutil` 或其他依赖。
- `.local/quant-research/<strategy_id>/<run_id>/` 必须直接满足 `local-research-package/2` 自包含布局；promotion（晋升）只能逐字节复制、校验并原子发布，不能重算。
- `analysis_id` 只作为策略档案目录别名；包内 `run_id` 和所有文件字节保持不变，共享行情数据不得复制进档案。
- 所有 Python 命令必须使用 `.\.venv\Scripts\python.exe`；不使用系统 Python，不安装或升级依赖。
- 当前规划产物位于未提交的 `main`。开始实现前必须按 Comet（变更工作流）选择并创建 branch（分支）或 worktree（隔离工作区），不得直接在 `main` 提交。
- 每个生产改动必须先出现对应失败测试，再写最小实现；每个 Task（任务）独立通过、独立审查、独立提交。

## File Map

- `scripts/research/local_quant_research/contracts.py`：后端中立的策略、订单、账本、结果扩展和运行状态契约。
- `scripts/research/local_quant_research/strategy_loader.py`：安全加载 `strategy_root/module/symbol`，验证 descriptor（描述符）和源码边界。
- `scripts/research/local_quant_research/vectorbt_runtime.py`：唯一 vectorbt Adapter（适配器），包含共享回调、即时/后续运行和惰性账本。
- `scripts/research/local_quant_research/scenario.py`、`performance.py`：单场景编排、冷热确定性、发布性能和 Windows 峰值内存。
- `scripts/research/local_quant_research/result_package.py`：标准四表、扩展、机械报告、回读勾稽与原子物化。
- `scripts/research/local_quant_research/archive.py`：只复制并校验 archive-ready package（可归档结果包）。
- `scripts/research/local_quant_research/runner.py`、`evidence.py`、`cli.py`：配置 v2、输入冻结、固定子进程、运行复用、`run/promote` 公开入口。
- `scripts/research/analysis_data/`：统一读取新本地结果、策略扩展和既有聚宽归档。
- `joinquant/strategies/strategy-003/research/turtle_etf/strategy.py`：唯一公开 `MODULE`。
- `joinquant/strategies/strategy-003/research/turtle_etf/_kernel.py`、`_attribution.py`、`_delayed.py`：策略私有内核、扩展和延迟计划。
- `joinquant/strategies/strategy-003/research/project-run.json`：配置 v2，只声明策略入口、快照、场景配置和输入。
- `.agents/skills/run-local-quant-research/SKILL.md`：只编排共享 `run` 与 `promote`。
- `tests/local_quant_research/`：契约、runtime（运行时）、结果包、档案、双策略入口、E2E（端到端）和性能门禁。

---

### Task 1: 冻结旧路径的等价性与发布性能基线

**Files:**
- Create: `tests/local_quant_research/test_local_research_equivalence.py`
- Create: `tests/local_quant_research/fixtures/local-research-v1-baseline.json`
- Create: `tests/local_quant_research/fixtures/performance-baseline.json`
- Modify: `tests/local_quant_research/test_turtle_vectorbt_performance.py`
- Modify: `tests/local_quant_research/test_contract_fixtures.py`

**Interfaces:**
- Consumes: 现有 `run_vectorbt_simulation()`、`to_joinquant_facts()`、11 ETF 基线、17 ETF 扩展和延迟 1 日配置。
- Produces: `logic_digest(facts) -> str`、三个场景的行为基线与固定机器性能基线；后续迁移任务以这些 fixture（夹具）为零差异裁判。

- [ ] **Step 1: 写失败的行为冻结测试**

在新测试中对每个场景固定以下摘要，摘要输入必须包含成交数量/价格/费用、每日现金/持仓/净值、海龟单位/共同止损/原因码，不能只比较最终收益：

```python
SCENARIOS = (
    "immediate-11-etf",
    "immediate-17-etf",
    "delayed-11-etf-1d",
)

def assert_equivalent(actual: dict[str, object], expected: dict[str, object]) -> None:
    assert actual["schema_version"] == 1
    assert actual["scenario"] == expected["scenario"]
    for key in ("orders", "fees", "cash", "positions", "value", "state", "logic"):
        assert actual[key] == expected[key]

def test_all_reference_scenarios_have_complete_equivalence_fixtures(repo_root: Path) -> None:
    fixture = json.loads((repo_root / "tests/local_quant_research/fixtures/local-research-v1-baseline.json").read_text(encoding="utf-8"))
    assert tuple(item["scenario"] for item in fixture["scenarios"]) == SCENARIOS
    assert all(set(item) == {"scenario", "orders", "fees", "cash", "positions", "value", "state", "logic"} for item in fixture["scenarios"])
```

- [ ] **Step 2: 运行测试并确认 fixture 尚未存在**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_local_research_equivalence.py tests\local_quant_research\test_contract_fixtures.py -q
```

Expected: FAIL，指出两份新 fixture 缺失或未包含三个完整场景。

- [ ] **Step 3: 使用旧生产路径生成并固化基线**

在测试辅助函数中从现有 `LocalExecutionFacts` 构造 canonical（规范化）摘要；对浮点数组统一编码为 little-endian float64（小端双精度），逻辑字段使用排序 JSON，再把真实摘要写入 `local-research-v1-baseline.json`。发布性能 fixture 固定以下结构并由现有三个真实场景采集值填充：

```json
{
  "schema_version": 1,
  "environment": {"python": "3.12", "vectorbt": "1.1.0"},
  "sampling": {"cold_processes": 3, "warm_runs": 5, "statistic": "median"},
  "limits": {"relative_ratio": 1.05, "absolute_seconds": 180.0},
  "scenarios": {}
}
```

采集命令必须从项目 `.venv` 启动旧公开入口；fixture 只保存摘要、阶段时间、中位峰值内存和包体积，不提交 `.local` 运行目录。

- [ ] **Step 4: 复跑冻结测试和旧 E2E（端到端）测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_local_research_equivalence.py tests\local_quant_research\test_turtle_vectorbt_performance.py tests\local_quant_research\test_turtle_e2e.py -q
```

Expected: PASS；三个场景都有行为摘要，性能 fixture 明确 3/5 采样与 5%/180 秒门槛。

- [ ] **Step 5: 提交冻结证据**

```powershell
git add -- tests/local_quant_research/test_local_research_equivalence.py tests/local_quant_research/fixtures/local-research-v1-baseline.json tests/local_quant_research/fixtures/performance-baseline.json tests/local_quant_research/test_turtle_vectorbt_performance.py tests/local_quant_research/test_contract_fixtures.py
git diff --cached --name-only
git commit -m "测试：冻结本地研究等价性与性能基线"
```

---

### Task 2: 建立后端中立 contracts 与安全 Strategy Module 加载器

**Files:**
- Modify: `scripts/research/local_quant_research/contracts.py`
- Create: `scripts/research/local_quant_research/strategy_loader.py`
- Create: `tests/local_quant_research/test_strategy_contract.py`
- Create: `tests/local_quant_research/fixtures/minimal_strategy/strategy.py`
- Create: `tests/local_quant_research/fixtures/minimal_strategy_b/strategy.py`

**Interfaces:**
- Consumes: `SnapshotView`、仓库根目录、配置 v2 的 `strategy.root/module/symbol`。
- Produces: `StrategyDescriptor`、`PreparedStrategy`、`LedgerInput`、`OrderBuffer`、`OrderProgram`、`ExecutionRun`、`ExecutionBundle`、`ResultExtension`、`load_strategy() -> LoadedStrategy`。

- [ ] **Step 1: 写 contracts 和双策略加载失败测试**

测试真实海龟模块和最小 fixture 模块可由同一加载器加载，并拒绝绝对路径、`..`、仓库外 module file、未知 symbol、重复/缺失 source file、旧 `command/project_entry` 字段：

```python
@pytest.mark.parametrize("invalid", ("C:/outside", "../outside", "/outside"))
def test_loader_rejects_strategy_root_escape(repo_root: Path, invalid: str) -> None:
    with pytest.raises(ConfigurationError, match="strategy_root"):
        load_strategy(repo_root, {"root": invalid, "module": "strategy", "symbol": "MODULE"})

def test_shared_loader_accepts_two_strategy_modules(repo_root: Path) -> None:
    first = load_strategy(repo_root, {"root": "tests/local_quant_research/fixtures/minimal_strategy", "module": "strategy", "symbol": "MODULE"})
    second = load_strategy(repo_root, {"root": "tests/local_quant_research/fixtures/minimal_strategy_b", "module": "strategy", "symbol": "MODULE"})
    assert (first.descriptor.strategy_id, second.descriptor.strategy_id) == ("minimal-fixture", "minimal-fixture-b")
```

- [ ] **Step 2: 运行测试并确认共享接口尚不存在**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_strategy_contract.py -q
```

Expected: FAIL，导入 `strategy_loader` 或新 contract 类型失败。

- [ ] **Step 3: 实现精确只读 contracts**

按技术设计建立 `slots=True, frozen=True` 数据类、数值枚举和 `SegmentView/FillEvent` namedtuple（命名元组）。`OrderBuffer` 必须验证所有数组等长，写保护只在 runtime 完成后启用；`StrategyEvidenceError` 必须携带稳定 `code`：

```python
SIDE_NONE, SIDE_BUY, SIDE_SELL = 0, 1, -1
FILL_IGNORED, FILL_ACCEPTED, FILL_REJECTED = 0, 1, 2

class StrategyEvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

@dataclass(frozen=True, slots=True)
class PreparedStrategy:
    ledger_input: LedgerInput
    primary_program: OrderProgram
    context: object

@dataclass(frozen=True, slots=True)
class ExecutionBundle:
    primary: ExecutionRun
    final: ExecutionRun
    stages: tuple[str, ...]
```

其余字段逐字采用 Design Doc（技术设计）3.1–3.4；`StrategyModule` Protocol（协议）的方法名和返回类型不得变化。

- [ ] **Step 4: 实现 loader 与最小测试策略**

`load_strategy()` 只临时把已解析的 `strategy_root` 加入 `sys.path`；导入后验证 `module.__file__`、descriptor 的每个 POSIX（可移植路径）source file 都在 root 内，再恢复 `sys.path`。返回值必须同时保留模块对象、绝对源码路径和 descriptor，供 code identity（代码身份）与档案复制复用：

```python
@dataclass(frozen=True, slots=True)
class LoadedStrategy:
    module: StrategyModule
    root: Path
    source_paths: tuple[Path, ...]
    descriptor: StrategyDescriptor
```

两个最小 fixture 的 `MODULE.prepare()` 都返回一列、两日、无订单 `OrderProgram`，但 descriptor identity（描述符身份）不同；它们只用于证明共享入口无需修改即可加载第二个策略，且不依赖海龟字段。

- [ ] **Step 5: 运行 contract、runner 和共享去策略化测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_strategy_contract.py tests\local_quant_research\test_runner.py tests\local_quant_research\test_generic_e2e.py::test_shared_sources_do_not_depend_on_one_strategy -q
```

Expected: PASS；共享源码不包含 `turtle`、`strategy-003` 或具体证券代码。

- [ ] **Step 6: 提交接口接缝**

```powershell
git add -- scripts/research/local_quant_research/contracts.py scripts/research/local_quant_research/strategy_loader.py tests/local_quant_research/test_strategy_contract.py tests/local_quant_research/fixtures/minimal_strategy/strategy.py tests/local_quant_research/fixtures/minimal_strategy_b/strategy.py
git diff --cached --name-only
git commit -m "重构：建立策略模块共享契约"
```

---

### Task 3: 抽取标准结果包与统一分析视图

**Files:**
- Create: `scripts/research/local_quant_research/result_package.py`
- Modify: `scripts/research/analysis_data/manifest.py`
- Modify: `scripts/research/analysis_data/views.py`
- Modify: `scripts/research/analysis_data/__init__.py`
- Create: `tests/local_quant_research/test_result_package.py`
- Modify: `tests/local_quant_research/test_analysis_data_views.py`

**Interfaces:**
- Consumes: `ExecutionBundle.final.ledger`、scenario/config/code/market/runtime evidence（证据）和 `tuple[ResultExtension, ...]`。
- Produces: `write_result_package(request: ResultPackageRequest) -> ResultPackage`、`validate_result_package(path) -> Mapping[str, object]`，以及可查询新包、扩展和聚宽档案的 `open_analysis_source()`。

- [ ] **Step 1: 写四表、扩展、报告和单次物化失败测试**

用 fake ledger（伪账本）记录 `orders/assets/cash/value` 属性访问次数；断言四张核心表、扩展表、跨表键、SHA256、机械报告禁词和回读失败清理：

```python
FORBIDDEN_REPORT_PHRASES = ("推荐", "稳健性通过", "适合实盘", "实盘准入")

def test_writer_materializes_one_package_without_recomputing_ledger(tmp_path: Path, request: ResultPackageRequest, counting_ledger: CountingLedger) -> None:
    package = write_result_package(replace(request, output_dir=tmp_path / "result"))
    manifest = validate_result_package(package.path)
    assert set(manifest["datasets"]) == {"results", "balances", "positions", "orders"}
    assert counting_ledger.calls == {"orders": 1, "assets": 1, "cash": 1, "value": 1}
    report = (package.path / "report/execution-summary.md").read_text(encoding="utf-8")
    assert not any(phrase in report for phrase in FORBIDDEN_REPORT_PHRASES)
```

- [ ] **Step 2: 运行测试并确认共享 writer 尚不存在**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_result_package.py tests\local_quant_research\test_analysis_data_views.py -q
```

Expected: FAIL，无法导入 `result_package` 或 analysis view（分析视图）不识别 `local-research-package/2`。

- [ ] **Step 3: 实现核心 Schema、扩展约束与 package request**

把现有 `result_adapter.py` 中四表 schema、公共勾稽、逻辑摘要、Arrow（列式内存）构造和 manifest 引用迁入共享模块；策略 action/reason code 不能进入共享文件。固定请求对象如下：

```python
@dataclass(frozen=True, slots=True)
class ResultPackageRequest:
    strategy_id: str
    scenario_id: str
    run_id: str
    output_dir: Path
    execution: ExecutionBundle
    extensions: tuple[ResultExtension, ...]
    code_files: Mapping[str, Path]
    config_documents: Mapping[str, object]
    evidence_documents: Mapping[str, object]

@dataclass(frozen=True, slots=True)
class ResultPackage:
    path: Path
    manifest: Mapping[str, object]
    package_sha256: str
```

每个 `ResultExtension` 的 name 必须唯一且匹配 `[a-z][a-z0-9_-]{0,63}`；writer 固定路径、Snappy（列式压缩）参数和文件名，策略无权直接写 Parquet（列式文件）。

- [ ] **Step 4: 实现一次物化、回读验证与原子发布**

先在同级 `.<run_id>.<uuid>.tmp` 写 `code/config/data/extensions/evidence/report`，每张表只调用一次 `pq.write_table()`；立刻回读 schema/行数/键/勾稽并生成 `local-research-package/2` 清单，最后 `os.replace()`。任何异常只删除本次暂存目录；若完成目录已存在，完整摘要相同则复用，冲突则失败。

- [ ] **Step 5: 扩展 analysis_data 统一读取三类来源**

为 `open_analysis_source()` 增加新包识别：返回 `authority=local_research`、`backend=vectorbt`、`formula_version`、四张核心表和策略扩展；既有聚宽归档读取分支保持原样。查询扩展时显式接收 extension name，不在共享层硬编码 `turtle_etf`。

- [ ] **Step 6: 运行结果包、分析视图和旧适配测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_result_package.py tests\local_quant_research\test_analysis_data_views.py tests\local_quant_research\test_turtle_result_adapter.py -q
```

Expected: PASS；旧测试仍作为迁移期对照，标准四表与扩展读取一致。

- [ ] **Step 7: 提交共享结果包**

```powershell
git add -- scripts/research/local_quant_research/result_package.py scripts/research/analysis_data/manifest.py scripts/research/analysis_data/views.py scripts/research/analysis_data/__init__.py tests/local_quant_research/test_result_package.py tests/local_quant_research/test_analysis_data_views.py
git diff --cached --name-only
git commit -m "重构：抽取本地研究标准结果包"
```

---

### Task 4: 实现策略目录自包含档案的原样晋升

**Files:**
- Create: `scripts/research/local_quant_research/archive.py`
- Create: `tests/local_quant_research/test_archive_promotion.py`
- Modify: `scripts/research/local_quant_research/cli.py`

**Interfaces:**
- Consumes: `.local/quant-research/<strategy_id>/<run_id>/manifest.json` 和 CLI（命令行接口）的 `strategy_id/run_id/analysis_id`。
- Produces: `promote_archive(repo_root, strategy_id, run_id, analysis_id) -> ArchiveResult`；目标固定为 `joinquant/strategies/<strategy_id>/research/archives/<analysis_id>/`。

- [ ] **Step 1: 写布局、字节相等、幂等和冲突失败测试**

测试必须把策略加载、vectorbt、PyArrow writer（列式写入器）替换为调用即失败对象，证明 promotion（晋升）没有重算；覆盖非法 `analysis_id`、源包不完整、同内容复用、异内容冲突、复制中断清理和删除 `.local` 后仍可查询：

```python
@pytest.mark.parametrize("analysis_id", ("Upper", "../escape", "a/b", "", "x" * 65))
def test_promote_rejects_invalid_analysis_id(repo_root: Path, analysis_id: str) -> None:
    result = promote_archive(repo_root, "strategy-003", "a" * 64, analysis_id)
    assert result.status == "failed"
    assert result.reasons == ("invalid_analysis_id",)

def test_promote_preserves_every_source_byte(complete_package: Path, repo_root: Path) -> None:
    result = promote_archive(repo_root, "strategy-003", complete_package.name, "baseline-v2")
    assert result.status == "complete"
    assert _tree_digests(result.source) == _tree_digests(result.target)
    assert json.loads((result.target / "manifest.json").read_text(encoding="utf-8"))["run_id"] == complete_package.name
```

- [ ] **Step 2: 运行测试并确认 archive 模块尚不存在**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_archive_promotion.py -q
```

Expected: FAIL，无法导入 `archive` 或 CLI 不支持 `promote`。

- [ ] **Step 3: 实现独立结果和严格源/目标定位**

使用后端中立返回对象，不复用 `RunResult` 的三种研究状态：

```python
@dataclass(frozen=True, slots=True)
class ArchiveResult:
    status: Literal["complete", "failed", "conflict"]
    reused: bool
    source: Path | None
    target: Path | None
    reasons: tuple[str, ...]
```

`strategy_id` 必须精确定位 `joinquant/strategies/<strategy_id>`；`run_id` 只能是 64 位小写 SHA256；`analysis_id` 必须匹配 `[a-z0-9][a-z0-9._-]{0,63}`。目标路径不得由调用者传入，也不得从未验证配置推导。

- [ ] **Step 4: 实现逐文件复制、复核和原子发布**

源包先由 `validate_result_package()` 只读验证 complete（完整）状态；遍历普通文件并拒绝 symlink（符号链接）、hardlink（硬链接）和目录连接。目标不存在时写同级 `.<analysis_id>.<uuid>.tmp`，每个文件复制后立即核对长度与 SHA256，整包再次比较 tree digest（目录摘要）后 `os.replace()`；失败只删除本次暂存目录。

目标已存在时不写任何字节：tree digest 完全一致返回 `reused=True`，否则返回 `status="conflict"`。不得修改源 manifest，也不得把 `analysis_id` 注入包内文件。

- [ ] **Step 5: 接入共享 promote 命令**

扩展 `_parser()`，用户公开参数必须精确为：

```python
promote = subparsers.add_parser("promote")
promote.add_argument("--strategy-id", required=True)
promote.add_argument("--run-id", required=True)
promote.add_argument("--analysis-id", required=True)
```

CLI 输出排序 JSON；complete（完整）为 0、conflict（冲突）为 1、failed（失败）为 2。`promote` 分支不得加载 `strategy_loader` 或 `vectorbt_runtime`。

- [ ] **Step 6: 运行晋升、结果查询和 CLI 测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_archive_promotion.py tests\local_quant_research\test_analysis_data_views.py tests\local_quant_research\test_skill_contract.py -q
```

Expected: PASS；删除源 `.local` fixture 后，档案四表、扩展、backend（后端）和 formula version（公式版本）仍可查询。

- [ ] **Step 7: 提交晋升能力**

```powershell
git add -- scripts/research/local_quant_research/archive.py scripts/research/local_quant_research/cli.py tests/local_quant_research/test_archive_promotion.py
git diff --cached --name-only
git commit -m "功能：支持本地研究结果原样晋升"
```

---

### Task 5: 统一单场景、性能证据、runner 与固定 CLI

**Files:**
- Modify: `scripts/research/local_quant_research/contracts.py`
- Create: `scripts/research/local_quant_research/vectorbt_runtime.py`
- Create: `scripts/research/local_quant_research/scenario.py`
- Create: `scripts/research/local_quant_research/performance.py`
- Modify: `scripts/research/local_quant_research/runner.py`
- Modify: `scripts/research/local_quant_research/evidence.py`
- Modify: `scripts/research/local_quant_research/cli.py`
- Modify: `.agents/skills/run-local-quant-research/SKILL.md`
- Modify: `.agents/skills/run-local-quant-research/agents/openai.yaml`
- Modify: `tests/local_quant_research/test_runner.py`
- Modify: `tests/local_quant_research/test_evidence.py`
- Modify: `tests/local_quant_research/test_skill_contract.py`
- Modify: `tests/local_quant_research/test_generic_e2e.py`
- Create: `tests/local_quant_research/test_vectorbt_runtime.py`

**Interfaces:**
- Consumes: 配置 v2、`LoadedStrategy`、共享行情 `SnapshotView`、`run_vectorbt()`、`write_result_package()`。
- Produces: `execute_scenario(request) -> ScenarioOutcome`、`run_project(config_path, repo_root) -> RunResult`、固定 `_execute` 子进程协议和稳定三状态语义。

- [ ] **Step 1: 把 runner/Skill 改成配置 v2 并写最小 runtime 失败测试**

新有效配置只包含以下顶层键；分别注入旧字段并断言 `ConfigurationError("legacy_run_field")`：

```python
VALID_V2 = {
    "schema_version": 2,
    "project_id": "minimal-fixture",
    "strategy": {
        "root": "tests/local_quant_research/fixtures/minimal_strategy",
        "module": "strategy",
        "symbol": "MODULE",
    },
    "snapshot_id": "a" * 64,
    "snapshot_requirements": {},
    "scenario_config": "tests/local_quant_research/fixtures/minimal-scenario.json",
    "declared_inputs": [],
}

LEGACY_RUN_FIELDS = ("command", "project_entry", "code_identity", "required_outputs", "output_root", "stop_states")
```

Skill 测试断言只公开共享 `run --config` 和 `promote --strategy-id --run-id --analysis-id`，不出现策略字段解释、任意命令或项目入口。`test_vectorbt_runtime.py` 同时定义一列、两日、无订单 `OrderProgram`，断言 `run_vectorbt()` 返回 `ExecutionRun` 且 ledger value（账本净值）为只读数组。

- [ ] **Step 2: 运行共享 runner、evidence 和 Skill 测试并确认失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_runner.py tests\local_quant_research\test_evidence.py tests\local_quant_research\test_skill_contract.py tests\local_quant_research\test_generic_e2e.py tests\local_quant_research\test_vectorbt_runtime.py -q
```

Expected: FAIL，旧配置仍要求 `project_entry/command/code_identity/required_outputs/output_root/stop_states`。

- [ ] **Step 3: 重写 RunConfig 和 v2 配置加载**

`RunConfig` 固定为以下输入；输出根和停止状态改为模块常量，不能由项目覆盖：

```python
RUN_OUTPUT_ROOT = Path(".local/quant-research")
RUN_STATUSES = ("complete", "evidence_insufficient", "failed")

@dataclass(frozen=True, slots=True)
class RunConfig:
    project_id: str
    strategy_root: Path
    strategy_module: str
    strategy_symbol: str
    snapshot_id: str
    snapshot_requirements: Mapping[str, object]
    scenario_config: Path
    declared_inputs: tuple[Path, ...]
    document: Mapping[str, object]
```

配置加载先拒绝未知/旧字段，再解析仓库内路径；运行身份由冻结的配置、行情 manifest、descriptor source files、固定共享 runtime 文件集和已安装依赖共同生成，不再读取手工 `code-identity.json`。

- [ ] **Step 4: 先实现最小共享 vectorbt runtime 与日常一次冷/热执行**

先在 `test_vectorbt_runtime.py` 写最小无订单 `OrderProgram` 的失败测试，再创建共享 `run_vectorbt()`：它必须调用 `Portfolio.from_order_func()`，隐藏原始 Portfolio（组合），并返回 `ExecutionRun`。本任务验收边界是两个最小 Strategy Module（策略模块）能通过共享 runtime 完成一列、两日、无订单运行；稳定优先级、完整成交回调、惰性缓存与延迟程序在 Task 6 的独立失败测试中扩展。

`execute_scenario()` 必须按固定阶段计时：`strategy_load/strategy_prepare/primary_vectorbt/followup_prepare/followup_vectorbt/core_facts/strategy_extensions/parquet_materialize/readback_validate/report_and_manifest`。每个 `run` 在一个全新 `_execute` 子进程里执行一次 cold（冷）和一次 warm（热），比较完整 execution digest（执行摘要）；两次任一超过 180 秒或摘要不同，返回稳定失败原因并清理暂存目录。

即时路径使用 `ExecutionBundle(primary, primary, ("primary",))`；策略返回后续程序时，运行第二次 `run_vectorbt()` 并使用 `ExecutionBundle(primary, final, ("primary", "followup"))`。结果 writer 只读取 `final`。

- [ ] **Step 5: 固定父进程与私有 `_execute` 协议**

父 runner 继续保留现有输入冻结、清洁环境、证据不足映射、失败 attempt（尝试）证据、完成目录复用和原子发布；子进程命令只能由共享代码生成：

```python
command = (
    repo_root / ".venv/Scripts/python.exe",
    repo_root / "scripts/research/local_quant_research/cli.py",
    "_execute",
    "--frozen-inputs", execution_root / "request.json",
    "--staging", staging,
)
```

`_execute` 不写入公开帮助，不接受项目配置提供的命令。只有 `ConfigurationError`、`StrategyEvidenceError` 和行情缺失的显式 evidence（证据）分支映射为 `evidence_insufficient`；未知异常统一为 `failed`，不把 traceback（堆栈）或敏感环境写入 manifest。

- [ ] **Step 6: 更新 Skill 与两个共享 E2E 测试**

Skill 明确结果包本身 archive-ready（可归档），并保持“单次调用、单个场景、返回调用者”边界。`test_generic_e2e.py` 使用最小 Strategy Module 从公开 CLI 完成 v2 `run`，断言四表、自包含目录、机械报告、复用和无 DuckDB（数据库）残留；另加配置缺失与摘要冲突入口测试。

- [ ] **Step 7: 运行共享全链路测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_runner.py tests\local_quant_research\test_evidence.py tests\local_quant_research\test_skill_contract.py tests\local_quant_research\test_generic_e2e.py tests\local_quant_research\test_vectorbt_runtime.py -q
```

Expected: PASS；最小策略不修改共享入口即可完成运行，状态仍严格为 `complete/evidence_insufficient/failed`。

- [ ] **Step 8: 提交共享编排**

```powershell
git add -- scripts/research/local_quant_research/contracts.py scripts/research/local_quant_research/vectorbt_runtime.py scripts/research/local_quant_research/scenario.py scripts/research/local_quant_research/performance.py scripts/research/local_quant_research/runner.py scripts/research/local_quant_research/evidence.py scripts/research/local_quant_research/cli.py .agents/skills/run-local-quant-research/SKILL.md .agents/skills/run-local-quant-research/agents/openai.yaml tests/local_quant_research/test_runner.py tests/local_quant_research/test_evidence.py tests/local_quant_research/test_skill_contract.py tests/local_quant_research/test_generic_e2e.py tests/local_quant_research/test_vectorbt_runtime.py
git diff --cached --name-only
git commit -m "重构：统一本地研究共享运行入口"
```

---

### Task 6: 建立 vectorbt 唯一账本并覆盖即时与延迟执行

**Files:**
- Modify: `scripts/research/local_quant_research/vectorbt_runtime.py`
- Modify: `tests/local_quant_research/test_vectorbt_runtime.py`

**Interfaces:**
- Consumes: `LedgerInput`、预分配 `OrderBuffer`、策略 `OrderProgram` 的三个 callback（回调）和只读 trace（轨迹）。
- Produces: `run_vectorbt(ledger_input, program) -> ExecutionRun`；`ExecutionLedger.orders/assets/cash/value/trades/positions/returns` 惰性、只读并各计算一次。

- [ ] **Step 1: 写共享回调、优先级、成交事件和惰性缓存失败测试**

测试一组两列订单：同优先级按原 column（列）稳定排序，卖出优先买入；`order_func_nb` 只把启用槽转换为 `nb.order_nb`；`after_fill_nb` 只在真实成交后推进状态。另用 monkeypatch（运行期替换）统计 portfolio accessor（组合访问器）调用与内存共享：

```python
def test_ledger_computes_and_freezes_each_view_once(portfolio: FakePortfolio) -> None:
    ledger = ExecutionLedger(portfolio)
    first = ledger.value
    second = ledger.value
    assert first is second
    assert first.flags.writeable is False
    assert portfolio.calls["value"] == 1

def test_priority_is_stable_for_equal_orders() -> None:
    assert stable_call_sequence(np.array([2, 1, 1]), np.array([0, 1, 2])) == (1, 2, 0)
```

- [ ] **Step 2: 运行 runtime 扩展测试并确认高级账本约束缺失**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_vectorbt_runtime.py -q
```

Expected: FAIL，最小 runtime 尚未满足稳定优先级、成交回调、惰性缓存、共享内存或 primary/follow-up（主/后续）双程序约束。

- [ ] **Step 3: 实现唯一共享 `from_order_func()` 接线**

`vectorbt_runtime.py` 是仓库唯一允许 `import vectorbt as vbt` 与 `from vectorbt.portfolio import nb` 的模块。`run_vectorbt()` 固定 `cash_sharing`、group、frequency、`max_logs=0`，关闭未使用的持仓周期跟踪；回调生命周期必须为：reset buffer → 构造 `SegmentView` → 策略 prepare → 原地稳定 call sequence → 转换订单 → 构造 `FillEvent` → 策略 after fill → 可选 after segment。

```python
@dataclass(frozen=True, slots=True)
class _SpecializedCallbacks:
    order_func_nb: object
    order_args: tuple[object, ...]
    pre_sim_func_nb: object
    pre_sim_args: tuple[object, ...]
    pre_segment_func_nb: object
    pre_segment_args: tuple[object, ...]
    post_order_func_nb: object
    post_order_args: tuple[object, ...]
    post_segment_func_nb: object
    post_segment_args: tuple[object, ...]

def run_vectorbt(ledger_input: LedgerInput, program: OrderProgram) -> ExecutionRun:
    callbacks = _specialize_program(program)
    rows, columns = ledger_input.close.shape
    portfolio = vbt.Portfolio.from_order_func(
        _close_frame(ledger_input),
        callbacks.order_func_nb,
        *callbacks.order_args,
        pre_sim_func_nb=callbacks.pre_sim_func_nb,
        pre_sim_args=callbacks.pre_sim_args,
        pre_segment_func_nb=callbacks.pre_segment_func_nb,
        pre_segment_args=callbacks.pre_segment_args,
        post_order_func_nb=callbacks.post_order_func_nb,
        post_order_args=callbacks.post_order_args,
        post_segment_func_nb=callbacks.post_segment_func_nb,
        post_segment_args=callbacks.post_segment_args,
        init_cash=ledger_input.initial_cash,
        group_by=ledger_input.group_ids,
        cash_sharing=ledger_input.cash_sharing,
        call_pre_segment=True,
        update_value=True,
        ffill_val_price=True,
        max_orders=rows * columns,
        freq=ledger_input.frequency,
        max_logs=0,
        use_numba=True,
    )
    return ExecutionRun(ledger=ExecutionLedger(portfolio), trace=_freeze_trace(program.trace))
```

`_specialize_program()` 按 callback（回调）函数身份缓存 `_SpecializedCallbacks`，只把 Numba 可接受的数组/标量 tuple（元组）传给 vectorbt，不能把 Python dataclass（数据类）塞进已编译回调。实际参数位置必须以 vectorbt 1.1.0 已安装签名和现有 `vectorbt_engine.py::_run_immediate()` 为准；不能用兼容捕获隐藏签名错误。

- [ ] **Step 4: 实现惰性只读 ExecutionLedger**

ledger 私有保存 Portfolio（组合）并为七个公开属性各设单独 cache。首次访问若结果是独占连续 `ndarray` 只设置 `writeable=False`；临时或非连续视图只复制一次。测试必须同时记录 accessor 次数和 `np.shares_memory()`，防止无意深复制。

- [ ] **Step 5: 证明即时和后续程序都经过同一 runtime**

用两个通用 fixture `OrderProgram` 验证 primary（主运行）与 follow-up（后续运行）都调用同一个 `run_vectorbt()`，第二个运行的 ledger 可作为 final ledger。runtime 不解释冻结计划、执行日可交易性或策略原因码；这些海龟私有语义在 Task 7 组装为 follow-up program。runtime 不得调用 `from_orders()`，不得在 Python 循环中推进现金、仓位或净值。

- [ ] **Step 6: 运行共享 runtime 与旧路径回归**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_vectorbt_runtime.py tests\local_quant_research\test_turtle_vectorbt_engine.py tests\local_quant_research\test_turtle_vectorbt_delayed.py tests\local_quant_research\test_turtle_vectorbt_callbacks.py -q
```

Expected: PASS；共享 fixture 覆盖即时与后续程序，每个 ledger 视图最多计算一次；尚未切换的旧海龟即时/延迟回归保持通过。

- [ ] **Step 7: 提交唯一账本 runtime**

```powershell
git add -- scripts/research/local_quant_research/vectorbt_runtime.py tests/local_quant_research/test_vectorbt_runtime.py
git diff --cached --name-only
git commit -m "重构：以vectorbt统一即时与延迟账本"
```

---

### Task 7: 收敛海龟为单一公开 Strategy Module

**Files:**
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/__init__.py`
- Create: `joinquant/strategies/strategy-003/research/turtle_etf/strategy.py`
- Create: `joinquant/strategies/strategy-003/research/turtle_etf/_kernel.py`
- Create: `joinquant/strategies/strategy-003/research/turtle_etf/_attribution.py`
- Create: `joinquant/strategies/strategy-003/research/turtle_etf/_delayed.py`
- Modify: `tests/local_quant_research/test_turtle_indicators.py`
- Modify: `tests/local_quant_research/test_turtle_vectorbt_inputs.py`
- Modify: `tests/local_quant_research/test_turtle_vectorbt_engine.py`
- Modify: `tests/local_quant_research/test_turtle_vectorbt_delayed.py`
- Modify: `tests/local_quant_research/test_turtle_vectorbt_callbacks.py`
- Modify: `tests/local_quant_research/test_turtle_result_adapter.py`
- Create: `tests/local_quant_research/test_turtle_strategy_module.py`

**Interfaces:**
- Consumes: 共享 `StrategyDescriptor/PreparedStrategy/OrderProgram/ExecutionBundle/ResultExtension` 与现有海龟 baseline（基线）语义。
- Produces: `turtle_etf.strategy:MODULE` 唯一公开对象；私有 `_kernel/_attribution/_delayed` 不被共享层或外部测试直接导入。

- [ ] **Step 1: 写唯一公开入口与导入边界失败测试**

测试 descriptor source files（描述符源码文件）、三个方法、扩展名称、策略根边界和禁止导入：

```python
def test_turtle_package_has_one_public_strategy_symbol() -> None:
    from turtle_etf.strategy import MODULE
    assert MODULE.descriptor.strategy_id == "strategy-003"
    assert MODULE.descriptor.extension_names == ("turtle_etf",)
    assert set(turtle_etf.__all__) == {"MODULE"}

def test_strategy_sources_do_not_import_vectorbt(repo_root: Path) -> None:
    root = repo_root / "joinquant/strategies/strategy-003/research/turtle_etf"
    for name in ("strategy.py", "_kernel.py", "_attribution.py", "_delayed.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imports = {node.names[0].name for node in ast.walk(tree) if isinstance(node, ast.Import)}
        imports |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        assert all(not item.startswith("vectorbt") for item in imports)
```

- [ ] **Step 2: 运行策略模块测试并确认公开 MODULE 尚不存在**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_strategy_module.py tests\local_quant_research\test_turtle_vectorbt_inputs.py tests\local_quant_research\test_turtle_result_adapter.py -q
```

Expected: FAIL，`turtle_etf.strategy` 缺失或测试仍依赖旧公开内部模块。

- [ ] **Step 3: 迁移输入、参数与订单内核到私有实现**

把 `vectorbt_inputs.py` 的行情准备与 `vectorbt_callbacks.py` 的 Numba 内核按责任迁到 `_kernel.py`；动作/原因码、单位、共同止损、4/6/12 风险缩放和全量再分配保持策略私有。`prepare()` 必须先验证纯配置，再读取 snapshot（快照）和分配矩阵；缺少声明证据抛 `StrategyEvidenceError(code, message)`，算法错误保留为失败。即时程序与 follow-up（后续）延迟程序都只构造共享 `OrderProgram`，账户变化统一交给 Task 6 的 `run_vectorbt()`。

`OrderBuffer` 只在 prepare 阶段预分配一次；每个 segment（分段）原地清零并写入，不创建 vectorbt Order（订单），不保存 vectorbt context（上下文）。

- [ ] **Step 4: 实现唯一 MODULE 组合对象**

公开模块只组合私有函数与共享 contract：

```python
@dataclass(frozen=True, slots=True)
class TurtleStrategyModule:
    descriptor: StrategyDescriptor

    def prepare(self, snapshot: SnapshotView, config: Mapping[str, object]) -> PreparedStrategy:
        return prepare_turtle_strategy(snapshot, config)

    def followup_program(self, prepared: PreparedStrategy, primary_run: ExecutionRun) -> OrderProgram | None:
        return build_delayed_program(prepared, primary_run)

    def build_extensions(self, prepared: PreparedStrategy, execution: ExecutionBundle) -> tuple[ResultExtension, ...]:
        return (build_turtle_attribution(prepared, execution),)

MODULE = TurtleStrategyModule(
    descriptor=StrategyDescriptor(
        strategy_id="strategy-003",
        contract_version="strategy-module/1",
        source_files=(Path("turtle_etf/__init__.py"), Path("turtle_etf/strategy.py"), Path("turtle_etf/_kernel.py"), Path("turtle_etf/_attribution.py"), Path("turtle_etf/_delayed.py")),
        extension_names=("turtle_etf",),
        accounting={"lot_size": 100, "cash_sharing": True},
    )
)
```

`__init__.py` 只 re-export（重新导出）`MODULE`。source files 顺序固定并同时驱动 code identity（代码身份）与档案 `code/`。

- [ ] **Step 5: 迁移归因和延迟计划到私有模块**

把 `result_adapter.py` 中海龟专属 attribution（归因）表构造移到 `_attribution.py`，只返回 `ResultExtension`；路径、压缩、清单和 Parquet 写入留在共享 writer。把冻结计划与执行日规则移到 `_delayed.py`，只返回后续 `OrderProgram`；不得维护现金、持仓、费用或净值数组。

- [ ] **Step 6: 把测试改到公开 contract 和结果扩展**

指标纯函数可通过 `MODULE.prepare()` 的 trace 或私有单元测试 fixture 验证；业务 E2E 只能导入 `MODULE`。删除测试对 `VectorbtSimulationResult`、`DelayedExecutionResult` 和 `LocalResultPackage` 等旧生产类型的依赖，等价性 fixture 继续作为行为裁判。

- [ ] **Step 7: 运行海龟模块与等价性测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_strategy_module.py tests\local_quant_research\test_turtle_indicators.py tests\local_quant_research\test_turtle_vectorbt_inputs.py tests\local_quant_research\test_turtle_vectorbt_engine.py tests\local_quant_research\test_turtle_vectorbt_delayed.py tests\local_quant_research\test_turtle_vectorbt_callbacks.py tests\local_quant_research\test_turtle_result_adapter.py tests\local_quant_research\test_local_research_equivalence.py -q
```

Expected: PASS；公开 API（接口）只有 `MODULE`，共享目录不导入海龟，策略文件不导入 vectorbt。

- [ ] **Step 8: 提交策略模块**

```powershell
git add -- joinquant/strategies/strategy-003/research/turtle_etf/__init__.py joinquant/strategies/strategy-003/research/turtle_etf/strategy.py joinquant/strategies/strategy-003/research/turtle_etf/_kernel.py joinquant/strategies/strategy-003/research/turtle_etf/_attribution.py joinquant/strategies/strategy-003/research/turtle_etf/_delayed.py tests/local_quant_research/test_turtle_strategy_module.py tests/local_quant_research/test_turtle_indicators.py tests/local_quant_research/test_turtle_vectorbt_inputs.py tests/local_quant_research/test_turtle_vectorbt_engine.py tests/local_quant_research/test_turtle_vectorbt_delayed.py tests/local_quant_research/test_turtle_vectorbt_callbacks.py tests/local_quant_research/test_turtle_result_adapter.py
git diff --cached --name-only
git commit -m "重构：收敛海龟策略公开模块"
```

---

### Task 8: 单次切换生产配置并完成 run → package → promote E2E

**Files:**
- Modify: `joinquant/strategies/strategy-003/research/project-run.json`
- Delete: `joinquant/strategies/strategy-003/research/code-identity.json`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_inputs.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_callbacks.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_engine.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_delayed.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/result_adapter.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/single_scenario.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_benchmark.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/vectorbt_cli.py`
- Delete: `joinquant/strategies/strategy-003/research/turtle_etf/indicators.py`
- Modify: `tests/local_quant_research/test_contract_fixtures.py`
- Modify: `tests/local_quant_research/test_turtle_e2e.py`
- Create: `tests/local_quant_research/test_local_research_v2_e2e.py`
- Modify: `.build-and-verify/config.json`
- Modify: `docs/research/2026-07-13-turtle-etf-system-final-plan.md`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/specs/local-quant-research-workflow/spec.md`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/specs/standard-strategy-analysis-data/spec.md`

**Interfaces:**
- Consumes: 完成的共享 runtime、结果包、archive、海龟 `MODULE` 和项目 `baseline.json`。
- Produces: 唯一生产命令 `cli.py run --config .../project-run.json` 与 `cli.py promote ...`；仓库中不存在旧策略入口或第二套生产账本。

- [ ] **Step 1: 先把配置、源码扫描和公开 E2E 改成最终契约**

`project-run.json` 的精确形状改为：

```json
{
  "schema_version": 2,
  "project_id": "strategy-003",
  "strategy": {
    "root": "joinquant/strategies/strategy-003/research",
    "module": "turtle_etf.strategy",
    "symbol": "MODULE"
  },
  "snapshot_id": "e88238cca420a8ae66b90adb6cda4dd6c38a07390a13b8ac2f471e534742e33e",
  "snapshot_requirements": {},
  "scenario_config": "joinquant/strategies/strategy-003/research/baseline.json",
  "declared_inputs": ["joinquant/strategies/strategy-003/manifest.json"]
}
```

实际提交时保留旧文件中完整 `snapshot_requirements`，不得缩减为示例空对象。源码扫描测试断言：共享目录只有 `vectorbt_runtime.py` 可出现 vectorbt import；旧九个生产文件和 `code-identity.json` 均不存在；生产配置中没有旧六字段。

- [ ] **Step 2: 运行最终配置和 E2E 测试并确认旧生产路径仍存在**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_contract_fixtures.py tests\local_quant_research\test_turtle_e2e.py tests\local_quant_research\test_local_research_v2_e2e.py -q
```

Expected: FAIL，配置仍为 v1、旧文件仍存在或公开入口尚未完成 `promote`。

- [ ] **Step 3: 在一个提交内切换配置并物理删除旧生产入口**

先更新配置 v2，再删除列出的九个旧策略文件和手工 `code-identity.json`。不得留 re-export（重新导出）兼容模块、feature flag（功能开关）、旧命令分支或双写路径；测试迁移已在 Task 7 完成，因此删除后应无生产/测试导入残留。

- [ ] **Step 4: 实现完整用户入口 E2E**

新 E2E 从 `.venv` 子进程执行共享 `run`，验证 `.local/quant-research/strategy-003/<run_id>/` 的 `manifest/code/config/data/extensions/evidence/report`、四张核心表、海龟扩展、runtime lock（运行时锁定）、性能证据与机械报告；随后执行公开 `promote`，验证策略档案逐字节一致。

同一入口还要分别验证：相同运行复用、证据不足、执行失败、摘要冲突、性能超限、相同晋升复用、`analysis_id` 冲突、中途失败清理，以及删除 `.local` 后档案仍可查询。测试产生的 `.local/e2e-tests` 与策略 `archives/<test-id>` 必须在 `finally` 清理。

- [ ] **Step 5: 同步 Skill、研究说明、旧规格与 Build and Verify**

文档明确本地 archive（档案）位于 `research/archives/`，语义上不是聚宽正式 `backtests/` 或 `simulations/`；机械执行报告不承担分析、推荐或人工批准。`.build-and-verify/config.json` 将所有新共享模块、策略私有文件、配置、Skill 与 E2E 测试纳入同一 local quant research（本地量化研究）检查，使用项目 `.venv`。

- [ ] **Step 6: 运行公开 E2E 和源码边界检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_contract_fixtures.py tests\local_quant_research\test_strategy_contract.py tests\local_quant_research\test_turtle_e2e.py tests\local_quant_research\test_local_research_v2_e2e.py tests\local_quant_research\test_skill_contract.py -q
rg -n "import vectorbt|from vectorbt" scripts joinquant/strategies/strategy-003/research/turtle_etf
```

Expected: 全部 PASS；`rg` 的唯一命中是 `scripts/research/local_quant_research/vectorbt_runtime.py`；旧九个文件和 `code-identity.json` 均不存在。

- [ ] **Step 7: 提交单次生产切换**

```powershell
git add -A -- joinquant/strategies/strategy-003/research scripts/research/local_quant_research tests/local_quant_research .agents/skills/run-local-quant-research .build-and-verify/config.json docs/research/2026-07-13-turtle-etf-system-final-plan.md openspec/changes/build-turtle-etf-local-research-workflow/specs/local-quant-research-workflow/spec.md openspec/changes/build-turtle-etf-local-research-workflow/specs/standard-strategy-analysis-data/spec.md
git diff --cached --name-only
git commit -m "重构：切换本地研究三层生产架构"
```

---

### Task 9: 建立发布性能门禁并完成仓库级验证

**Files:**
- Modify: `scripts/research/local_quant_research/performance.py`
- Create: `tests/local_quant_research/test_performance_gate_v2.py`
- Modify: `tests/local_quant_research/test_turtle_vectorbt_performance.py`
- Modify: `openspec/changes/refactor-local-research-three-layer-architecture/tasks.md`
- Create: `docs/superpowers/reports/2026-07-17-local-research-three-layer-architecture-verify.md`

**Interfaces:**
- Consumes: Task 1 的行为/性能 baseline（基线）、共享 CLI、三个固定场景和完成结果包。
- Produces: `performance release --baseline <path>` 发布验证命令、Windows 峰值内存证据、零差异/5%/180 秒报告与完整 Build and Verify（构建与验证）结果。

- [ ] **Step 1: 写 3/5 采样、ctypes、5% 与绝对门禁失败测试**

测试注入子进程指标，断言冷启动取 3 个全新 PID（进程号）的中位数、同一进程 5 次 warm（热）中位数；时间、峰值内存或包体积任一 `actual > baseline * 1.05` 均失败，任一单次超过 180 秒也失败：

```python
@pytest.mark.parametrize("metric", ("seconds", "peak_working_set_bytes", "package_bytes"))
def test_release_gate_rejects_more_than_five_percent(metric: str) -> None:
    baseline = {"seconds": 100.0, "peak_working_set_bytes": 1000, "package_bytes": 2000}
    actual = dict(baseline)
    actual[metric] = baseline[metric] * 1.050001
    with pytest.raises(PerformanceGateError, match=metric):
        compare_release_metrics(actual, baseline, maximum_ratio=1.05)

def test_sampling_contract_is_three_cold_processes_and_five_warm_runs() -> None:
    result = collect_release_samples(FakeLauncher())
    assert len({sample.pid for sample in result.cold}) == 3
    assert len(result.warm) == 5
    assert len({sample.pid for sample in result.warm}) == 1
```

- [ ] **Step 2: 运行性能测试并确认发布命令与内存采集缺失**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_performance_gate_v2.py tests\local_quant_research\test_turtle_vectorbt_performance.py -q
```

Expected: FAIL，尚无发布采样器或 Windows peak working set（峰值工作集）采集。

- [ ] **Step 3: 实现 Windows 标准库峰值内存采集**

在 `performance.py` 用 `ctypes.WinDLL("psapi")` 和 `GetProcessMemoryInfo` 轮询父进程启动的子进程；结构体固定包含 `PeakWorkingSetSize`，句柄使用 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ)` 获取并在 `finally` 调用 `CloseHandle`。非 Windows CI（持续集成）只运行 180 秒绝对门禁，不伪造 5% 相对数据。

- [ ] **Step 4: 实现独立 release performance 命令**

CLI 增加仅发布验证使用的 `performance release --baseline tests/local_quant_research/fixtures/performance-baseline.json`；依次运行 3,432 × 11、3,432 × 17、延迟 1 日三个场景。每个场景先做完整行为摘要零差异，再比较总阶段时间、峰值 working set 和同逻辑包字节数；日常 `run` 代码不得调用此 3/5 采样器。

- [ ] **Step 5: 运行单元、模块和完整用户入口回归**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research -q
.\.venv\Scripts\python.exe scripts\research\local_quant_research\cli.py run --config joinquant\strategies\strategy-003\research\project-run.json
```

Expected: 测试全部 PASS；真实入口返回 `status=complete` 或在声明行情不可用时返回有明确 reason code 的 `evidence_insufficient`，不能因旧入口、旧 identity（身份）或旧输出路径失败。

- [ ] **Step 6: 在固定 Windows 机器运行发布相对门禁**

Run:

```powershell
.\.venv\Scripts\python.exe scripts\research\local_quant_research\cli.py performance release --baseline tests\local_quant_research\fixtures\performance-baseline.json
```

Expected: 三个场景行为摘要零差异；每个冷/热单次小于 180 秒；三个场景的中位时间、峰值进程内存和同逻辑包体积均不超过各自基线 1.05 倍。若机器环境与 baseline environment 不一致，命令必须拒绝比较并报告 `environment_mismatch`，不得覆盖 baseline。

- [ ] **Step 7: 运行完整 Build and Verify 与残留扫描**

先读取仓库 `build-and-verify` Skill（技能），再执行 full（完整）验证；同时扫描旧生产入口、第二套账本和临时产物：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
rg -n "Portfolio\.from_orders|run_delayed_execution|vectorbt_cli|single_scenario|vectorbt_benchmark|code-identity\.json" scripts joinquant tests .agents
git status --short
```

Expected: 完整测试 PASS；残留扫描只允许历史文档/迁移说明的明确引用，不得有生产或测试导入；工作区没有 `.tmp`、测试 archive（档案）或新 `.local` 临时目录。随后按 `build-and-verify` Skill 执行仓库 full（完整）命令并保存实际命令、退出码和耗时。

- [ ] **Step 8: 写验证报告并逐项勾选 OpenSpec 任务**

验证报告必须记录：三份 capability（能力规格）的逐项覆盖、所有测试命令与结果、三个场景的零差异摘要、3/5 性能样本、5%/180 秒门禁、峰值内存来源、结果包体积、旧路径残留扫描、无法验证项。只在对应证据真实存在后把 `tasks.md` 的 33 项逐项改为 `[x]`。

- [ ] **Step 9: 提交性能与验证证据**

```powershell
git add -- scripts/research/local_quant_research/performance.py tests/local_quant_research/test_performance_gate_v2.py tests/local_quant_research/test_turtle_vectorbt_performance.py openspec/changes/refactor-local-research-three-layer-architecture/tasks.md docs/superpowers/reports/2026-07-17-local-research-three-layer-architecture-verify.md
git diff --cached --name-only
git commit -m "验证：完成本地研究三层架构门禁"
```

---

## Final Acceptance Checklist

- [ ] `scripts/research/local_quant_research/vectorbt_runtime.py` 是唯一 vectorbt 导入点，且即时/延迟都使用 `Portfolio.from_order_func()`。
- [ ] 海龟策略公开面只有 `turtle_etf.strategy:MODULE`，共享层无需修改即可运行最小策略与海龟策略。
- [ ] 配置 v2 不再声明 `command/project_entry/code_identity/required_outputs/output_root/stop_states`，代码身份由 descriptor 和共享 runtime 自动生成。
- [ ] 每个 `.local/<strategy>/<run_id>` 包含 manifest、code、config、data、extensions、evidence、report，并能原样晋升到策略目录。
- [ ] promotion（晋升）幂等、冲突安全、失败清理、原子发布，删除 `.local` 后档案仍可独立查询且不复制共享行情。
- [ ] 机械执行报告只陈述可复核事实，没有推荐、稳健性或实盘批准判断。
- [ ] 三个固定场景结果零差异，固定机器时间/峰值内存/包体积不超过 5%，冷/热单次均小于 180 秒。
- [ ] 公开 `run → package → promote` E2E（端到端）与完整 Build and Verify（构建与验证）通过，没有旧生产入口、双账本或临时产物。
