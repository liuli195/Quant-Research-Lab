---
change: build-turtle-etf-robustness-analysis-workflow
design-doc: docs/superpowers/specs/2026-07-19-standard-strategy-analysis-workflow-design.md
base-ref: 272a6bca1246b965bb36cf91902086fe7d7d9bc8
---

# 标准策略分析流程实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`（子代理驱动开发，推荐） or `superpowers:executing-plans`（执行计划） to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立一个只读的标准策略分析入口，使用明确登记的本地研究、聚宽回测和聚宽模拟交易快照，交付共同绩效/风险、可验证的深度归因和按能力降级的稳健性分析。

**Architecture:** 新增一个小型来源登记模块，负责路径、清单摘要、来源类型和模拟交易快照身份的验证；`analysis_data`（分析数据）仍是唯一读取共同事实表的入口。通用入口在既有确定性计算函数之上增加来源能力矩阵：四张共同表始终可计算，归因、成本敏感性和策略事件依赖其对应的已验证证据，缺失时仅产生 `evidence_insufficient`（证据不足）条目。

**Tech Stack:** Python（编程语言）3.12、PyArrow（列式数据处理）、DuckDB（嵌入式分析数据库）、Pandas（表格数据处理）、pytest（测试框架）、JSON Schema（结构约束）。

## Global Constraints

- 只使用项目 `\.venv\Scripts\python.exe`；不得安装新依赖或回退系统 Python（编程语言）。
- 正式回测和模拟交易仍只在 JoinQuant（聚宽）云端运行；本变更只能读取已归档的对象，绝不提交、同步、恢复或修改上游流程。
- 仓库内 `analyze-quant-robustness` Skill（技能）是唯一公开流程入口；Python（编程语言）命令行接口只可作为该 Skill 的内部执行后端。
- 来源登记版本固定为 `standard-analysis-source-registry/1`，每个来源必须显式提供 `scenario_id`、仓库相对 `path`、`source_type`、`manifest_sha256`；模拟交易额外提供 `snapshot_id`。
- 允许的 `source_type`（来源类型）只有 `local_research`、`joinquant_backtest`、`joinquant_simulation`；拒绝 `latest`（最新）、目录扫描、绝对路径、`..` 越界路径和未登记替代对象。
- 共同计算只读取 `results`（收益）、`balances`（资产）、`positions`（持仓）、`orders`（订单）及显式基准集；不得复制、转换或改写来源归档。
- 本地研究没有物理 `risk`（风险）和 `period_risks`（分期风险）表时必须保留 `missing_at_source`（来源缺失）参考，不能伪造表；聚宽模拟交易 `risk` 的 `intraday_return`、`monthly_return` 等额外官方字段只作来源参考，不能加入共同字段。
- 事件级深度归因仅接受通过摘要、来源原生事件时间和事件标识字段及时间范围验证的本地归因扩展或聚宽 `attribution_log`（归因日志）；不得把聚宽事件日志伪造成策略专用的单标的盈亏归因，订单、持仓和后验价格不能反推缺失的事件归因。
- 只使用三种结论状态：`pass`（通过）、`fail`（失败）和 `evidence_insufficient`（证据不足）；已知失败与证据不足必须同时保留。
- 新标准分析输出只能写入 `.local/standard-strategy-analysis/<analysis_id>/`；输入来源的文件字节摘要在运行前后必须相同。

## 文件结构

| 文件 | 责任 |
| --- | --- |
| `scripts/research/analysis_data/manifest.py` | 打开并验证聚宽回测或带指定快照的聚宽模拟交易清单，保留来源根目录与数据前缀。 |
| `scripts/research/analysis_data/views.py` | 为三类来源建立只读 DuckDB（嵌入式分析数据库）视图，并将模拟交易官方风险额外字段排除在共同视图之外。 |
| `scripts/research/quant_analysis/source_registry.py` | 读取、校验并归一化版本化来源登记，生成不可变来源能力摘要。 |
| `scripts/research/quant_analysis/unified_analysis.py` | 加载已登记场景、编排共同指标/归因/稳健性，写入标准 JSON（结构化数据）和证据矩阵。 |
| `scripts/research/quant_analysis/reporting.py` | 由标准分析 JSON（结构化数据）生成包含来源与能力矩阵的 Markdown（标记文档）报告。 |
| `.agents/skills/analyze-quant-robustness/` | 唯一公开 Skill（技能）入口，只描述离线调用顺序和停止条件。 |
| `.claude/skills/analyze-quant-robustness` | 指向上述 Skill（技能）目录的相对符号链接。 |
| `tests/local_quant_research/test_analysis_data_views.py` | 聚宽模拟快照、共同表和风险参考的只读契约测试。 |
| `tests/quant_analysis/test_source_registry.py` | 来源登记路径、摘要、类型、快照和能力矩阵测试。 |
| `tests/quant_analysis/test_unified_analysis.py` | 已登记场景加载、归因投影、三态稳健性与聚合规则测试。 |
| `tests/quant_analysis/test_reporting.py` | 标准 JSON（结构化数据）到 Markdown（标记文档）的可追溯交付测试。 |
| `tests/quant_analysis/test_standard_analysis_e2e.py` | 通过真实 Skill（技能）入口运行三类来源的离线端到端回归。 |
| `tests/test_skill_layout.py` | 新 Skill（技能）与 Claude（克劳德）相对符号链接的布局测试。 |

---

### Task 1: 让分析数据入口显式支持聚宽模拟交易快照

**Files:**

- Modify: `scripts/research/analysis_data/manifest.py:47-297`
- Modify: `scripts/research/analysis_data/views.py:95-264`
- Modify: `tests/local_quant_research/test_analysis_data_views.py:497-651`

**Interfaces:**

- Consumes: `open_analysis_source(result_dir: Path)` 与聚宽归档 `manifest.json`（清单）。
- Produces: `open_analysis_source(result_dir: Path, *, snapshot_id: str | None = None) -> AnalysisSource`，其中 `AnalysisSource.kind` 为 `joinquant_backtest` 或 `joinquant_simulation`，并新增 `data_prefix: PurePosixPath`、`snapshot_id: str | None`。
- Produces: `open_analysis_database(result_dir: Path, *, snapshot_id: str | None = None) -> AnalysisDatabase`；共同视图字段继续使用 `_SCHEMAS`（字段约束）。

