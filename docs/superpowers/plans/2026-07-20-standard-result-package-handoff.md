# 标准结果包交接精简实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 量化分析只读取显式给出的标准结果包、分析计划和独立基准，不再使用来源登记、路径语义或生产者类型。

**Architecture:** 复用现有结果包校验和 `analysis_data`（分析数据）只读视图。结果包内的策略、场景、配置和内容摘要是唯一来源身份；调用路径只用于打开文件，不进入分析身份。

**Tech Stack:** Python（编程语言）3.12、PyArrow（列式数据处理）、DuckDB（嵌入式分析数据库）、pytest（测试框架）。

## Global Constraints

- 不修改本地研究执行、海龟策略和当前未提交风险控制改动。
- 不新增登记替代物、工厂、接口层或兼容目录扫描。
- 保留标准结果包完整性、显式输入、防漂移、只读分析和三态证据校验。
- 本轮不提交 Git（版本管理）变更，不进入 PR（拉取请求）流程。

---

### Task 1: 以标准结果包定义公开分析输入

**Files:**
- Modify: `tests/quant_analysis/test_unified_analysis.py`
- Modify: `tests/quant_analysis/test_standard_analysis_e2e.py`
- Modify: `.agents/skills/analyze-quant-robustness/scripts/analyze_quant_robustness.py`
- Modify: `.agents/skills/analyze-quant-robustness/scripts/quant_analysis/unified_analysis.py`

**Interfaces:**
- Produces: `run_standard_analysis(repo_root, package_paths, analysis_plan_path, benchmark_manifest_path)`。
- CLI（命令行接口）接受可重复 `--package`、一个 `--analysis-plan` 和一个 `--benchmark-manifest`。

- [ ] **Step 1: 写失败测试**

```python
def test_analysis_uses_package_identity_not_location(...):
    first = run_standard_analysis(root, [first_copy], plan, benchmark)
    second = run_standard_analysis(root, [second_copy], plan, benchmark)
    assert first["analysis_id"] == second["analysis_id"]

def test_package_scenario_must_match_plan(...):
    with pytest.raises(UnifiedAnalysisError, match="scenario"):
        run_standard_analysis(root, [mismatched_package], plan, benchmark)
```

- [ ] **Step 2: 运行测试并确认因新接口不存在而失败**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_unified_analysis.py tests\quant_analysis\test_standard_analysis_e2e.py -q
```

- [ ] **Step 3: 实现最小直接包入口**

```python
run.add_argument("--package", type=Path, action="append", required=True)
run.add_argument("--analysis-plan", type=Path, required=True)
run.add_argument("--benchmark-manifest", type=Path, required=True)
```

分析从包内 `object.strategy_id`、`object.scenario_id`、冻结 `config/scenario.json` 和包内容摘要建立身份；拒绝计划不匹配与重复包。

- [ ] **Step 4: 运行定向测试并确认通过**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_unified_analysis.py tests\quant_analysis\test_standard_analysis_e2e.py -q
```

### Task 2: 删除来源登记和真实归因断点

**Files:**
- Delete: `.agents/skills/analyze-quant-robustness/scripts/quant_analysis/source_registry.py`
- Delete: `.agents/skills/analyze-quant-robustness/scripts/quant_analysis/schemas/source-registry.schema.json`
- Delete: `tests/quant_analysis/test_source_registry.py`
- Modify: `.agents/skills/analyze-quant-robustness/scripts/quant_analysis/unified_analysis.py`
- Modify: `tests/quant_analysis/test_unified_analysis.py`

**Interfaces:**
- 结果包能力直接由已验证清单和物理字段派生。
- 本地归因按必需字段识别唯一扩展，不按扩展名或生产者类型识别。

- [ ] **Step 1: 写失败测试**

```python
def test_turtle_extension_is_discovered_by_fields(...):
    scenario = load_package_scenario(turtle_package, expected_params)
    assert len(scenario.events) > 0
    assert "details_json" in scenario.events
```

- [ ] **Step 2: 运行测试并确认当前返回空归因而失败**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\quant_analysis\test_unified_analysis.py -k turtle_extension -q
```

- [ ] **Step 3: 移除登记类型并保留必要能力校验**

删除 `SourceRegistration`、`RegisteredSource`、`SourceRegistry`、登记 Schema（结构约束）和重复声明字段。归因读取保留 `details_json`、`risk_before`、`risk_after` 及结果包实际存在的风险字段。

- [ ] **Step 4: 运行量化分析测试并确认通过**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\quant_analysis -q
```

### Task 3: 删除活动需求和 Skill 中的登记设计

**Files:**
- Modify: `.agents/skills/analyze-quant-robustness/SKILL.md`
- Modify: `.agents/skills/analyze-quant-robustness/agents/openai.yaml`
- Modify: `openspec/specs/standard-strategy-analysis-workflow/spec.md`
- Modify: `openspec/specs/standard-strategy-analysis-data/spec.md`
- Modify: `docs/superpowers/specs/2026-07-19-standard-strategy-analysis-workflow-design.md`
- Modify: `tests/test_skill_layout.py`

**Interfaces:**
- Skill（技能）只描述标准结果包、分析计划、独立基准和只读交付。
- 活动规格不再要求登记、来源类型、仓库内路径、快照字段或生产者分支。

- [ ] **Step 1: 写失败的 Skill 契约断言**

```python
assert "--package" in skill
assert "--source-registry" not in skill
assert "source_registry" not in production_text
```

- [ ] **Step 2: 运行测试并确认旧登记文本导致失败**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_skill_layout.py -q
```

- [ ] **Step 3: 删除活动规则和文档中的镀金需求**

保留历史 `openspec/changes/archive/`（归档变更）作为历史证据；只修改当前有效规格、Skill 和设计说明。

- [ ] **Step 4: 完整验证**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\quant_analysis tests\local_quant_research\test_analysis_data_views.py tests\test_skill_layout.py -q
git diff --check
```

预期：全部通过；生产代码和活动规格中不再存在 `source registry`（来源登记）入口或类型。
