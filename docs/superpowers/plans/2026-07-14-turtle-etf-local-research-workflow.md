---
change: build-turtle-etf-local-research-workflow
design-doc: docs/superpowers/specs/2026-07-14-turtle-etf-local-research-workflow-design.md
base-ref: 6b3e89f4b53314d17b203c9ad0424169b36d8f5b
---

# 海龟 ETF 完整本地研究流程实施计划

> **给执行 Agent（代理）：** 必须按任务逐项使用 `superpowers:executing-plans`（计划执行）；所有代码任务遵循 TDD（测试驱动开发）的 RED（失败）→ GREEN（通过）→ REFACTOR（重构）循环。

**目标：** 把现有 CSV（逗号分隔文件）版本地研究升级为 Parquet（列式文件）行情中心、完整确定性分析、完整本地稳健性、七方案报告、推荐和人工确认门禁。

**架构：** `run-local-quant-research` Skill（技能）只编排；`scripts/research/market_data/` 管理 Parquet 唯一行情事实源和 DuckDB（嵌入式分析数据库）内存查询；`scripts/research/quant_analysis/` 提供与海龟解耦的确定性分析；`strategy-003/research/turtle_etf/` 复用现有状态、A1（同日共享预算分配）、风险和执行模块运行七方案并形成项目事实。报告阶段只复用当前可用的 Vibe-Trading（AI 研究助理）材料，不新增其职责、适配器或身份模型。

**技术栈：** Python 3.12、pytest（测试框架）、DuckDB 1.5.4、Pandas 3.0.2、PyArrow 23.0.1、NumPy 2.4.4、JSON（结构化清单）、Parquet、PowerShell。

## 全局约束

- 所有本地 Python（编程语言）命令必须使用 `.\.venv\Scripts\python.exe`，不得回退系统 Python 或静默安装依赖。
- 完整行情和研究结果只能写入已忽略的 `.local/`；不得提交行情值、账号、Token（访问令牌）或 Cookie（浏览器凭证）。
- `market-data.parquet` 是本地唯一行情事实源；聚宽 CSV 只作传输暂存，转换和双重摘要校验成功后删除。
- DuckDB 只能连接 `:memory:` 并通过 `read_parquet` 查询；不得产生持久 `.duckdb` 文件。
- Skill 只负责编排，共用能力沉淀为脚本；共用脚本不得导入海龟模块或硬编码海龟资产、参数和规则。
- 现有海龟 `state.py`、`allocation.py`、`risk.py`、`execution.py` 继续作为交易路径事实源，不另建通用回测引擎。
- 冻结基线和六个预设挑战全部运行、全部保留；不得按本地收益自动删除候选、替换基线或新增参数。
- 本变更在完整报告、明确推荐和 `next_action=human_confirmation_required` 处停止；不得启动聚宽正式复核、冻结、模拟交易或实盘。
- 不新增或修改 Vibe-Trading 的职责、适配器、身份模型或上游代码；已知前视偏差组合优化器继续禁用。

---

### Task 1：把共享行情中心迁移为 Parquet 唯一事实源

**对应 OpenSpec：** 8.1、8.2、8.3

**Files：**
- Modify: `scripts/research/market_data/storage.py`
- Modify: `scripts/research/market_data/query.py`
- Modify: `scripts/research/market_data/joinquant_export.py`
- Modify: `scripts/research/market_data/contracts.py`
- Create: `scripts/research/market_data/cli.py`
- Modify: `tests/local_quant_research/test_market_data_storage.py`
- Modify: `tests/local_quant_research/test_market_data_query.py`
- Modify: `tests/local_quant_research/test_joinquant_export.py`

