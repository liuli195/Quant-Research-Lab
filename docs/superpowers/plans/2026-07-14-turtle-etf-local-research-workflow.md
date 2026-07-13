---
change: build-turtle-etf-local-research-workflow
design-doc: docs/superpowers/specs/2026-07-14-turtle-etf-local-research-workflow-design.md
base-ref: a61a53b6852b8dd8ad111693145e40d5555e99d8
---

# 海龟 ETF 本地研究流程实施计划

> **给执行 Agent（代理）：** 必须按任务逐项使用 `superpowers:subagent-driven-development`（子代理驱动开发，推荐）或 `superpowers:executing-plans`（计划执行）实施；所有代码任务遵循 TDD（测试驱动开发）的 RED（失败）→ GREEN（通过）→ REFACTOR（重构）循环。

**目标：** 建立与具体策略解耦的本地日线行情中心和研究运行器，并为 `strategy-003` 实现可复算的海龟 ETF 探索性研究、报告、结论和固定候选包。

**架构：** `.agents/skills/run-local-quant-research/` 只编排；`scripts/research/market_data/` 管理不可变 CSV（逗号分隔文件）批次、快照和内存 DuckDB（嵌入式分析数据库）视图；`scripts/research/local_quant_research/` 负责配置、运行身份、项目子进程和原子证据；`joinquant/strategies/strategy-003/research/` 只保存海龟项目配置、纯计算模块和报告逻辑。正式回测不在本计划执行。

**技术栈：** Python 3.12、pytest（测试框架）、DuckDB 1.5.4、Pandas 3.0.2、NumPy 2.4.4、JSON（结构化清单）、CSV、PowerShell、JoinQuant（聚宽）研究环境。

## 全局约束

- 所有本地 Python（编程语言）命令必须使用 `.\.venv\Scripts\python.exe`，不得回退系统 Python 或静默安装依赖。
- 完整行情只能写入已忽略的 `.local/market-data/`；不得提交行情值、账号、Token（访问令牌）或 Cookie（浏览器凭证）。
- `market-data.csv` 是唯一行情事实源；DuckDB 只能使用 `duckdb.connect(':memory:')`，不得生成持久 `.duckdb` 文件。
- 首版只实现 `source=joinquant`、`asset_type=etf`、`frequency=1d`，但共享模块不得包含海龟参数、ETF 资产池或交易规则。
- 运行状态且只能是 `complete`、`evidence_insufficient`、`failed`；项目建议使用独立枚举，不得混用。
- 本地研究只生成探索性证据，不能宣称正式回测通过、稳健性通过或实盘准入。
- 不修改 `strategy-001`、`strategy-002`；`strategy-003` 必须先绑定真实聚宽策略对象，且本计划不启动正式回测。
- 每个任务完成后运行该任务的定向测试，勾选对应 OpenSpec（开放规格）任务并用简体中文提交说明提交。

## 文件结构

```text
.agents/skills/run-local-quant-research/
├── SKILL.md
└── agents/openai.yaml
.claude/skills/run-local-quant-research -> ../../.agents/skills/run-local-quant-research
scripts/research/
├── market_data/
│   ├── __init__.py
│   ├── contracts.py
│   ├── storage.py
│   ├── query.py
│   └── joinquant_export.py
└── local_quant_research/
    ├── __init__.py
    ├── contracts.py
    ├── evidence.py
    ├── runner.py
    └── cli.py
joinquant/strategies/strategy-003/
├── default_code.py
├── manifest.json
└── research/
    ├── project-run.json
    ├── baseline.json
    ├── candidates.json
    └── turtle_etf/
        ├── __init__.py
        ├── indicators.py
        ├── signals.py
        ├── state.py
        ├── risk.py
        ├── allocation.py
        ├── execution.py
        ├── reporting.py
        └── cli.py
tests/local_quant_research/
├── test_skill_contract.py
├── test_market_data_storage.py
├── test_market_data_query.py
├── test_joinquant_export.py
├── test_runner.py
├── test_evidence.py
├── test_turtle_indicators.py
├── test_turtle_risk.py
├── test_turtle_allocation.py
├── test_turtle_e2e.py
├── test_generic_e2e.py
└── fixtures/daily-bars.csv
```

## OpenSpec 覆盖映射