- [ ] **Step 1: 为模拟交易快照写失败测试**

  在 `tests/local_quant_research/test_analysis_data_views.py` 增加使用现有 `strategy-001` 模拟归档的测试；先断言当前实现拒绝模拟交易或没有 `snapshot_id` 参数。

  ```python
  def test_joinquant_simulation_requires_and_pins_one_snapshot(repo_root: Path) -> None:
      root = repo_root / "joinquant/strategies/strategy-001/simulations/simulation-001"
      snapshot_id = "5cc582a778eca2ddc481282b"
      before = _tree_sha(root)

      with pytest.raises(AnalysisManifestError, match="explicit snapshot_id"):
          open_analysis_source(root)

      with open_analysis_database(root, snapshot_id=snapshot_id) as database:
          assert database.source.kind == "joinquant_simulation"
          assert database.source.snapshot_id == snapshot_id
          assert database.connection.sql("select count(*) from results").fetchone()[0] > 0
          assert database.connection.sql("select count(*) from risk").fetchone() == 1

      assert _tree_sha(root) == before
  ```

- [ ] **Step 2: 运行测试并确认失败原因**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_analysis_data_views.py::test_joinquant_simulation_requires_and_pins_one_snapshot -q
  ```

  Expected: FAIL，错误说明聚宽清单不是 `backtest`（回测）或函数不接受 `snapshot_id`（快照标识）。

- [ ] **Step 3: 最小化扩展清单验证和数据路径解析**

  在 `manifest.py` 中将来源类型和数据前缀变为 `AnalysisSource`（分析来源）的显式属性；只允许聚宽回测在没有快照参数时打开，模拟交易必须匹配根清单 `streams.snapshots.cursor` 且所有 `data/*.parquet` 声明都位于 `snapshots/<snapshot_id>/data/`。

  ```python
  @dataclass(frozen=True)
  class AnalysisSource:
      root: Path
      kind: str
      schema_version: int | str
      manifest: Mapping[str, object]
      data_prefix: PurePosixPath = PurePosixPath("data")
      snapshot_id: str | None = None
      authority: str | None = None
      backend: str | None = None
      formula_version: str | None = None


  def open_analysis_source(
      result_dir: Path, *, snapshot_id: str | None = None
  ) -> AnalysisSource:
      # 现有本地两类分支保持不变；schema_version == 1 时调用该分支。
      object_kind = _validate_joinquant_document(document, snapshot_id=snapshot_id)
      if object_kind == "simulation":
          assert snapshot_id is not None
          _validate_simulation_snapshot_files(root, document, snapshot_id)
          return AnalysisSource(
              root=root,
              kind="joinquant_simulation",
              schema_version=1,
              manifest=document,
              data_prefix=PurePosixPath(f"snapshots/{snapshot_id}/data"),
              snapshot_id=snapshot_id,
          )
      _validate_joinquant_files(root, document, data_prefix=PurePosixPath("data"))
      return AnalysisSource(
          root=root,
          kind="joinquant_backtest",
          schema_version=1,
          manifest=document,
      )
  ```

  `_validate_simulation_snapshot_files`（验证模拟交易快照文件）必须逐个复用已有大小、SHA256（完整性摘要）、Parquet（列式数据）行数检查；遇到当前游标、文件路径或摘要漂移时抛出 `AnalysisManifestError`（分析清单错误），不得选择其他快照。

- [ ] **Step 4: 使视图只投影共同字段并保留风险参考状态**

  在 `views.py` 的 `_declared_parquet_path` 使用 `source.data_prefix`，在 `_validate_physical_fields` 仅对 `joinquant_simulation` 的 `risk`（风险）允许字段超集，其余共同表仍要求现有字段集合。`_parquet_query` 保持只选择 `_SCHEMAS["risk"]` 中已有的 31 个共同字段，因此 `intraday_return` 和 `monthly_return` 不会进入分析视图。

  ```python
  def _declared_parquet_path(source: AnalysisSource, name: str) -> Path | None:
      expected = (source.data_prefix / f"{name}.parquet").as_posix()
      for reference in source.manifest["datasets"][name]["files"]:
          if reference.get("path") == expected and reference.get("format") == "parquet":
              return source.root / expected
      return None


  def _validate_physical_fields(source: AnalysisSource, name: str, path: Path) -> None:
      actual = tuple(pq.read_schema(path).names)
      expected = tuple(field for field, _ in _SCHEMAS[name])
      if source.kind == "joinquant_simulation" and name == "risk":
          fields_match = set(expected).issubset(actual)
      elif source.kind.startswith("joinquant_"):
          fields_match = len(actual) == len(expected) and set(actual) == set(expected)
      else:
          fields_match = actual == expected
      if not fields_match:
          raise AnalysisManifestError(
              f"{source.kind} {name} fields do not match the observed contract"
          )
  ```

  同时为 `local_research`（本地研究）扩展 `AnalysisDatabase.reference_status`：当调用者查询 `risk` 或 `period_risks` 时返回 `("missing_at_source", "local-research-package/2 has no physical official risk table")`，但 `table_names` 仍只返回四张物理表。

- [ ] **Step 5: 扩充失败场景与通过场景**

  在同一测试文件加入这两个断言：

  ```python
  def test_simulation_rejects_dataset_outside_registered_snapshot(
      repo_root: Path, tmp_path: Path
  ) -> None:
      source = repo_root / "joinquant/strategies/strategy-001/simulations/simulation-001"
      copied = _copy_tree(source, tmp_path / "simulation")
      manifest_path = copied / "manifest.json"
      manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
      manifest["datasets"]["results"]["files"][1]["path"] = "data/results.parquet"
      manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
      with pytest.raises(AnalysisManifestError, match="registered snapshot"):
          open_analysis_database(copied, snapshot_id="5cc582a778eca2ddc481282b")


  def test_simulation_risk_extra_fields_are_not_common_fields(repo_root: Path) -> None:
      root = repo_root / "joinquant/strategies/strategy-001/simulations/simulation-001"
      with open_analysis_database(root, snapshot_id="5cc582a778eca2ddc481282b") as database:
          fields = [row[0] for row in database.connection.sql("describe risk").fetchall()]
      assert "intraday_return" not in fields
      assert "monthly_return" not in fields
      assert "sharpe" in fields
  ```

- [ ] **Step 6: 运行定向分析数据回归**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_analysis_data_views.py -q
  ```

  Expected: PASS；既有本地研究和聚宽回测视图测试不改变，新增模拟快照与额外风险字段测试通过。

- [ ] **Step 7: 提交本任务**

  ```powershell
  git add scripts/research/analysis_data/manifest.py scripts/research/analysis_data/views.py tests/local_quant_research/test_analysis_data_views.py
  git commit -m "feat: 支持聚宽模拟交易快照分析数据"
  ```

### Task 2: 建立版本化、只读的来源登记与能力矩阵

**Files:**

- Create: `scripts/research/quant_analysis/source_registry.py`
- Create: `scripts/research/quant_analysis/schemas/source-registry.schema.json`
- Create: `tests/quant_analysis/test_source_registry.py`
- Modify: `scripts/research/quant_analysis/__init__.py`

**Interfaces:**

- Consumes: `open_analysis_source(result_dir: Path, snapshot_id: str | None = None)`、仓库根目录和外部提供的 JSON（结构化数据）来源登记。
- Produces: `load_source_registry(repo_root: Path, registry_path: Path) -> SourceRegistry`。
- Produces: `SourceRegistration(scenario_id: str, source_type: str, root: Path, manifest_sha256: str, snapshot_id: str | None)` 与 `RegisteredSource(registration: SourceRegistration, source: AnalysisSource, capabilities: Mapping[str, Mapping[str, object]])`。

- [ ] **Step 1: 写来源登记契约失败测试**

  在新测试文件先固定一个最小、可读的登记文档。它必须同时登记本地研究包、聚宽回测和聚宽模拟快照，且根路径全部相对仓库。

  ```python
  def _registry(repo_root: Path, *, entries: list[dict[str, object]]) -> Path:
      path = repo_root / "fixtures" / "source-registry.json"
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(json.dumps({
          "schema_version": "standard-analysis-source-registry/1",
          "analysis_plan": "joinquant/strategies/strategy-003/research/analysis-plan.json",
          "benchmark_manifest": ".local/market-data/benchmark-sets/bench/manifest.json",
          "baseline_scenario_id": "baseline",
          "sources": entries,
      }, sort_keys=True), encoding="utf-8")
      return path


  def test_registry_opens_only_three_explicit_source_kinds(repo_root: Path, tmp_path: Path) -> None:
      local = _copy_result_package_into_repo(repo_root, tmp_path / "local")
      entries = [
          _entry(repo_root, local, "baseline", "local_research"),
          _entry(repo_root, repo_root / "joinquant/strategies/strategy-001/backtests/111", "backtest", "joinquant_backtest"),
          _entry(repo_root, repo_root / "joinquant/strategies/strategy-001/simulations/simulation-001", "simulation", "joinquant_simulation", snapshot_id="5cc582a778eca2ddc481282b"),
      ]
      registry = load_source_registry(repo_root, _registry(repo_root, entries=entries))
      assert [item.source.kind for item in registry.sources] == [
          "local_research", "joinquant_backtest", "joinquant_simulation"
      ]
  ```

- [ ] **Step 2: 运行测试并确认新模块尚不存在**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_source_registry.py::test_registry_opens_only_three_explicit_source_kinds -q
  ```

  Expected: FAIL，原因是 `source_registry`（来源登记）模块或 `load_source_registry`（加载来源登记）尚不存在。

- [ ] **Step 3: 实现严格登记解析与来源验证**

  在 `source-registry.schema.json` 明确顶层和来源条目，不允许额外字段。`source_registry.py` 以 JSON Schema（结构约束）先校验文件，再完成安全路径、清单摘要、声明类型和模拟快照的实体验证。

  ```python
  SOURCE_REGISTRY_SCHEMA_VERSION = "standard-analysis-source-registry/1"
  SOURCE_TYPES = frozenset({
      "local_research", "joinquant_backtest", "joinquant_simulation",
  })


  @dataclass(frozen=True)
  class SourceRegistration:
      scenario_id: str
      source_type: str
      root: Path
      manifest_sha256: str
      snapshot_id: str | None


  def load_source_registry(repo_root: Path, registry_path: Path) -> SourceRegistry:
      root = Path(repo_root).resolve()
      document_path = _resolve_repository_file(root, registry_path, "registry_path")
      document = _load_json(document_path, "source registry")
      _validate_schema(document)
      sources = tuple(_open_registration(root, entry) for entry in document["sources"])
      _require_unique([item.registration.scenario_id for item in sources], "scenario_id")
      _require_member(document["baseline_scenario_id"], {item.registration.scenario_id for item in sources}, "baseline_scenario_id")
      return SourceRegistry(
          path=document_path,
          analysis_plan=_resolve_repository_file(root, document["analysis_plan"], "analysis_plan"),
          benchmark_manifest=_resolve_repository_file(root, document["benchmark_manifest"], "benchmark_manifest"),
          baseline_scenario_id=str(document["baseline_scenario_id"]),
          sources=sources,
      )
  ```

  `_open_registration`（打开登记）必须拒绝 `path == "latest"`、目录、绝对路径、含 `..` 的路径和摘要不匹配；调用 `open_analysis_source(root, snapshot_id=entry_snapshot)` 后要求 `source.kind == source_type`。它只读取明确的 `root/manifest.json`，不使用 `glob`（通配扫描）、时间排序或目录搜索。

- [ ] **Step 4: 产生固定能力摘要**

  将能力摘要限定为下列键和状态，供后续统一分析使用；它不能把缺失证据转换为成功。

  ```python
  def _capabilities(source: AnalysisSource) -> Mapping[str, Mapping[str, object]]:
      physical = set(LOCAL_PHYSICAL_DATASETS if source.kind == "local_research" else CORE_DATASETS)
      return MappingProxyType({
          "common_facts": {"status": "available", "tables": list(LOCAL_PHYSICAL_DATASETS)},
          "official_risk": {
              "status": "available" if "risk" in physical else "missing_at_source",
              "source_only_extra_fields": ["intraday_return", "monthly_return"]
                  if source.kind == "joinquant_simulation" else [],
          },
          "attribution": _attribution_capability(source),
          "cost_execution": _cost_capability(source),
      })
  ```

  `_attribution_capability`（归因能力）只检查已声明文件、摘要、Parquet（列式数据）来源原生的事件时间和事件标识字段，以及可解析、非倒置且与共同收益区间重叠的时间范围；返回 `available`、`missing_at_source` 或 `evidence_insufficient`。本地检查登记的 `attribution_log` 扩展，聚宽检查 `datasets.attribution_log` 指定快照文件。聚宽以 `current_dt`（事件时间）和 `event`（事件标识）为最小契约，可选保留 `reason`（原因）与 `etf`/`security`（标的），不得改写成 `security_daily_pnl`（单标的日盈亏）。`_cost_capability` 只有在显式来源证明可安全定位同一市场快照时才返回 `available`；其他来源直接是 `missing_at_source`。

- [ ] **Step 5: 补齐拒绝和能力测试**

  ```python
  @pytest.mark.parametrize("mutate, message", [
      (lambda entry: entry.update(path="latest"), "latest"),
      (lambda entry: entry.update(path="../outside"), "repository-relative"),
      (lambda entry: entry.update(manifest_sha256="0" * 64), "manifest digest"),
      (lambda entry: entry.update(source_type="joinquant_backtest"), "declared source_type"),
      (lambda entry: entry.update(snapshot_id="another-snapshot"), "registered snapshot"),
  ])
  def test_registry_rejects_unpinned_or_mismatched_source(
      repo_root: Path,
      tmp_path: Path,
      mutate: Callable[[dict[str, object]], None],
      message: str,
  ) -> None:
      local = _copy_result_package_into_repo(repo_root, tmp_path / "local")
      entry = _entry(repo_root, local, "baseline", "local_research")
      mutate(entry)
      registry_path = _registry(repo_root, entries=[entry])
      with pytest.raises(SourceRegistryError, match=message):
          load_source_registry(repo_root, registry_path)


  def test_local_risk_and_missing_attribution_are_capabilities_not_fake_data(
      repo_root: Path, tmp_path: Path
  ) -> None:
      local = _copy_result_package_into_repo(repo_root, tmp_path / "local")
      registry_path = _registry(
          repo_root, entries=[_entry(repo_root, local, "baseline", "local_research")]
      )
      registered = load_source_registry(repo_root, registry_path).sources[0]
      assert registered.capabilities["official_risk"]["status"] == "missing_at_source"
      assert registered.capabilities["attribution"]["status"] == "missing_at_source"
  ```

  `_copy_result_package_into_repo`（复制结果包到仓库）必须把现有测试包复制到 `repo_root / ".local" / "source-registry-tests" / tmp_path.name`，并通过测试的 `finally` 清理该目录；`_entry`（登记条目帮助函数）必须从 `root / "manifest.json"` 计算 `manifest_sha256`，将路径转换为 `relative_to(repo_root).as_posix()`。

- [ ] **Step 6: 运行来源登记回归**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_source_registry.py tests\local_quant_research\test_analysis_data_views.py -q
  ```

  Expected: PASS；三类来源均按显式登记打开，五类不安全或漂移登记均被拒绝。

- [ ] **Step 7: 提交本任务**

  ```powershell
  git add scripts/research/quant_analysis/source_registry.py scripts/research/quant_analysis/schemas/source-registry.schema.json scripts/research/quant_analysis/__init__.py tests/quant_analysis/test_source_registry.py
  git commit -m "feat: 增加标准分析来源登记"
  ```

### Task 3: 将已登记来源投影为共同场景与深度归因输入

**Files:**

- Modify: `scripts/research/quant_analysis/unified_analysis.py:64-305`
- Modify: `tests/quant_analysis/test_unified_analysis.py:1-450`
- Modify: `tests/quant_analysis/test_source_registry.py`

**Interfaces:**

- Consumes: `RegisteredSource`（已登记来源）、`SourceRegistry`（来源登记）和 `expand_analysis_plan`（展开分析计划）结果。
- Produces: `load_registered_scenario(registered: RegisteredSource, *, analysis_params: Mapping[str, object]) -> ScenarioInput`。
- Produces: `ScenarioInput` 新增 `source_type: str`、`source_manifest_sha256: str`、`capabilities: Mapping[str, Mapping[str, object]]`、`attribution_status: str`，现有字段对旧本地流程保持兼容。

- [ ] **Step 1: 先写共同四表和归因缺失的失败测试**

  ```python
  def test_registered_sources_share_the_four_common_facts(repo_root: Path, registry: SourceRegistry) -> None:
      plan = expand_analysis_plan(repo_root, registry.analysis_plan)
      by_id = {item["scenario_id"]: item["params"] for item in plan["scenarios"]}
      scenarios = [
          load_registered_scenario(item, analysis_params=by_id[item.registration.scenario_id])
          for item in registry.sources
      ]
      assert [list(item.balances.columns[:5]) for item in scenarios] == [
          ["total_value", "net_value", "cash", "aval_cash", "time"]
      ] * 3
      assert all(not item.returns.empty for item in scenarios)


  def test_missing_attribution_keeps_common_analysis_and_marks_deep_input_missing(
      local_without_attribution: RegisteredSource,
  ) -> None:
      scenario = load_registered_scenario(local_without_attribution, analysis_params={})
      assert scenario.events.empty
      assert scenario.attribution_status == "missing_at_source"
  ```

- [ ] **Step 2: 运行测试并确认旧加载器只接受本地运行目录**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_unified_analysis.py -k "registered_sources or missing_attribution" -q
  ```

  Expected: FAIL，因为 `_load_scenario`（加载场景）要求 `manifest.run`、`params.json` 和 `performance.json`，并强制需要本地归因文件。

- [ ] **Step 3: 增加已登记场景加载器，不替换旧本地入口**

  保留 `_load_scenario` 和 `run_deterministic_analysis` 的既有本地研究调用链；新增一个由标准入口专用的加载器，任何数据访问只能通过 Task 1 的 `open_analysis_database`（打开分析数据库）。

  ```python
  @dataclass(frozen=True)
  class ScenarioInput:
      scenario_id: str
      run_id: str
      result_dir: Path
      returns: pd.Series
      balances: pd.DataFrame
      positions: pd.DataFrame
      orders: pd.DataFrame
      events: pd.DataFrame
      params: Mapping[str, object]
      performance: Mapping[str, object]
      source_type: str = "local_research"
      source_manifest_sha256: str = ""
      capabilities: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
      attribution_status: str = "available"


  def load_registered_scenario(
      registered: RegisteredSource, *, analysis_params: Mapping[str, object]
  ) -> ScenarioInput:
      registration = registered.registration
      with open_analysis_database(
          registration.root, snapshot_id=registration.snapshot_id
      ) as database:
          returns = _daily_returns(database)
          balances = database.connection.sql(
              "select total_value, net_value, cash, aval_cash, time from balances order by time"
          ).fetchdf()
          positions = database.connection.sql("select * from positions order by time, security").fetchdf()
          orders = database.connection.sql("select * from orders order by time, security").fetchdf()
      for frame in (balances, positions, orders):
          frame["date"] = pd.to_datetime(frame["time"]).dt.normalize()
      events, attribution_status = _load_verified_attribution(registered)
      return ScenarioInput(
          scenario_id=registration.scenario_id,
          run_id=str(registered.source.manifest["object"]["local_id"]),
          result_dir=registration.root,
          returns=returns,
          balances=balances,
          positions=positions,
          orders=orders,
          events=events,
          params=dict(analysis_params),
          performance={},
          source_type=registration.source_type,
          source_manifest_sha256=registration.manifest_sha256,
          capabilities=registered.capabilities,
          attribution_status=attribution_status,
      )
  ```

  对 `local_research` 的 `run_id` 使用 `object.run_id`；对聚宽来源使用 `object.local_id`。实现中将这两个分支放入 `_source_run_id`（来源运行标识）小函数，避免错误地假设聚宽有 `run`（运行）对象。

- [ ] **Step 4: 只在能力可用时投影事件归因**

  实现 `_load_verified_attribution(registered)`：能力为 `available` 时读取 Task 2 已验证的单个 Parquet（列式数据）文件，要求字段 `time`、`event_type`、`reason_code`、`security`、`details_json`，填充 `event_time` 与 `date`；能力不是 `available` 时返回指定列的零行 DataFrame（表格数据）和原始状态。禁止由 `orders`（订单）或 `positions`（持仓）合成 `events`（事件）。

  ```python
  _EVENT_COLUMNS = ("time", "event_type", "reason_code", "security", "details_json")

  def _load_verified_attribution(
      registered: RegisteredSource,
  ) -> tuple[pd.DataFrame, str]:
      state = str(registered.capabilities["attribution"]["status"])
      if state != "available":
          return pd.DataFrame(columns=[*_EVENT_COLUMNS, "event_time", "date"]), state
      path = Path(str(registered.capabilities["attribution"]["path"]))
      events = pq.read_table(path, columns=list(_EVENT_COLUMNS)).to_pandas()
      events["event_time"] = pd.to_datetime(events["time"])
      events["date"] = events["event_time"].dt.normalize()
      return events, "available"
  ```

- [ ] **Step 5: 收紧深度归因函数的降级行为**

  让 `_attribution`（归因）在 `scenario.attribution_status != "available"` 时返回不含推断数值的明确结果；让 `_security_pnl_facts`（证券盈亏事实）返回空表，后续依赖项将产生证据不足而非“现金未分类”伪归因。

  ```python
  def _attribution(scenario: ScenarioInput, pnl_facts: pd.DataFrame) -> dict[str, object]:
      if scenario.attribution_status != "available":
          return {
              "status": "evidence_insufficient",
              "reason": scenario.attribution_status,
              "method": None,
              "security": [], "asset_group": [], "trading_reason": [], "period": [],
              "event_counts": {}, "reconciliation_error": None,
          }
      return _available_attribution(scenario, pnl_facts)
  ```

  将当前 `_attribution` 的完整已验证算术日盈亏实现原样移动到新函数 `_available_attribution(scenario: ScenarioInput, pnl_facts: pd.DataFrame) -> dict[str, object]`；该函数继续计算 `security`、`asset_group`、`trading_reason`、`period`、`exposure`、`event_counts` 和精确的 `reconciliation_error=0.0`。现有纯本地测试的默认 `attribution_status="available"` 必须继续覆盖该路径。

- [ ] **Step 6: 运行共同表和归因测试**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_unified_analysis.py tests\quant_analysis\test_source_registry.py -q
  ```

  Expected: PASS；三种来源使用同一共同表加载路径，本地和聚宽归因分别可用，归因缺失只降级深度归因。

- [ ] **Step 7: 提交本任务**

  ```powershell
  git add scripts/research/quant_analysis/unified_analysis.py tests/quant_analysis/test_unified_analysis.py tests/quant_analysis/test_source_registry.py
  git commit -m "feat: 统一加载标准分析来源"
  ```

### Task 4: 对共同稳健性计算实施能力门禁与三态聚合

**Files:**

- Modify: `scripts/research/quant_analysis/unified_analysis.py:683-1435`
- Modify: `scripts/research/quant_analysis/evidence.py`
- Modify: `tests/quant_analysis/test_unified_analysis.py:450-720`

**Interfaces:**

- Consumes: `ScenarioInput.capabilities`、共同收益/资产/持仓/订单表和既有分析计划 `analyses`（分析配置）。
- Produces: `run_standard_analysis(repo_root: Path, source_registry_path: Path) -> dict[str, object]`。
- Produces: 每个稳健性条目均包含 `scenario_id`、`dimension`、`status`、`reasons`、`metrics`；`build_evidence_matrix`（构建证据矩阵）保留三种状态的原始行。

- [ ] **Step 1: 编写能力降级和并存状态失败测试**

  ```python
  def test_standard_analysis_keeps_pass_fail_and_evidence_insufficient_together(
      repo_root: Path, standard_registry_path: Path
  ) -> None:
      result = run_standard_analysis(repo_root, standard_registry_path)
      statuses = {row["status"] for row in result["evidence_rows"]}
      assert statuses == {"pass", "fail", "evidence_insufficient"}
      assert result["attribution"]["status"] == "evidence_insufficient"
      assert result["robustness"]["cost_execution"][0]["status"] == "evidence_insufficient"


  def test_single_source_runs_return_only_robustness_and_marks_cross_scenario_missing(
      repo_root: Path, single_source_registry: Path
  ) -> None:
      result = run_standard_analysis(repo_root, single_source_registry)
      assert result["challenge_results"] == []
      assert result["robustness"]["bootstrap"]
      assert result["cross_scenario"]["status"] == "evidence_insufficient"
  ```

- [ ] **Step 2: 运行测试并确认当前入口依赖本地准备工作区**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_unified_analysis.py -k "standard_analysis or single_source" -q
  ```

  Expected: FAIL，因为当前 `run_deterministic_analysis`（运行确定性分析）要求 `preparation_workspace`（准备工作区）、七个本地运行和市场快照。

- [ ] **Step 3: 以登记与分析计划编排标准入口**

  新增 `run_standard_analysis`，它读取一个来源登记，调用 `expand_analysis_plan`，要求登记中的场景是分析计划场景的非空子集且包含基线；不要求两个或七个场景。分析标识只由来源登记字节摘要、展开计划摘要、基准清单摘要和所有已验证来源清单摘要决定。

  ```python
  def run_standard_analysis(repo_root: Path, source_registry_path: Path) -> dict[str, object]:
      root = Path(repo_root).resolve()
      registry = load_source_registry(root, source_registry_path)
      expanded = expand_analysis_plan(root, registry.analysis_plan)
      scenario_config = {item["scenario_id"]: item["params"] for item in expanded["scenarios"]}
      scenarios = {
          item.registration.scenario_id: load_registered_scenario(
              item, analysis_params=scenario_config[item.registration.scenario_id]
          )
          for item in registry.sources
      }
      baseline = scenarios[registry.baseline_scenario_id]
      analysis_id = evidence_digest({
          "formula_version": "standard-strategy-analysis/1",
          "registry_sha256": _sha256(registry.path),
          "analysis_plan_sha256": expanded["analysis_plan_sha256"],
          "benchmark_manifest_sha256": _sha256(registry.benchmark_manifest),
          "source_manifests": [item.registration.manifest_sha256 for item in registry.sources],
      })
      workspace = root / ".local" / "standard-strategy-analysis" / analysis_id
      return _run_standard_analysis_to_workspace(workspace, registry, expanded, scenarios)
  ```

  `_run_standard_analysis_to_workspace`（运行到工作区）必须复用 `calculate_return_metrics`、`align_three_way_benchmarks`、`_fixed_and_rolling`、`_bootstrap`、`_historical_stress`、`_position_shocks` 和 `_cvar`，不复制算法。它应将每个登记来源的 `source_type`、清单摘要、`snapshot_id` 和 `capabilities` 写入 `source-results.json` 与返回摘要。

- [ ] **Step 4: 将依赖能力的检查转换为明确的证据不足行**

  保持只使用四张共同表的检查运行：收益、回撤、双基准、固定期、滚动期、区块抽样、历史压力、普通持仓冲击和 CVaR（条件风险价值）。以下路径必须由能力门禁包装：

  ```python
  def _unavailable_result(
      scenario_id: str, dimension: str, reason: str
  ) -> tuple[dict[str, object], ScenarioResult]:
      row = {
          "scenario_id": scenario_id,
          "dimension": dimension,
          "status": "evidence_insufficient",
          "reasons": [reason],
          "metrics": {},
      }
      return row, ScenarioResult(
          scenario_id=scenario_id,
          dimension=dimension,
          status="evidence_insufficient",
          metrics={},
          input_sha256=evidence_digest(row),
          reasons=(reason,),
      )
  ```

  - `_deletion_sensitivity`（删除敏感性）在基线归因不可用时为每个删除场景返回 `attribution_missing_at_source`。
  - `_cost_sensitivity`（成本敏感性）在 `cost_execution` 能力不是 `available` 时为每个成本定义返回 `market_snapshot_missing_at_source`，不得调用 `_market_open_lookup`。
  - `_position_shocks` 的 `use_stop_failure_loss=true` 保持现有 `stop_failure_loss_missing_at_source`；其他冲击继续使用共同持仓表。
  - 跨场景比较在登记只有一个场景时写一条 `cross_scenario` / `single_registered_source` 的证据不足行，而不是阻断 bootstrap（区块抽样）和 CVaR（条件风险价值）。

- [ ] **Step 5: 完成聚合规则与回归测试**

  `pre_vibe_recommendation`（Vibe 前推荐）和标准输出必须将 `fail` 与 `evidence_insufficient` 分别计数；只有基线通过、严重维度无失败且证据不足计数为零时才能返回 `recommend_joinquant_confirmation`（建议聚宽确认），否则返回 `revise_before_joinquant`（修改后再评估）。

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_unified_analysis.py -q
  ```

  Expected: PASS；现有本地确定性分析回归保持通过，新标准入口覆盖单来源、多来源、失败和证据不足并存。

- [ ] **Step 6: 提交本任务**

  ```powershell
  git add scripts/research/quant_analysis/unified_analysis.py scripts/research/quant_analysis/evidence.py tests/quant_analysis/test_unified_analysis.py
  git commit -m "feat: 增加能力降级的稳健性分析"
  ```

### Task 5: 交付标准报告、命令行入口与唯一 Skill

**Files:**

- Modify: `scripts/research/quant_analysis/unified_analysis.py:1438-1490`
- Modify: `scripts/research/quant_analysis/reporting.py:135-760`
- Modify: `tests/quant_analysis/test_reporting.py`
- Modify: `tests/test_skill_layout.py`
- Create: `.agents/skills/analyze-quant-robustness/SKILL.md`
- Create: `.agents/skills/analyze-quant-robustness/agents/openai.yaml`
- Create: `.claude/skills/analyze-quant-robustness` (relative symbolic link)

**Interfaces:**

- Consumes: `run_standard_analysis(repo_root, source_registry_path)` 生成的 `.local/standard-strategy-analysis/<analysis_id>/deterministic-analysis.json` 与 `source-results.json`。
- Produces: `write_analysis_delivery(workspace: Path) -> dict[str, Any]` 支持标准输出并写入 `standard-strategy-analysis-report.md` 与 `recommendation.json`。
- Produces: 仅供 `analyze-quant-robustness` Skill（技能）调用的内部命令 `python -m scripts.research.quant_analysis.unified_analysis --repo-root <repo> --source-registry <relative-json>`；成功时打印一行 JSON（结构化数据）状态。

- [ ] **Step 1: 为命令行和报告可追溯性写失败测试**

  ```python
  def test_standard_report_lists_source_identity_and_capabilities(tmp_path: Path) -> None:
      analysis = _standard_analysis_fixture(
          source_type="joinquant_simulation",
          snapshot_id="snapshot-1",
          attribution_status="missing_at_source",
      )
      report = render_analysis_report(analysis, build_recommendation(analysis), {})
      assert "聚宽模拟交易" in report
      assert "snapshot-1" in report
      assert "证据不足" in report


  def test_cli_requires_one_explicit_source_registry(repo_root: Path) -> None:
      completed = subprocess.run(
          [
              str(repo_root / ".venv/Scripts/python.exe"),
              "-m", "scripts.research.quant_analysis.unified_analysis",
              "--repo-root", str(repo_root),
          ],
          cwd=repo_root, capture_output=True, text=True, shell=False, check=False,
      )
      assert completed.returncode == 2
      assert "--source-registry" in completed.stderr
  ```

- [ ] **Step 2: 运行测试并确认当前入口仍要求旧参数**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_reporting.py -k standard tests\quant_analysis\test_unified_analysis.py -k source_registry -q
  ```

  Expected: FAIL，因为报告只陈述本地探索性研究，CLI（命令行接口）只支持 `--preparation-workspace` 和重复 `--source`。

- [ ] **Step 3: 扩展 CLI（命令行接口）且保持旧入口兼容**

  将参数拆为互斥调用模式：旧模式仍需要 `--preparation-workspace` 和至少一个 `--source`；新模式只接受一个仓库相对 `--source-registry`。禁止新模式接受 `--source`，避免退回运行目录猜测。

  ```python
  def _parser() -> argparse.ArgumentParser:
      parser = argparse.ArgumentParser(description="Run read-only standard strategy analysis")
      parser.add_argument("--repo-root", type=Path, default=Path.cwd())
      mode = parser.add_mutually_exclusive_group(required=True)
      mode.add_argument("--source-registry", type=Path)
      mode.add_argument("--preparation-workspace", type=Path)
      parser.add_argument("--source", action="append", metavar="SCENARIO_ID=RUN_ID")
      return parser


  def main(argv: list[str] | None = None) -> int:
      args = _parser().parse_args(argv)
      if args.source_registry is not None:
          if args.source:
              raise UnifiedAnalysisError("--source cannot be used with --source-registry")
          result = run_standard_analysis(args.repo_root, args.source_registry)
      else:
          if not args.source:
              raise UnifiedAnalysisError("--source is required with --preparation-workspace")
          result = run_deterministic_analysis(
              args.repo_root, args.preparation_workspace, _parse_source_registry(args.source)
          )
      print(json.dumps(_cli_summary(result), ensure_ascii=False, sort_keys=True))
      return 0
  ```

- [ ] **Step 4: 报告来源能力与三态证据，不误称正式裁判**

  在 `render_analysis_report` 开头基于 `analysis["schema_version"]` 选择标题；标准版本必须显示来源表、每行的类型/清单 SHA256（完整性摘要）/模拟快照/归因状态，以及证据矩阵的通过、失败、证据不足计数。`build_recommendation` 对标准版本把权限写为 `read_only_analysis`（只读分析），并保留“不是 JoinQuant（聚宽）正式回测、模拟交易或最终验收”的文字。

  ```python
  def _source_provenance_table(analysis: Mapping[str, Any]) -> list[str]:
      rows = []
      for source in _rows(_mapping(analysis.get("sources")).get("registered")):
          rows.append([
              str(source.get("scenario_id", "")),
              str(source.get("source_type", "")),
              str(source.get("manifest_sha256", "")),
              str(source.get("snapshot_id") or "—"),
              str(_mapping(source.get("capabilities")).get("attribution", {}).get("status", "")),
          ])
      return _table(["场景", "来源", "清单摘要", "模拟快照", "深度归因"], rows)
  ```

  `write_analysis_delivery` 必须在标准版本读取 `deterministic-analysis.json` 后写入 `standard-strategy-analysis-report.md`、`recommendation.json` 和既有 `vibe-evidence.json`；不能写回任何登记来源。

- [ ] **Step 5: 创建最小公开 Skill（技能）和布局测试**

  `SKILL.md` 是唯一公开流程入口，只声明输入、内部命令、三态停止条件与只读边界；调用者不得绕过该 Skill（技能）直接编排 Python（编程语言）参数。它不保存数据，也不调用聚宽认证或同步。其内部命令为：

  ```powershell
  .\.venv\Scripts\python.exe -m scripts.research.quant_analysis.unified_analysis `
    --repo-root . `
    --source-registry <仓库相对来源登记.json>

  .\.venv\Scripts\python.exe -m scripts.research.quant_analysis.reporting `
    --workspace .local/standard-strategy-analysis/<analysis_id>
  ```

  `agents/openai.yaml` 使用：

  ```yaml
  interface:
    display_name: "标准策略分析"
    short_description: "只读分析已登记的本地研究、聚宽回测和聚宽模拟交易快照"
    default_prompt: "使用 $analyze-quant-robustness 对明确来源登记执行只读标准分析并核验报告。"
  ```

  在 `tests/test_skill_layout.py` 添加新 Skill（技能）断言，按现有三个 Skill（技能）的同一结构验证真实目录、`openai.yaml`、相对符号链接解析结果和两侧 `SKILL.md` 摘要相等。

- [ ] **Step 6: 运行报告、入口与布局回归**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_reporting.py tests\quant_analysis\test_unified_analysis.py tests\test_skill_layout.py -q
  ```

  Expected: PASS；旧本地报告继续可用，标准报告包含来源能力与快照，CLI（命令行接口）不接受隐式来源。

- [ ] **Step 7: 提交本任务**

  ```powershell
  git add scripts/research/quant_analysis/unified_analysis.py scripts/research/quant_analysis/reporting.py tests/quant_analysis/test_reporting.py tests/test_skill_layout.py .agents/skills/analyze-quant-robustness .claude/skills/analyze-quant-robustness
  git commit -m "feat: 发布标准策略分析入口"
  ```

### Task 6: 通过真实 Skill 入口完成离线端到端回归与完整验证

**Files:**

- Create: `tests/quant_analysis/test_standard_analysis_e2e.py`
- Modify: `openspec/changes/build-turtle-etf-robustness-analysis-workflow/tasks.md`

**Interfaces:**

- Consumes: 已创建的 `analyze-quant-robustness` Skill（技能）、三个固定测试来源、显式来源登记和项目 `.venv`。
- Produces: 可重复的离线 E2E（端到端）测试，证明没有网络访问、没有上游调用、所有来源摘要未改变且同时产生 JSON（结构化数据）与 Markdown（标记文档）交付。

- [ ] **Step 1: 写真实 Skill（技能）入口的失败 E2E（端到端）测试**

  在临时仓库目录放入三个最小来源和一份已知的分析计划/基准清单；使用 `subprocess.run(command, shell=False)` 调用 Task 5 的两个公开命令。对来源目录先后计算树摘要。

  ```python
  def test_standard_analysis_skill_is_offline_and_preserves_three_sources(
      repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      fixture = _prepare_three_source_fixture(repo_root, tmp_path)
      before = {name: _tree_sha(path) for name, path in fixture.sources.items()}
      monkeypatch.setattr(socket, "socket", _forbid_network)

      analysis = _run_skill_command(repo_root, fixture.registry)
      workspace = repo_root / ".local/standard-strategy-analysis" / analysis["analysis_id"]
      delivery = _run_report_command(repo_root, workspace)

      assert analysis["status"] == "complete"
      assert delivery["status"] == "complete"
      assert (workspace / "deterministic-analysis.json").is_file()
      assert (workspace / "standard-strategy-analysis-report.md").is_file()
      assert {name: _tree_sha(path) for name, path in fixture.sources.items()} == before
      assert {row["status"] for row in analysis["evidence_rows"]} >= {"pass", "evidence_insufficient"}
  ```

- [ ] **Step 2: 运行 E2E（端到端）测试并确认当前发布入口不存在**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_standard_analysis_e2e.py -q
  ```

  Expected: FAIL，直到 Task 5 的 Skill（技能）入口、标准注册表和报告文件全部存在。

- [ ] **Step 3: 固化三类来源、缺失归因与多场景通过覆盖**

  `_prepare_three_source_fixture`（准备三来源样例）必须复用现有本地结果包测试帮助函数和仓库内小型聚宽回测/模拟归档；它不能下载、同步或创建 `.local` 以外的运行产物。测试至少覆盖：

  ```python
  assert source_types == {
      "local_research", "joinquant_backtest", "joinquant_simulation"
  }
  assert result["attribution"]["status"] == "evidence_insufficient"
  assert result["robustness"]["bootstrap"][0]["status"] == "pass"
  assert result["evidence_matrix"]["evidence_insufficient"] >= 1
  ```

  若真实归档日期不足以满足 `historical_stress`（历史压力）定义，样例分析计划必须使用其共同日期范围；对 CVaR（条件风险价值）保留足够尾部样本的通过定义。不要篡改归档或放宽来源验证。

- [ ] **Step 4: 运行全部定向回归与格式检查**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_analysis_data_views.py tests\quant_analysis\test_source_registry.py tests\quant_analysis\test_unified_analysis.py tests\quant_analysis\test_reporting.py tests\quant_analysis\test_standard_analysis_e2e.py tests\test_skill_layout.py -q
  git diff --check
  ```

  Expected: 全部 PASS，`git diff --check` 无输出。

- [ ] **Step 5: 标记已完成的 OpenSpec（开放规范）任务并提交**

  仅在 Step 4 成功后，将当前 change 的 `tasks.md` 中 1.1–5.3 标为完成；5.4 仍等待完整验证确认。然后提交：

  ```powershell
  git add tests/quant_analysis/test_standard_analysis_e2e.py openspec/changes/build-turtle-etf-robustness-analysis-workflow/tasks.md
  git commit -m "test: 覆盖标准策略分析端到端流程"
  ```

- [ ] **Step 6: 请求完整验证授权并运行完整验证**

  向用户说明 `build-and-verify:build-and-verify`（构建与验证）完整模式会运行仓库所有检查，取得本次明确确认后执行：

  ```powershell
  .\.venv\Scripts\python.exe C:\Users\liuli\.codex\plugins\cache\my-agent-skills-marketplace\build-and-verify\0.1.44\scripts\build_and_verify.py verify --project . --full
  ```

  Expected: 完整验证通过。若出现测试、构建或运行异常，先加载 `superpowers:systematic-debugging`（系统化调试）技能，完成根因调查后再修改代码。

- [ ] **Step 7: 记录完整验证并完成最后提交**

  将完整验证命令、日期、结果和无法覆盖项写入现有 `tasks.md` 的 5.4 下方；仅当完整验证通过时勾选 5.4，然后提交。

  ```powershell
  git add openspec/changes/build-turtle-etf-robustness-analysis-workflow/tasks.md
  git commit -m "test: 完成标准策略分析完整验证"
  ```

## 自查结果

- **规格覆盖：** Task 1–2 覆盖显式来源、摘要和模拟快照；Task 3 覆盖共同事实和两种归因入口；Task 4 覆盖深度归因、所有既有稳健性算法、单来源与三态聚合；Task 5–6 覆盖 JSON（结构化数据）、Markdown（标记文档）、Skill（技能）、只读边界和真实入口 E2E（端到端）。
- **占位符扫描：** 已逐项扫描并确认计划没有任何未落实步骤、代码省略或模糊错误处理要求。
- **接口一致性：** Task 1 的 `snapshot_id` 由 Task 2 的 `SourceRegistration` 提供；Task 2 的 `RegisteredSource` 被 Task 3 消费；Task 3 的 `ScenarioInput.capabilities` 被 Task 4 门禁消费；Task 4 的标准 JSON（结构化数据）被 Task 5 的报告和 Task 6 的 E2E（端到端）读取。