**Interfaces：**
- 保留 `import_batch(csv_path: Path, manifest: Mapping[str, object], root: Path) -> BatchRecord`，但其成功产物改为 `market-data.parquet`。
- 批次清单升级到 `schema_version=2`，保存 `content_sha256`、`parquet.sha256`、`parquet.byte_count`、`transport_csv.sha256`、写入器版本和行数。
- `batch_id` 只由来源身份、结构版本、导出契约和规范化逻辑内容摘要决定；Parquet 字节摘要只验证完整性。
- `open_snapshot(snapshot_id: str, root: Path) -> SnapshotView` 使用 `duckdb.connect(':memory:')` 与 `read_parquet`。
- 旧 `schema_version=1` CSV 批次只保留为历史证据；新快照不得引用旧批次，并返回明确迁移错误。

- [x] **Step 1：写失败测试**

```python
def test_import_batch_publishes_only_parquet_and_content_identity(tmp_path, fixture_csv):
    record = import_batch(csv_path=fixture_csv, manifest=manifest(), root=tmp_path)
    assert (record.path / "market-data.parquet").is_file()
    assert not (record.path / "market-data.csv").exists()
    stored = json.loads((record.path / "manifest.json").read_text("utf-8"))
    assert stored["schema_version"] == 2
    assert stored["content_sha256"]
    assert stored["parquet"]["sha256"]
    assert stored["transport_csv"]["sha256"]
```

同时添加：同一逻辑内容不同 CSV 换行仍复用 `batch_id`、Parquet 篡改拒绝、旧 CSV 快照迁移提示、DuckDB 只使用 `:memory:`、CSV 转换失败不发布批次、成功后本地暂存和远端暂存均清理的测试。

- [x] **Step 2：验证 RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_storage.py tests\local_quant_research\test_market_data_query.py tests\local_quant_research\test_joinquant_export.py -q`

Expected: FAIL，原因是现有批次仍发布 `market-data.csv`，查询仍调用 `read_csv`。

- [x] **Step 3：实现最小 Parquet 导入与查询**

```python
def _write_parquet(rows: Sequence[Mapping[str, object]], target: Path) -> None:
    table = pa.Table.from_pylist(list(rows), schema=MARKET_DATA_ARROW_SCHEMA)
    pq.write_table(table, target, compression="zstd", use_dictionary=False)

def _batch_identity(manifest: Mapping[str, object], content_sha256: str) -> dict[str, object]:
    return {
        "source": manifest["source"],
        "asset_type": manifest["asset_type"],
        "frequency": manifest["frequency"],
        "schema_version": 2,
        "fields": manifest["fields"],
        "price_semantics": manifest["price_semantics"],
        "export_code_sha256": manifest["export_code_sha256"],
        "content_sha256": content_sha256,
    }
```

导入顺序固定为：读取传输 CSV → 规范化类型与排序 → 计算逻辑摘要 → 在同文件系统暂存 Parquet → DuckDB 回读并复核摘要 → 原子发布 → 删除本地传输 CSV；任一步失败都不产生完成批次。远端删除仍由现有聚宽传输接口执行并验证。

- [x] **Step 4：验证 GREEN 和回归**

Run: `.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_market_data_storage.py tests\local_quant_research\test_market_data_query.py tests\local_quant_research\test_joinquant_export.py -q`

Expected: PASS，且测试临时目录不存在 `market-data.csv` 暂存和 `*.duckdb`。

- [x] **Step 5：勾选 OpenSpec 8.1–8.3 并提交**

Commit: `实现：迁移共享行情中心到Parquet`

---

### Task 2：建立八表分析数据包、绩效、基准和归因

**对应 OpenSpec：** 9.1、9.2、9.3、9.4

**Files：**
- Create: `scripts/research/quant_analysis/__init__.py`
- Create: `scripts/research/quant_analysis/contracts.py`
- Create: `scripts/research/quant_analysis/metrics.py`
- Create: `scripts/research/quant_analysis/benchmarks.py`
- Create: `scripts/research/quant_analysis/attribution.py`
- Modify: `scripts/research/local_quant_research/runner.py`
- Modify: `scripts/research/local_quant_research/contracts.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/reporting.py`
- Modify: `joinquant/strategies/strategy-003/research/project-run.json`
- Create: `tests/local_quant_research/test_analysis_contracts.py`
- Create: `tests/local_quant_research/test_quant_metrics.py`
- Create: `tests/local_quant_research/test_quant_attribution.py`

**Interfaces：**

```python
STANDARD_TABLES = (
    "equity", "returns", "trades", "orders",
    "positions", "risk", "events", "benchmarks",
)