| 计划任务 | 覆盖 OpenSpec 子项 |
|---|---|
| 任务 1 | 1.1、1.2、1.3 |
| 任务 2 | 2.1、2.2 |
| 任务 3 | 3.1、3.2、3.3 |
| 任务 4 | 3.4、3.5 |
| 任务 5 | 2.3、2.4、2.5、2.6 |
| 任务 6 | 4.1、4.2、4.3、4.4 |
| 任务 7 | 4.5、4.6 |
| 任务 8 | 5.1、5.2、5.3 |
| 任务 9 | 6.1、6.2、6.3、6.4、7.1 |
| 任务 10 | 7.2、7.3 |

### 任务 1：建立真实 `strategy-003` 身份和冻结契约夹具

**文件：**

- 创建：`joinquant/strategies/strategy-003/default_code.py`
- 创建：`joinquant/strategies/strategy-003/manifest.json`
- 修改：`joinquant/strategies/strategy_index.csv`
- 创建：`joinquant/strategies/strategy-003/research/baseline.json`
- 创建：`joinquant/strategies/strategy-003/research/candidates.json`
- 创建：`tests/local_quant_research/test_strategy_identity.py`
- 创建：`tests/local_quant_research/test_contract_fixtures.py`

**接口：**

- 产出：真实聚宽详情 URL、远端名称、`default_code.py` SHA256（文件摘要）和本地 `strategy-003` 唯一映射。
- 产出：`baseline.json` 固定资产池、资产组、55/20 通道、20 日 N、0.5N 加仓、2N 止损、风险和价格口径。
- 产出：`candidates.json` 恰好为冻结基线加六个单参数挑战。

- [x] **步骤 1：先写身份与冻结契约失败测试**

```python
def test_strategy_003_is_real_and_unique(repo_root):
    rows = list(csv.DictReader((repo_root / "joinquant/strategies/strategy_index.csv").open(encoding="utf-8")))
    row = next(item for item in rows if item["strategy_id"] == "strategy-003")
    assert row["joinquant_strategy_url"].startswith("https://www.joinquant.com/algorithm/index/edit?")
    assert (repo_root / row["current_default_code"]).is_file()

def test_candidates_are_frozen_single_factor_challenges(repo_root):
    document = json.loads((repo_root / "joinquant/strategies/strategy-003/research/candidates.json").read_text(encoding="utf-8"))
    items = document["candidates"]
    assert [item["id"] for item in items] == [
        "baseline", "entry-40", "entry-60", "stop-1.5n", "stop-2.5n",
        "covariance-120d", "covariance-ewma-30d",
    ]
    assert all(len(item["overrides"]) <= 1 for item in items)
```

- [x] **步骤 2：运行测试并确认因 `strategy-003` 尚不存在而失败**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_strategy_identity.py tests\local_quant_research\test_contract_fixtures.py -q`

预期：FAIL（失败），明确指出缺少 `strategy-003` 行或配置文件。

- [x] **步骤 3：通过已登录的聚宽页面创建真实策略空壳并同步身份**

使用 Chrome（浏览器）现有登录状态创建名为 `turtle_etf_local_research` 的策略，仅保存最小 `initialize(context): pass` 代码，不创建回测。记录详情 URL，按现有 manifest（清单）结构写入远端身份、观察时间、代码路径和摘要；不得读取或保存 Cookie。

- [x] **步骤 4：写入精确冻结配置**

`baseline.json` 必须包含 11 个聚宽代码（`.XSHG`/`.XSHE`）、六个固定资产组、`entry_days=55`、`exit_days=20`、`n_days=20`、`risk_per_unit=0.005`、`add_step_n=0.5`、`stop_n=2.0`、`covariance_days=60`、`target_volatility=0.10`、`fq=null`、`use_real_price=false`。`candidates.json` 只覆盖对应单一字段。

- [x] **步骤 5：重跑定向测试并保护既有策略**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_strategy_identity.py tests\local_quant_research\test_contract_fixtures.py -q`

预期：PASS（通过）；另运行 `git diff -- joinquant/strategies/strategy-001 joinquant/strategies/strategy-002`，预期无输出。

- [x] **步骤 6：提交任务 1**