def write_analysis_table(name: str, rows: Iterable[Mapping[str, object]], output_dir: Path) -> Path: ...
def validate_analysis_bundle(output_dir: Path) -> AnalysisBundle: ...
def calculate_performance(bundle: AnalysisBundle, annualization: int = 252) -> dict[str, float | int | None]: ...
def calculate_benchmark_statistics(strategy_returns, benchmark_returns) -> dict[str, float | None]: ...
def calculate_attribution(bundle: AnalysisBundle) -> tuple[dict[str, object], ...]: ...
```

沪深 300 人民币总回报与纳斯达克 100 人民币总回报必须作为显式基准输入进入 `benchmarks.parquet`，字段至少为 `date`、`benchmark_id`、`currency`、`total_return_index`、`return`、`source_id`。仓库没有声明来源或日期不完整时返回 `evidence_insufficient`，不得使用 ETF 代理、价格指数或零收益补齐。

- [ ] **Step 1：写八表和黄金指标失败测试**

```python
def test_bundle_requires_all_eight_parquet_tables(tmp_path):
    write_analysis_table("equity", equity_rows(), tmp_path)
    with pytest.raises(AnalysisContractError, match="missing tables"):
        validate_analysis_bundle(tmp_path)

def test_performance_and_two_benchmark_attribution_match_golden_values(golden_bundle):
    bundle = validate_analysis_bundle(golden_bundle)
    metrics = calculate_performance(bundle)
    assert metrics["cumulative_return"] == pytest.approx(0.21)
    assert metrics["max_drawdown"] == pytest.approx(-0.10)
    assert metrics["calmar"] == pytest.approx(metrics["cagr"] / 0.10)
```

补充主键冲突、单位错误、跨表现金/持仓/权益不勾稽、完整往返交易、基准日期错位、Alpha（超额收益）、Beta（市场暴露）、信息比率、上下行捕获率和归因求和容差测试。

- [ ] **Step 2：验证 RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_analysis_contracts.py tests\local_quant_research\test_quant_metrics.py tests\local_quant_research\test_quant_attribution.py -q`

Expected: FAIL，原因是 `quant_analysis` 尚不存在。

- [ ] **Step 3：实现最小确定性分析层并接入项目输出**

所有表以 PyArrow 显式结构写入，保存 `schema_version`、货币、单位、主键和摘要。指标只消费校验后的八表；交易统计只消费闭合往返 `trades.parquet`；归因按 ETF、资产组、时期、交易原因、仓位、现金、趋势过滤和风险约束输出，并检查贡献总和回到组合事实。

- [ ] **Step 4：验证 GREEN 和既有输出回归**

Run: `.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_analysis_contracts.py tests\local_quant_research\test_quant_metrics.py tests\local_quant_research\test_quant_attribution.py tests\local_quant_research\test_turtle_e2e.py tests\local_quant_research\test_runner.py -q`

Expected: PASS；输出格式校验允许 `parquet`，缺任一标准表不得 `complete`。

- [ ] **Step 5：勾选 OpenSpec 9.1–9.4 并提交**

Commit: `实现：增加完整绩效基准与归因分析`

---

### Task 3：实现完整本地稳健性、压力和尾部风险

**对应 OpenSpec：** 10.1

**Files：**
- Create: `scripts/research/quant_analysis/robustness.py`
- Create: `scripts/research/quant_analysis/stress.py`
- Create: `scripts/research/quant_analysis/cvar.py`
- Create: `scripts/research/quant_analysis/evidence.py`
- Create: `tests/local_quant_research/test_quant_robustness.py`
- Create: `tests/local_quant_research/test_quant_stress.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/cli.py`

**Interfaces：**

```python
@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    dimension: str
    status: Literal["pass", "fail", "evidence_insufficient"]
    metrics: Mapping[str, float | int | None]
    input_sha256: str

def run_path_scenarios(base_config, scenario_configs, run_turtle) -> tuple[ScenarioResult, ...]: ...
def block_bootstrap(returns: np.ndarray, block_size: int, paths: int, horizon: int, seed: int) -> np.ndarray: ...
def calculate_historical_stress(bundle, windows) -> tuple[ScenarioResult, ...]: ...
def calculate_position_shocks(positions, shocks) -> tuple[ScenarioResult, ...]: ...
def calculate_cvar(returns: np.ndarray, confidence: float) -> float: ...
def build_evidence_matrix(results: Iterable[ScenarioResult], output: Path) -> Path: ...
```

路径变化场景必须通过注入的现有海龟运行函数重新执行，不允许静态缩放收益。完整维度固定为：6 个参数变体、3 个固定时期、季度三年窗口、11 个逐 ETF 删除、6 个逐资产组删除、5 个成本/执行场景、5/20/60 日区块各 10,000 条 756 日路径、5 个历史压力窗口、4 个持仓冲击和 3 项 CVaR。

- [ ] **Step 1：写固定场景数量、确定性和路径重跑失败测试**

```python
def test_path_changing_scenarios_invoke_turtle_runner_once_each(configs):
    calls = []
    results = run_path_scenarios(BASE, configs, lambda cfg: calls.append(cfg) or bundle(cfg))
    assert len(calls) == len(configs)
    assert {row.scenario_id for row in results} == {cfg["scenario_id"] for cfg in configs}

def test_bootstrap_is_seeded_and_has_exact_shape(sample_returns):
    first = block_bootstrap(sample_returns, 20, 10_000, 756, 20260714)
    second = block_bootstrap(sample_returns, 20, 10_000, 756, 20260714)
    assert first.shape == (10_000, 756)
    np.testing.assert_array_equal(first, second)
```

- [ ] **Step 2：验证 RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_quant_robustness.py tests\local_quant_research\test_quant_stress.py -q`

Expected: FAIL，原因是稳健性模块尚不存在。

- [ ] **Step 3：实现向量化稳健性和证据矩阵**

区块抽样按固定种子分批计算，避免同时保存三组完整三维中间对象；证据矩阵逐场景保存维度、输入身份、公式版本、指标、状态和理由。输入不足必须写 `evidence_insufficient`，不得用零值补齐。

- [ ] **Step 4：验证 GREEN 与性能边界**

Run: `.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_quant_robustness.py tests\local_quant_research\test_quant_stress.py -q`

Expected: PASS；三组 10,000 路径测试使用缩小黄金夹具验证算法，完整 10,000×756 仅在集成研究运行执行。

- [ ] **Step 5：勾选 OpenSpec 10.1 并提交**

Commit: `实现：增加完整本地稳健性分析`

---

### Task 4：运行七方案并生成完整报告、推荐和人工确认门禁

**对应 OpenSpec：** 10.2、10.3、10.4、10.5

**Files：**
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/cli.py`
- Modify: `joinquant/strategies/strategy-003/research/turtle_etf/reporting.py`
- Modify: `joinquant/strategies/strategy-003/research/candidates.json`
- Modify: `joinquant/strategies/strategy-003/research/project-run.json`
- Create: `scripts/research/local_quant_research/decision.py`
- Create: `tests/local_quant_research/test_turtle_complete_report.py`
- Create: `tests/local_quant_research/test_human_decision.py`
- Modify: `tests/local_quant_research/test_turtle_e2e.py`

**Interfaces：**

```python
def run_candidate_set(snapshot, baseline, candidates) -> tuple[CandidateResult, ...]: ...
def write_complete_reports(candidate_results, evidence_matrix, attribution, output_dir) -> None: ...
def record_human_decision(run_dir: Path, decision_root: Path, decision: Mapping[str, object]) -> Path: ...
def validate_human_decision(run_dir: Path, decision_path: Path) -> Mapping[str, object]: ...
```