```powershell
git add joinquant/strategies/strategy-003 joinquant/strategies/strategy_index.csv tests/local_quant_research/test_strategy_identity.py tests/local_quant_research/test_contract_fixtures.py
git commit -m "功能：建立海龟ETF研究项目身份与冻结契约"
```

### 任务 2：初始化薄 Skill（技能）并锁定公开入口

**文件：**

- 创建：`.agents/skills/run-local-quant-research/SKILL.md`
- 创建：`.agents/skills/run-local-quant-research/agents/openai.yaml`
- 创建：`.claude/skills/run-local-quant-research`（目录链接）
- 修改：`tests/test_skill_layout.py`
- 创建：`tests/local_quant_research/test_skill_contract.py`

**接口：**

- 产出：唯一公开命令 `.\.venv\Scripts\python.exe scripts\research\local_quant_research\cli.py run --config <path>`。
- 约束：Skill 只描述顺序、输入、三态、正式回测边界和凭证边界，不包含海龟参数。

- [x] **步骤 1：添加失败的布局和内容测试**

```python
def test_local_research_skill_is_thin(repo_root):
    skill = repo_root / ".agents/skills/run-local-quant-research/SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert "scripts/research/local_quant_research/cli.py" in text
    assert "complete" in text and "evidence_insufficient" in text and "failed" in text
    for forbidden in ("55日", "0.5N", "strategy-003", "510300"):
        assert forbidden not in text
```

- [x] **步骤 2：运行测试并确认缺少 Skill**

运行：`.\.venv\Scripts\python.exe -m pytest tests\test_skill_layout.py tests\local_quant_research\test_skill_contract.py -q`

预期：FAIL，缺少 `run-local-quant-research`。

- [x] **步骤 3：用官方生成器初始化 Skill**

运行：`.\.venv\Scripts\python.exe C:\Users\liuli\.codex\skills\.system\skill-creator\scripts\init_skill.py run-local-quant-research --path .agents\skills`

编辑 `SKILL.md` 和 `agents/openai.yaml` 后，在仓库根目录运行：

```powershell
New-Item -ItemType SymbolicLink -Path '.claude\skills\run-local-quant-research' -Target '..\..\.agents\skills\run-local-quant-research'
```

随后用 `Get-Item '.claude\skills\run-local-quant-research' | Format-List LinkType,Target` 确认 `LinkType=SymbolicLink` 且目标为 `..\..\.agents\skills\run-local-quant-research`。

- [x] **步骤 4：运行结构验证和定向测试**

运行：`.\.venv\Scripts\python.exe C:\Users\liuli\.codex\skills\.system\skill-creator\scripts\quick_validate.py .agents\skills\run-local-quant-research`

运行：`.\.venv\Scripts\python.exe -m pytest tests\test_skill_layout.py tests\local_quant_research\test_skill_contract.py -q`

预期：全部 PASS。

- [x] **步骤 5：提交任务 2**

```powershell
git add .agents/skills/run-local-quant-research .claude/skills/run-local-quant-research tests/test_skill_layout.py tests/local_quant_research/test_skill_contract.py
git commit -m "功能：增加通用本地量化研究技能入口"
```

### 任务 3：实现不可变行情批次与快照

**文件：**

- 创建：`scripts/research/market_data/__init__.py`
- 创建：`scripts/research/market_data/contracts.py`
- 创建：`scripts/research/market_data/storage.py`
- 创建：`tests/local_quant_research/test_market_data_storage.py`
- 创建：`tests/local_quant_research/fixtures/daily-bars.csv`

**接口：**

```python
def import_batch(*, csv_path: Path, manifest: Mapping[str, object], root: Path) -> BatchRecord: ...
def create_snapshot(*, batch_ids: Sequence[str], selection: SnapshotSelection, root: Path) -> SnapshotRecord: ...
def validate_snapshot(snapshot_id: str, *, root: Path) -> SnapshotRecord: ...
```

- [x] **步骤 1：为批次身份、去重和冲突写失败测试**

测试必须断言：批次目录恰好包含 `manifest.json`、`market-data.csv`、`validation.json`；相同来源和字节摘要复用同一 `batch_id`；重叠键值不同抛出 `MarketDataConflict`；新证券追加不改变旧批次摘要。