必须输出 `candidate-comparison.parquet`、`candidate-screening.parquet`、`local-evidence-matrix.parquet`、`attribution.parquet`、`local-research-report.md`、`challenge-report.md`、`recommendation.json` 和七项 `candidate-strategies.json`。`recommendation.json` 只能使用 `proceed_to_joinquant`、`revise_and_reassess`、`stop_evidence_insufficient`。Vibe-Trading 仅使用仓库当前可用材料；若当前没有可调用能力，报告如实记录不可用，不安装、不新增适配器，也不阻塞确定性数字和报告主体。

- [ ] **Step 1：写七次真实运行、输出门禁和人工决定失败测试**

```python
def test_all_seven_candidates_are_run_and_retained(fake_runner, candidate_config):
    results = run_candidate_set(SNAPSHOT, BASELINE, candidate_config)
    assert len(fake_runner.calls) == 7
    assert [row.candidate_id for row in results] == [row["candidate_id"] for row in candidate_config]

def test_complete_requires_reports_recommendation_and_human_next_action(output_dir):
    write_complete_reports(results(), evidence(), attribution(), output_dir)
    recommendation = json.loads((output_dir / "recommendation.json").read_text("utf-8"))
    assert recommendation["next_action"] == "human_confirmation_required"
```

补充：不得自动删候选、不得替换基线、Vibe 不可用留痕、决定文件必须位于 `.local/quant-research-decisions/strategy-003/<run_id>/<decision_id>/`、决定摘要不匹配拒绝、确认前禁止外部动作。

- [ ] **Step 2：验证 RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_complete_report.py tests\local_quant_research\test_human_decision.py tests\local_quant_research\test_turtle_e2e.py -q`

Expected: FAIL，原因是当前项目只运行基线并输出旧三类结果。

- [ ] **Step 3：实现七方案、报告和追加式人工决定**

候选按 `candidates.json` 固定顺序运行，共用代码摘要和快照。推荐基于确定性指标、稳健性证据和反对证据形成，但不得修改候选清单。人工决定引用 `run_id`、报告摘要和推荐摘要，使用原子目录写入且永不覆盖。

- [ ] **Step 4：验证 GREEN 和项目回归**

Run: `.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_turtle_complete_report.py tests\local_quant_research\test_human_decision.py tests\local_quant_research\test_turtle_e2e.py tests\local_quant_research\test_runner.py -q`

Expected: PASS；缺任何完整输出时项目和通用运行器都不得返回 `complete`。

- [ ] **Step 5：勾选 OpenSpec 10.2–10.5 并提交**

Commit: `实现：生成七方案完整研究报告`

---

### Task 5：更新 Skill、配置并完成非海龟与海龟完整 E2E

**对应 OpenSpec：** 11.1、11.2、11.3

**Files：**
- Modify: `.agents/skills/run-local-quant-research/SKILL.md`
- Modify: `.agents/skills/run-local-quant-research/references/operations.md`
- Modify: `.build-and-verify/config.json`
- Modify: `tests/local_quant_research/test_skill_contract.py`
- Modify: `tests/local_quant_research/test_generic_e2e.py`
- Modify: `tests/local_quant_research/test_turtle_e2e.py`
- Modify: `joinquant/strategies/strategy-003/research/project-run.json`

**Interfaces：** Skill 公开入口仍为现有单一 CLI；只更新阶段、必需输出和停止状态，不把海龟字段写入 Skill。非海龟夹具必须使用同一 Parquet 行情、八表分析、报告和人工确认门禁，证明共用层未反向依赖海龟。

- [ ] **Step 1：写失败的公开入口与完整 E2E 断言**

```python
def test_skill_documents_parquet_complete_analysis_and_human_gate(skill_text):
    assert "market-data.parquet" in skill_text
    assert "human_confirmation_required" in skill_text
    assert "55 日" not in skill_text
```

两个 E2E 都必须从 Skill 文档公开命令启动，检查 CSV 暂存清理、Parquet、内存 DuckDB、八表、分析、稳健性、报告、推荐、七候选和停止门禁；不得以单元测试拼接替代。

- [ ] **Step 2：验证 RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_skill_contract.py tests\local_quant_research\test_generic_e2e.py tests\local_quant_research\test_turtle_e2e.py -q`