- [x] **步骤 2：为快照引用和旧快照稳定性写失败测试**

```python
snapshot = create_snapshot(batch_ids=[first.batch_id], selection=selection, root=tmp_path)
before = sha256((tmp_path / "snapshots" / f"{snapshot.snapshot_id}.json").read_bytes()).hexdigest()
import_batch(csv_path=second_csv, manifest=second_manifest, root=tmp_path)
assert sha256((tmp_path / "snapshots" / f"{snapshot.snapshot_id}.json").read_bytes()).hexdigest() == before
```

- [x] **步骤 3：运行失败测试**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_storage.py -q`

预期：FAIL，模块不存在。

- [x] **步骤 4：实现规范化 JSON、SHA256、原子写入和冲突索引**

`batch_id` 使用来源身份、导出契约和 CSV 字节摘要的规范化 JSON 摘要；`snapshot_id` 使用快照清单规范化 JSON 摘要。写入必须先进入同文件系统临时目录，再以原子替换固化；目标已存在时只允许内容完全相同。

- [x] **步骤 5：验证所有存储不变量**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_storage.py -q`

预期：PASS；测试临时目录结束后不存在 `.tmp`、`.duckdb` 或未被快照引用的伪完成目录。

- [x] **步骤 6：提交任务 3**

```powershell
git add scripts/research/market_data tests/local_quant_research/test_market_data_storage.py tests/local_quant_research/fixtures/daily-bars.csv
git commit -m "功能：实现共享行情批次与快照"
```

### 任务 4：实现内存 DuckDB 查询和聚宽导出契约

**文件：**

- 创建：`scripts/research/market_data/query.py`
- 创建：`scripts/research/market_data/joinquant_export.py`
- 创建：`tests/local_quant_research/test_market_data_query.py`
- 创建：`tests/local_quant_research/test_joinquant_export.py`

**接口：**

```python
def open_snapshot(snapshot_id: str, *, root: Path) -> SnapshotView: ...
def normalized_digest(rows: Iterable[Mapping[str, object]]) -> str: ...
def render_export_program(request: ExportRequest) -> str: ...
def verify_transfer(*, local_file: Path, remote_sha256: str, remote_cleaned: bool) -> TransferEvidence: ...
```

- [x] **步骤 1：写 CSV 与内存视图一致性失败测试**

覆盖固定 13 字段、`date/security` 唯一键、排序、空值、`paused` 数值到布尔规范化、行差异和内容摘要差异；使用 `monkeypatch` 断言连接字符串恰好为 `:memory:`。

- [x] **步骤 2：写聚宽导出程序失败测试**

断言生成程序包含 `get_price(..., fq=None, skip_paused=False)`、13 个字段、`line_terminator='\n'`、远端回读 SHA256 和清理入口；断言没有 `from jqdata import get_price`，没有凭证字面量。

- [x] **步骤 3：运行失败测试**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_query.py tests\local_quant_research\test_joinquant_export.py -q`

预期：FAIL，查询和导出模块尚未实现。

- [x] **步骤 4：实现最小查询层和可渲染导出程序**

查询层从快照引用的一个或多个权威 CSV 建内存视图，返回只读 `SnapshotView`；导出模块只接受 `ExportRequest(securities, fields, snapshot_end_date, fq=None, skip_paused=False)`，不内置资产池或交易规则。

- [x] **步骤 5：验证失败门禁和无持久数据库**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_query.py tests\local_quant_research\test_joinquant_export.py -q`

预期：PASS；摘要不一致或 `remote_cleaned=False` 返回 `failed`；测试树中不存在 `*.duckdb`。

- [x] **步骤 6：提交任务 4**

```powershell
git add scripts/research/market_data/query.py scripts/research/market_data/joinquant_export.py tests/local_quant_research/test_market_data_query.py tests/local_quant_research/test_joinquant_export.py
git commit -m "功能：实现行情查询与聚宽日线导出契约"
```

### 任务 5：实现通用运行器、三态和原子证据

**文件：**

- 创建：`scripts/research/local_quant_research/__init__.py`
- 创建：`scripts/research/local_quant_research/contracts.py`
- 创建：`scripts/research/local_quant_research/evidence.py`
- 创建：`scripts/research/local_quant_research/runner.py`
- 创建：`scripts/research/local_quant_research/cli.py`
- 创建：`tests/local_quant_research/test_runner.py`
- 创建：`tests/local_quant_research/test_evidence.py`

**接口：**

```python
RunStatus = Literal["complete", "evidence_insufficient", "failed"]
def load_run_config(path: Path, *, repo_root: Path) -> RunConfig: ...
def compute_run_id(snapshot_digest: str, config_digest: str, code_digest: str) -> str: ...
def run_project(config_path: Path, *, repo_root: Path) -> RunResult: ...
```

- [x] **步骤 1：写不安全配置和三态失败测试**

覆盖 Shell（命令解释器）字符串、仓库外路径、缺少 `snapshot_id`、缺少必需输出、未知状态、系统 Python、隐式安装和凭证字段。缺失声明输入必须在项目子进程前返回 `evidence_insufficient`。

- [x] **步骤 2：写 `run_id`、幂等和原子固化失败测试**

测试相同输入复用、快照/配置/代码任一变化产生新 ID、相同 ID 输出变化返回 `failed`、失败尝试保留紧凑诊断但不创建完成目录、成功目录恰好位于 `.local/quant-research/<project_id>/<run_id>/`。

- [x] **步骤 3：运行失败测试**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_runner.py tests\local_quant_research\test_evidence.py -q`

预期：FAIL，通用运行器不存在。

- [x] **步骤 4：实现固定运行阶段**

阶段严格为：配置和路径校验 → 快照与 CSV 摘要 → 内存 DuckDB 同源校验 → 同文件系统暂存目录 → `subprocess.run([...], shell=False)` 调用项目适配器 → 输出结构和摘要 → 原子固化唯一状态。

- [x] **步骤 5：验证安全边界和三态矩阵**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_runner.py tests\local_quant_research\test_evidence.py -q`

预期：PASS；测试日志不含环境凭证值，项目适配器不能写出暂存目录。

- [x] **步骤 6：提交任务 5**

```powershell
git add scripts/research/local_quant_research tests/local_quant_research/test_runner.py tests/local_quant_research/test_evidence.py
git commit -m "功能：实现通用研究运行器与不可变证据"
```

### 任务 6：实现海龟指标、状态和风险门禁

**文件：**

- 创建：`joinquant/strategies/strategy-003/research/turtle_etf/__init__.py`
- 创建：`joinquant/strategies/strategy-003/research/turtle_etf/indicators.py`
- 创建：`joinquant/strategies/strategy-003/research/turtle_etf/signals.py`
- 创建：`joinquant/strategies/strategy-003/research/turtle_etf/state.py`
- 创建：`joinquant/strategies/strategy-003/research/turtle_etf/risk.py`
- 创建：`tests/local_quant_research/test_turtle_indicators.py`
- 创建：`tests/local_quant_research/test_turtle_risk.py`

**接口：**

```python
def true_range(frame: pd.DataFrame) -> pd.Series: ...
def turtle_n(frame: pd.DataFrame, days: int = 20) -> pd.Series: ...
def breakout_levels(frame: pd.DataFrame, entry_days: int, exit_days: int) -> pd.DataFrame: ...
def initial_unit(equity: Decimal, n_value: Decimal, risk_fraction: Decimal = Decimal("0.005")) -> int: ...
def evaluate_risk(requests: Sequence[OrderIntent], state: PortfolioState, inputs: RiskInputs) -> RiskDecision: ...
```

- [x] **步骤 1：写指标、信号和次日执行失败测试**

固定夹具必须证明 55/20 通道排除信号当日、TR 使用 `max(high-low, abs(high-pre_close), abs(low-pre_close))`、N 为 20 日均值、收盘确认信号在下一交易日开盘才成为订单。

- [x] **步骤 2：写批次和共同止损失败测试**

覆盖固定信号日 N、0.5N 理论档位、同一 ETF 每日最多一次加仓、实际成交才改变批次、共同止损只上移、保护止损和 20 日退出均生成全仓退出意图。

- [x] **步骤 3：写风险和故障安全失败测试**

覆盖整手、现金、流动性、单 ETF、资产组、计划风险、60 个对齐样本、60 日协方差、10% 目标波动率；持仓价格或风险输入缺失时 `allow_new_risk=False`，但退出和强制减仓仍保留。