Expected: FAIL，原因是 Skill 和 E2E 仍声明 CSV 与旧三类输出。

- [ ] **Step 3：更新编排说明、项目配置和验证映射**

保持 `.claude/skills/run-local-quant-research` 符号链接不变；Build and Verify（构建与验证）影响映射加入 `quant_analysis`、新测试和报告配置，继续排除 `.local` 数据。

- [ ] **Step 4：验证 GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_skill_contract.py tests\local_quant_research\test_generic_e2e.py tests\local_quant_research\test_turtle_e2e.py -q`

Expected: PASS；两个 E2E 均从公开入口独立完成并清理临时产物。

- [ ] **Step 5：勾选 OpenSpec 11.1–11.3 并提交**

Commit: `验证：覆盖完整本地研究端到端流程`

---

### Task 6：迁移真实行情、运行完整研究并完成全部门禁

**对应 OpenSpec：** 11.4、11.5

**Files：**
- Modify: `docs/research/2026-07-13-turtle-etf-system-final-plan.md`
- Modify: `docs/superpowers/reports/2026-07-14-build-turtle-etf-local-research-workflow-verify.md`
- Modify: `openspec/changes/build-turtle-etf-local-research-workflow/tasks.md`
- Modify only if required by verified impact mapping: `.build-and-verify/config.json`
- Runtime only, ignored: `.local/market-data/`
- Runtime only, ignored: `.local/quant-research/strategy-003/`

**执行要求：** 优先迁移 `.local` 中已经验证的真实 11 ETF CSV 批次；只有现有数据不完整时才使用已确认的聚宽研究导出流程补充。不得启动正式回测。真实研究必须使用明确的 `snapshot_end_date`，完整运行七方案和全部稳健性维度，产生完整报告与推荐，并停在人工确认门禁。任何临时 CSV 在验证后删除。

- [ ] **Step 1：运行迁移前只读审计**

Run: `.\.venv\Scripts\python.exe -m scripts.research.market_data.cli audit --root .local/market-data`

Expected: 列出旧 CSV 批次、11 ETF 覆盖、日期范围和迁移资格；不修改原批次。

- [ ] **Step 2：迁移或补充真实行情并验证清理**

Run: 使用 Skill 文档公开的行情导入命令创建新的 Parquet 批次和快照。

Expected: 新快照只引用 `schema_version=2` 批次；`.local` 暂存和聚宽远端均无传输 CSV；旧 CSV 批次保持历史不变。

- [ ] **Step 3：从 Skill 公开入口运行真实完整研究**

Run: 使用 `.agents/skills/run-local-quant-research/SKILL.md` 声明的命令和 `strategy-003/research/project-run.json`。

Expected: 七方案全部运行；完整报告、推荐、八表、归因和证据矩阵通过摘要校验；状态为 `complete` 且 `next_action=human_confirmation_required`。

- [ ] **Step 4：运行全量验证**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
openspec validate build-turtle-etf-local-research-workflow --strict
.\.venv\Scripts\python.exe "$env:USERPROFILE\.codex\skills\.system\skill-creator\scripts\quick_validate.py" .agents\skills\run-local-quant-research
```

再运行仓库 `build-and-verify`（构建与验证）完整门禁、敏感数据扫描和独立前向验证。

Expected: 全部 PASS；Git（版本管理）跟踪文件中无行情、凭证或 `.local` 产物。

- [ ] **Step 5：人工复核报告并更新验证证据**

验证报告必须记录：实际命令、测试数量、真实 `snapshot_id`、`run_id`、七方案、收益、回撤、风险、Alpha/Beta（超额收益/市场暴露）、完整稳健性、推荐、无法验证项和临时产物清理证据。不能把本地结论写成聚宽正式结论。

- [ ] **Step 6：勾选 OpenSpec 11.4–11.5 并提交**

Commit: `完成：交付海龟ETF完整本地研究`