- [x] **步骤 4：运行失败测试**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_indicators.py tests\local_quant_research\test_turtle_risk.py -q`

预期：FAIL，海龟纯计算模块尚未实现。

- [x] **步骤 5：实现最小纯计算模块并通过测试**

模块只接收 DataFrame（数据表）和不可变记录对象，不读取全局目录、环境变量或行情中心；计算只使用未复权 `open/high/low/close/pre_close`。

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_indicators.py tests\local_quant_research\test_turtle_risk.py -q`

预期：PASS。

- [x] **步骤 6：提交任务 6**

```powershell
git add joinquant/strategies/strategy-003/research/turtle_etf tests/local_quant_research/test_turtle_indicators.py tests/local_quant_research/test_turtle_risk.py
git commit -m "功能：实现海龟指标状态与风险门禁"
```

### 任务 7：实现执行状态流与 A1 共享预算分配

**文件：**

- 创建：`joinquant/strategies/strategy-003/research/turtle_etf/allocation.py`
- 创建：`joinquant/strategies/strategy-003/research/turtle_etf/execution.py`
- 创建：`tests/local_quant_research/test_turtle_allocation.py`
- 创建：`tests/local_quant_research/test_turtle_e2e.py`

**接口：**

```python
def allocate_a1(candidates: Sequence[BuyRequest], constraints: PortfolioConstraints) -> AllocationResult: ...
def process_day(day: TradingDay, state: PortfolioState, market: DailyMarket) -> DayResult: ...
```

- [x] **步骤 1：写 A1 公平分配失败测试**

测试所有可行候选先按同一完成比例缩放；自身或资产组上限释放的预算可流向其他候选；先向下取整手，再按小数余额降序逐手补分；完全同分按证券代码升序；每补一手重查全部硬门槛。

- [x] **步骤 2：写输入顺序不变量和约束失败测试**

对同一候选集合的全部排列运行 `allocate_a1`，断言分配摘要相同、现金非负、单 ETF/资产组/计划风险/目标波动率均不突破。

- [x] **步骤 3：写每日完整顺序失败测试**

固定场景同时产生退出、强制减仓、新建仓和加仓，断言顺序严格为“全仓退出 → 强制风险减仓 → 同级新建仓/加仓”；同一 ETF 退出取消当日全部买入；停牌、跳空、涨跌停和不可成交不虚构成交。

- [x] **步骤 4：运行失败测试**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_allocation.py tests\local_quant_research\test_turtle_e2e.py -q`

预期：FAIL，分配和执行模块尚未实现。

- [x] **步骤 5：实现最小分配和日状态机并通过测试**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_allocation.py tests\local_quant_research\test_turtle_e2e.py -q`

预期：PASS，重复运行产生相同审计摘要。

- [x] **步骤 6：提交任务 7**

```powershell
git add joinquant/strategies/strategy-003/research/turtle_etf/allocation.py joinquant/strategies/strategy-003/research/turtle_etf/execution.py tests/local_quant_research/test_turtle_allocation.py tests/local_quant_research/test_turtle_e2e.py
git commit -m "功能：实现A1共享预算与每日执行状态流"
```

### 任务 8：实现项目适配器、报告、结论和候选包

**文件：**

- 创建：`joinquant/strategies/strategy-003/research/project-run.json`
- 创建：`joinquant/strategies/strategy-003/research/turtle_etf/reporting.py`
- 创建：`joinquant/strategies/strategy-003/research/turtle_etf/cli.py`
- 修改：`tests/local_quant_research/test_turtle_e2e.py`

**接口：**

```python
def run_research(config_path: Path, snapshot_path: Path, output_dir: Path) -> ProjectResult: ...
def write_outputs(result: ResearchResult, output_dir: Path) -> Mapping[str, str]: ...
```

- [ ] **步骤 1：写三类必需输出失败测试**

断言运行生成 `research-report.md`、`conclusion.json`、`candidate-strategies.json`，以及 `daily-audit.csv`、`trades.csv`、`positions.csv`、`risk.csv`。任一文件缺失、JSON 结构错误或摘要不匹配时项目不得报告 `complete`。

- [ ] **步骤 2：写研究建议和候选包失败测试**

`conclusion.json.recommendation` 只能为 `proceed_to_joinquant`、`revise_and_reassess`、`stop_evidence_insufficient`；候选恰好七项、共用代码摘要与 `snapshot_id`，且没有按收益排名删除候选或新增参数。

- [ ] **步骤 3：写报告内容失败测试**

报告必须列出方法、输入身份、事件/交易、实际仓位分布、现金占比、留现原因、资产组和组合风险使用率、限制、产物摘要，以及“不是正式回测或最终验收结论”。设计期 63.7%/55.7% 代理值不得作为本次运行结果。

- [ ] **步骤 4：运行失败测试**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_e2e.py -q`

预期：FAIL，报告和项目 CLI（命令行接口）尚未实现。

- [ ] **步骤 5：实现报告与项目入口并通过测试**

`project-run.json` 只引用共享 `snapshot_id`，不得复制 CSV。Vibe-Trading（AI 研究助理）组合优化器配置固定为 `enabled=false` 并在报告写明跳过原因；方向性粗筛只消费确定性结果，不反向修改配置。

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_e2e.py -q`

预期：PASS。

- [ ] **步骤 6：提交任务 8**

```powershell
git add joinquant/strategies/strategy-003/research tests/local_quant_research/test_turtle_e2e.py
git commit -m "功能：生成海龟ETF本地研究报告与候选包"
```

### 任务 9：贯通 Skill 用户入口、非海龟 E2E 和验证映射

**文件：**

- 创建：`tests/local_quant_research/test_generic_e2e.py`
- 修改：`tests/local_quant_research/test_skill_contract.py`
- 修改：`tests/local_quant_research/test_turtle_e2e.py`
- 修改：`.build-and-verify/config.json`

**接口：**

- 产出：从 Skill 文档公开命令启动的完整离线 E2E（端到端）。
- 产出：不加载 `strategy-003`、海龟参数或海龟资产的最小项目 E2E。

- [ ] **步骤 1：写 Skill 用户入口失败测试**

测试从 `SKILL.md` 提取公开命令，使用临时 `.local` 根目录和固定日线夹具执行：批次 → 快照 → CSV 校验 → 内存 DuckDB → 通用运行器 → 海龟项目 → 审计/三类输出 → 不可变证据。

- [ ] **步骤 2：写非海龟前向失败测试**

在临时目录生成只输出 `result.json` 的最小项目适配器，环境中不加入 `joinquant/strategies/strategy-003/research`；断言同一行情中心和运行器可返回 `complete`，通用源码中不出现 `turtle`、`55日` 或 11 个 ETF 代码。

- [ ] **步骤 3：运行失败测试**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_generic_e2e.py tests\local_quant_research\test_turtle_e2e.py tests\local_quant_research\test_skill_contract.py -q`

预期：FAIL，公开入口和验证映射尚未贯通。

- [ ] **步骤 4：补齐 CLI 入口与 Build and Verify（构建与验证）检查**

在 `.build-and-verify/config.json` 新增 `verify.local-quant-research-unit` 和 `verify.local-quant-research-e2e`，路径覆盖新 Skill、共享行情脚本、通用运行器、`strategy-003/research` 和测试，但 `inputs` 不得包含 `.local/**`。

- [ ] **步骤 5：运行离线完整回归**

运行：`.\.venv\Scripts\python.exe -m pytest tests\local_quant_research -q`

运行：`.\.venv\Scripts\python.exe -m pytest tests\test_skill_layout.py -q`

预期：全部 PASS；测试结束后临时目录自动清理。

- [ ] **步骤 6：提交任务 9**

```powershell
git add tests/local_quant_research tests/test_skill_layout.py .build-and-verify/config.json scripts/research .agents/skills/run-local-quant-research .claude/skills/run-local-quant-research
git commit -m "测试：贯通本地研究技能端到端流程"
```

### 任务 10：真实导出 11 只 ETF、执行本地研究并完成仓库验证

**文件：**

- 仅本地生成：`.local/market-data/batches/<batch_id>/manifest.json`
- 仅本地生成：`.local/market-data/batches/<batch_id>/market-data.csv`
- 仅本地生成：`.local/market-data/batches/<batch_id>/validation.json`
- 仅本地生成：`.local/market-data/snapshots/<snapshot_id>.json`
- 仅本地生成：`.local/quant-research/strategy-003/<run_id>/...`
- 修改：`openspec/changes/build-turtle-etf-local-research-workflow/tasks.md`

**验收输入：**

- 证券：`510300.XSHG`、`512100.XSHG`、`512480.XSHG`、`159819.XSHE`、`516160.XSHG`、`513100.XSHG`、`513180.XSHG`、`515180.XSHG`、`516080.XSHG`、`518880.XSHG`、`511010.XSHG`。
- 字段：`date, security, open, high, low, close, pre_close, volume, money, factor, paused, high_limit, low_limit`。
- 区间：每只 ETF 自身首个可用完整交易日至显式 `snapshot_end_date`；2015-01-01 前仅作预热；新增风险需 60 个有效对齐样本。

- [ ] **步骤 1：在真实聚宽研究环境执行导出**

使用任务 4 生成的程序，确认内置 `get_price`/`write_file`/`read_file`、`fq=None`、`skip_paused=False`、Pandas 0.23.4 `line_terminator` 和 `paused` 原始类型；远端文件回读字节摘要必须与下载文件一致。

- [ ] **步骤 2：导入共享中心并清理远端中转文件**

先验证 13 字段、唯一键、日期、空值、实际起止日、CSV 字节摘要和规范化内容摘要，再固化批次及快照；删除聚宽端中转文件并复查不存在。若清理无法确认，运行状态必须为 `failed`。

- [ ] **步骤 3：从 Skill 用户入口执行真实本地研究**

运行：`.\.venv\Scripts\python.exe scripts\research\local_quant_research\cli.py run --config joinquant\strategies\strategy-003\research\project-run.json`

预期：输出唯一三态之一；只有 11 只 ETF 快照、运行清单、全部审计和三类必需输出均通过摘要校验时才允许 `complete`。

- [ ] **步骤 4：人工复核本地报告和临时产物**

确认报告给出实际平均/中位仓位、低于 50% 与接近满仓占比、现金占比和留现原因；确认没有把本地结果描述为正式回测；确认聚宽远端中转、本地下载暂存、隐藏 staging（暂存）目录均不存在，已固化批次/快照/完整运行证据保留。

- [ ] **步骤 5：运行完整验证**

运行：`.\.venv\Scripts\python.exe C:\Users\liuli\.codex\skills\.system\skill-creator\scripts\quick_validate.py .agents\skills\run-local-quant-research`

运行：`.\.venv\Scripts\python.exe -m pytest -q`

运行：`openspec validate --all --strict --no-interactive`

在已获授权的 PR Flow hotfix（拉取请求热修复流程）收尾前运行：`.\.venv\Scripts\python.exe .build-and-verify\runtime\build_and_verify.py verify --project . --full`，并确认新检查命中；再运行 `git ls-files | rg "(^|/)\.local/|market-data\.csv$|\.duckdb$"`，预期无输出。

- [ ] **步骤 6：逐项勾选 OpenSpec 任务并提交验证证据**

只有在对应测试、真实集成和清理证据均通过后，才把 `tasks.md` 的 30 项全部改为 `[x]`。无法验证的项保持未勾选并记录精确原因，不得用说明文字代替完成证据。

```powershell
git add openspec/changes/build-turtle-etf-local-research-workflow/tasks.md
git commit -m "验证：完成海龟ETF本地研究流程回归"
```

## 最终完成门禁

- `git diff --check` 无输出。
- `.\.venv\Scripts\python.exe -m pytest -q` 全部通过。
- `openspec validate --all --strict --no-interactive` 全部通过。
- Build and Verify（构建与验证）full（完整）检查通过。
- Skill `quick_validate.py` 通过，`.claude` 目录链接解析正确。
- `strategy-001`、`strategy-002` 无改动；没有正式回测或模拟交易被启动。
- Git（版本管理）跟踪文件不含 `.local`、完整行情、持久 DuckDB、账号、Token 或 Cookie。
- 真实 11 ETF 导出中转与本地暂存已清理；不可变行情批次、快照和完整运行证据保留在 `.local/`。
- `research-report.md`、`conclusion.json`、`candidate-strategies.json` 均绑定同一 `run_id`、`snapshot_id`、代码摘要和配置摘要。
