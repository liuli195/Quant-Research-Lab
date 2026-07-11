---
change: add-joinquant-archive-sync
design-doc: docs/superpowers/specs/2026-07-11-joinquant-archive-sync-design.md
base-ref: 1318db74acbe665286a9f137ed9efd95205a5018
archived-with: 2026-07-11-add-joinquant-archive-sync
---

# 聚宽归档与增量同步实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在仓库内实现一套由 Codex（代码代理）、Claude（代码代理）和 Windows Task Scheduler（Windows 任务计划程序）共同调用的 JoinQuant（聚宽）归档 Skill（技能），能按目标增量同步策略、回测和模拟交易，并逐数据集证明代码、结构化数据和日志完整性。

**Architecture:** `.agents/skills/joinquant-archive-sync/` 保存唯一 Skill、Python CLI（命令行入口）和四个职责模块；Browser（浏览器）与 Research（研究接口）证据先进入同一暂存包，再由 archive（归档）模块校验并原子替换 manifest（清单）。原始证据用 gzip，事实表用 Parquet + Zstd，查询使用内存 DuckDB（分析型数据库），常规 E2E（端到端）由 `self-test` 在内存生成小证据完成。

**Tech Stack:** Python 3.12、标准库 `argparse/gzip/hashlib/json/pathlib/tempfile/msvcrt/subprocess`、Playwright、Pandas、PyArrow、DuckDB、pytest、Windows Task Scheduler、Git LFS（大文件存储）。

## Global Constraints

- 正式回测和模拟交易只在聚宽云端运行；本地结果不得冒充正式运行。
- 所有 Python 命令使用 `.\.venv\Scripts\python.exe`。
- 历史回测必须明确给出策略与页面序号或详情 URL；拒绝空目标、`latest` 和隐式全量。
- 归因日志是独立核心数据集；回测只读取目标源码单一路径并关联最终资产，模拟交易完整保存代码历史映射但只读取启动生命周期实际初始化的单一路径；归属不唯一、断序、摘要不符、运行边界冲突、回测最终资产不符或已结束运行缺少 `run_end` 必须阻断门禁。
- 普通日志必须取得全部免费内容；到 1000 条后探测下一页，只有后续不可免费取得或无法证明结束时才用 `capped_free`。
- 积分日志必须指定运行、类型、范围并确认聚宽当次价格；不默认消耗积分。
- 活动模拟交易每天北京时间 04:00 同步，失败每 30 分钟重试，最多 3 次；关闭后最终同步一次。
- 凭证和 Playwright persistent context（持久上下文）只在仓库外，不打印或提交 Cookie、Token 或密码。
- 常规 E2E 使用 `self-test` 的内存证据，不访问网络或历史归档；真实外部链路只由首次 PoC（概念验证）证明。
- 不创建 Plugin（插件）、marketplace（市场）、守护进程、第二套同步逻辑或持久 `.duckdb` 文件。
- 提交步骤只有在用户明确授权 Git 提交后执行；未授权时保留工作树改动并报告。

---

## 文件结构

```text
.agents/skills/joinquant-archive-sync/
├── SKILL.md                         # Codex/Claude 使用入口和安全编排
├── requirements.txt                 # Skill 唯一运行依赖清单
├── scripts/
│   ├── jq_sync.py                   # 唯一 CLI 与调度编排
│   └── joinquant_sync/
│       ├── __init__.py
│       ├── browser.py               # 登录、页面、代码、日志、下载
│       ├── research.py              # 结构化数据分页与校验
│       ├── research_cloud.py        # Research 云端执行与单次原始返回
│       ├── archive.py               # 身份、证据、门禁、增量、原子清单
│       └── query.py                 # Parquet、DuckDB、CSV
└── references/
    ├── manifest.md                  # manifest 字段与状态说明
    └── operations.md                # 认证、PoC、计划任务、恢复步骤

.claude/skills/joinquant-archive-sync
└── SymbolicLink -> ../../.agents/skills/joinquant-archive-sync

tests/
├── conftest.py
├── joinquant_sync/
│   ├── test_archive.py
│   ├── test_browser_research.py
│   ├── test_query.py
│   ├── test_scheduler.py
│   └── test_self_test.py
└── test_skill_layout.py
```

---

### Task 1: 建立最小运行骨架并完成真实 PoC

**Files:**
- Create: `.agents/skills/joinquant-archive-sync/requirements.txt`
- Create: `.agents/skills/joinquant-archive-sync/scripts/jq_sync.py`
- Create: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/__init__.py`
- Create: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/browser.py`
- Create: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/research.py`
- Create: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/archive.py`
- Create: `.agents/skills/joinquant-archive-sync/references/operations.md`
- Create: `docs/research/joinquant-archive-sync-poc.md`
- Create: `tests/conftest.py`
- Create: `tests/joinquant_sync/test_browser_research.py`
- Modify: `requirements.txt`
- Delete: `tests/test_placeholder.py`

**Interfaces:**
- Produces: `build_parser() -> argparse.ArgumentParser`
- Produces: `main(argv: list[str] | None = None) -> int`
- Produces: `open_authenticated_context(profile_dir: Path, headless: bool) -> BrowserContext`
- Produces: `ensure_authenticated(page: Page) -> None`, raising `AuthRequired`
- Produces: `capture_download(page: Page, trigger: Callable[[], None], destination: Path) -> Path`
- Produces: `stage_external_file(source: Path, stage_dir: Path) -> dict[str, object]`
- Produces: `export_structured_backtest(page: Page, target_url: str, stage_dir: Path) -> list[dict[str, object]]`

- [x] **Step 1: 写失败测试，固定认证失效、下载捕获和人工导入摘要契约**

```python
class FakePage:
    url = "https://www.joinquant.com/user/login/index"

def test_login_redirect_is_auth_required():
    with pytest.raises(AuthRequired):
        ensure_authenticated(FakePage())

def test_stage_external_file_preserves_bytes_and_sha256(tmp_path):
    source = tmp_path / "export.json"
    source.write_bytes(b'{"ok":true}')
    item = stage_external_file(source, tmp_path / "stage")
    assert Path(item["path"]).read_bytes() == source.read_bytes()
    assert item["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()

def test_stage_only_sync_requires_explicit_target():
    assert main(["sync-backtest", "--strategy", "strategy-001", "--stage-only", ".local/poc"]) == 2
```

- [x] **Step 2: 运行测试并确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_browser_research.py -v`

Expected: FAIL，因为 `joinquant_sync.browser`、`AuthRequired` 和 `stage_external_file` 尚不存在。

- [x] **Step 3: 写最小骨架和依赖单一来源**

Skill `requirements.txt` 使用以下唯一运行清单；根 `requirements.txt` 只保留 `-r .agents/skills/joinquant-archive-sync/requirements.txt`，避免双份版本漂移：

```text
numpy==2.4.4
pandas==3.0.2
requests
beautifulsoup4
pyyaml==6.0.3
playwright>=1.40,<2
pyarrow>=23.0.1,<24
duckdb>=1,<2
```

`tests/conftest.py` 把 Skill 的 `scripts` 目录加入 `sys.path`，并提供返回仓库根目录的 `repo_root` fixture（夹具）。实现上方接口；Browser 只使用 Playwright，文件复制使用 `shutil.copyfileobj`，摘要使用 `hashlib.sha256`。`jq_sync.py` 在本任务只实现 PoC 需要的 `auth`、`sync-backtest --stage-only` 和 `verify --import-file --stage-only`，其余正式命令由后续任务补齐；`argparse` 必须在进入 Browser/Research 前拒绝缺少 `--target` 的历史同步。

- [x] **Step 4: 运行最小测试并确认通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_browser_research.py -v`

Expected: PASS。

- [x] **Step 5: 用项目 `.venv` 安装新增依赖并确认导入**

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
& .\.venv\Scripts\python.exe -c "import duckdb, playwright, pyarrow; print('runtime-ok')"
```

Expected: 输出 `runtime-ok`。

- [x] **Step 6: 执行真实 PoC，目标必须由环境变量显式提供**

```powershell
if (-not $env:JQ_POC_STRATEGY -or -not $env:JQ_POC_BACKTEST) {
    throw 'Set JQ_POC_STRATEGY and JQ_POC_BACKTEST to one explicit backtest with an attribution writer.'
}
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py auth
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py sync-backtest --strategy $env:JQ_POC_STRATEGY --target $env:JQ_POC_BACKTEST --stage-only .local\joinquant-sync\poc
```

Expected: 已登录页面、Research 导出、官方下载和归因日志均写入 `.local/joinquant-sync/poc`，报告记录文件大小、分页证据和 SHA256。

- [x] **Step 7: 自动下载失败时验证人工导入；两条路径都失败立即停止**

```powershell
if (-not $env:JQ_POC_IMPORT) { throw 'Set JQ_POC_IMPORT to the manually downloaded official file.' }
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py verify --import-file $env:JQ_POC_IMPORT --stage-only .local\joinquant-sync\poc-import
```

Expected: 人工文件进入相同证据包格式并通过摘要校验。若自动路径和人工路径都失败，只更新 `docs/research/joinquant-archive-sync-poc.md` 为 `BLOCKED` 并停止后续 Task。

- [x] **Step 8: 记录可复核证据**

`docs/research/joinquant-archive-sync-poc.md` 只记录目标页面身份、执行时间、文件名、字节数、行数、分页终止证据、SHA256、归因日志校验和自动/人工路径结论，不记录凭证和 Cookie。

- [x] **Step 9: 获得用户 Git 授权后提交**

```powershell
git add requirements.txt .agents/skills/joinquant-archive-sync tests/conftest.py tests/joinquant_sync/test_browser_research.py docs/research/joinquant-archive-sync-poc.md
git rm tests/test_placeholder.py
git commit -m "feat: 验证聚宽归档下载链路"
```

---

### Task 2: 实现稳定身份、目标校验和 manifest 门禁

**Files:**
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/archive.py`
- Modify: `.agents/skills/joinquant-archive-sync/scripts/jq_sync.py`
- Create: `.agents/skills/joinquant-archive-sync/references/manifest.md`
- Create: `tests/joinquant_sync/test_archive.py`

**Interfaces:**
- Produces: `validate_history_target(strategy_id: str | None, target: str | None) -> tuple[str, str]`
- Produces: `resolve_local_id(index_path: Path, kind: str, page_identity: dict[str, str]) -> str`
- Produces: `expected_datasets(kind: str, run_status: str, has_attribution_writer: bool) -> dict[str, dict[str, object]]`
- Produces: `evaluate_gate(datasets: dict[str, dict[str, object]]) -> dict[str, object]`

- [x] **Step 1: 写目标和门禁失败测试**

```python
@pytest.mark.parametrize("target", [None, "", "latest"])
def test_history_target_rejects_implicit_selection(target):
    with pytest.raises(TargetRequired):
        validate_history_target("strategy-001", target)

def test_incomplete_attribution_blocks_gate():
    datasets = expected_datasets("backtest", "done", True)
    datasets["attribution_log"].update(status="failed")
    assert evaluate_gate(datasets)["status"] == "fail"

def test_missing_writer_is_explicit_exception():
    datasets = expected_datasets("backtest", "done", False)
    assert datasets["attribution_log"]["status"] == "missing_at_source"
    assert evaluate_gate(datasets)["status"] == "pass"
```

- [x] **Step 2: 运行测试并确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_archive.py -v`

Expected: FAIL，因为身份、预期数据集和门禁函数尚不存在。

- [x] **Step 3: 实现最小 manifest 模型**

在 `archive.py` 使用 `dataclasses` 和普通字典实现五种状态：`complete`、`capped_free`、`missing_at_source`、`unsupported_api_version`、`failed`。manifest 固定包含 `schema_version/object/source/fence/code/datasets/gate`；失败或取消运行的合法空表使用 `complete + rows: 0 + verified_empty: true`，不使用缺文件表达。

- [x] **Step 4: 实现稳定 ID 和目标拒绝规则**

`strategy_id`、`simulation_id` 由索引首次分配后复用；回测目录使用页面序号；远端 ID 和 URL 只追加到 `aliases`。CLI 在任何下载前调用 `validate_history_target`。

- [x] **Step 5: 运行测试并确认通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_archive.py -v`

Expected: PASS。

- [x] **Step 6: 获得用户 Git 授权后提交**

```powershell
git add .agents/skills/joinquant-archive-sync tests/joinquant_sync/test_archive.py
git commit -m "feat: 建立归档身份与完整性门禁"
```

---

### Task 3: 实现原始证据优先和原子 manifest 提交

**Files:**
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/archive.py`
- Modify: `tests/joinquant_sync/test_archive.py`

**Interfaces:**
- Produces: `write_raw_gzip(raw: bytes, destination: Path) -> dict[str, object]`
- Produces: `commit_manifest(object_dir: Path, manifest: dict[str, object], staged_files: list[Path]) -> None`
- Produces: `verify_existing_manifest(object_dir: Path) -> dict[str, object]`

- [x] **Step 1: 写失败批次不覆盖和重复同步测试**

```python
def test_failed_batch_keeps_previous_manifest(tmp_path):
    object_dir = tmp_path / "backtests" / "1"
    object_dir.mkdir(parents=True)
    old = {"schema_version": 1, "gate": {"status": "pass"}, "datasets": {}}
    (object_dir / "manifest.json").write_text(json.dumps(old), encoding="utf-8")
    with pytest.raises(IntegrityError):
        commit_manifest(object_dir, {"gate": {"status": "fail"}}, [])
    assert json.loads((object_dir / "manifest.json").read_text()) == old

def test_raw_response_round_trips_and_hashes(tmp_path):
    result = write_raw_gzip(b'{"x":1}', tmp_path / "raw.json.gz")
    assert gzip.decompress((tmp_path / "raw.json.gz").read_bytes()) == b'{"x":1}'
    assert result["sha256"] == hashlib.sha256(b'{"x":1}').hexdigest()
```

- [x] **Step 2: 运行测试并确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_archive.py -v`

Expected: FAIL，新接口不存在。

- [x] **Step 3: 实现不可变文件和原子指针**

暂存目录使用 `.local/joinquant-sync/{uuid.uuid4().hex}`；原始和 Parquet 文件以内容摘要或不可变游标命名。校验通过后移动新文件，最后写 `manifest.json.tmp` 并用 `os.replace` 替换。读取端只枚举 manifest 引用文件；崩溃产生的未引用文件不改变上次完整视图。

- [x] **Step 4: 实现对象级文件锁**

使用 Windows 标准库 `msvcrt.locking` 锁住对象 `.sync.lock` 的一个字节；不增加第三方锁库。锁冲突返回可重试错误，不并发修改同一 manifest。

- [x] **Step 5: 运行测试并确认通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_archive.py -v`

Expected: PASS。

- [x] **Step 6: 获得用户 Git 授权后提交**

```powershell
git add .agents/skills/joinquant-archive-sync/scripts/joinquant_sync/archive.py tests/joinquant_sync/test_archive.py
git commit -m "feat: 原子提交聚宽归档清单"
```

---

### Task 4: 实现 Research 分页、运行状态和前后清单围栏

**Files:**
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/research.py`
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/archive.py`
- Modify: `.agents/skills/joinquant-archive-sync/scripts/jq_sync.py`
- Modify: `tests/joinquant_sync/test_browser_research.py`

**Interfaces:**
- Produces: `collect_pages(fetch_page: Callable[[str | None], dict[str, object]]) -> tuple[list[dict[str, object]], dict[str, object]]`
- Produces: `validate_fact_table(name: str, rows: list[dict[str, object]], run_status: str, pagination: dict[str, object]) -> dict[str, object]`
- Produces: `sync_with_fence(read_inventory: Callable[[], dict[str, object]], collect: Callable[[], object]) -> object`

- [x] **Step 1: 写分页结束、合法空结果和清单漂移测试**

```python
def test_full_page_without_end_evidence_is_not_complete():
    pages = iter([{"rows": [{"id": 1}], "next": None, "page_full": True}])
    with pytest.raises(PaginationIncomplete):
        collect_pages(lambda _cursor: next(pages))

def test_failed_run_accepts_verified_empty_table():
    result = validate_fact_table("risk", [], "failed", {"end": "empty_page"})
    assert result["status"] == "complete"
    assert result["verified_empty"] is True

def test_inventory_drift_blocks_second_unstable_batch():
    inventories = iter([{"rev": 1}, {"rev": 2}, {"rev": 2}, {"rev": 3}])
    with pytest.raises(InventoryChanged):
        sync_with_fence(lambda: next(inventories), lambda: object())
```

- [x] **Step 2: 运行测试并确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_browser_research.py -v`

Expected: FAIL，新分页和围栏接口不存在。

- [x] **Step 3: 实现分页和事实表校验**

结果、资金、持仓、订单、自定义记录、风险和分期风险分别保存页证据；校验字段、唯一键、排序、时间范围、交易日关联、总数和结束信号。满页但无空页、总数或结束游标时抛出 `PaginationIncomplete`。

- [x] **Step 4: 实现前后清单围栏和变化部分重取**

第一次前后清单不一致时只重取变化数据集；第二次仍不一致时保留暂存证据但不提交 manifest。

- [x] **Step 5: 保存策略、回测和模拟交易的完整代码上下文**

策略写 `default_code.py`；`sync-backtest` 保存完整 `code.py`、参数、数据和报告；模拟交易保存来源回测、当前代码、全部代码版本、参数和快照。每份代码写入 SHA256，失败或取消运行也不得省略代码目录。可行性复核确认聚宽没有独立构建页面对象，因此不实现虚构的 `sync-build`。

- [x] **Step 6: 运行测试并确认通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_browser_research.py -v`

Expected: PASS。

- [x] **Step 7: 获得用户 Git 授权后提交**

```powershell
git add .agents/skills/joinquant-archive-sync tests/joinquant_sync/test_browser_research.py
git commit -m "feat: 校验聚宽结构化数据分页"
```

---

### Task 5: 实现归因日志、普通日志 1000 条边界和积分确认

**Files:**
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/browser.py`
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/archive.py`
- Modify: `.agents/skills/joinquant-archive-sync/scripts/jq_sync.py`
- Modify: `tests/joinquant_sync/test_archive.py`
- Modify: `tests/joinquant_sync/test_browser_research.py`

**Interfaces:**
- Produces: `validate_attribution(lines: Iterable[bytes], run_status: str, writer_present: bool) -> dict[str, object]`
- Produces: `collect_free_logs(fetch_page: Callable[[int], dict[str, object]]) -> tuple[list[dict[str, object]], str]`
- Produces: `recover_malformed_json(raw: bytes) -> tuple[list[dict[str, object]], list[dict[str, object]]]`
- Produces: `create_paid_preview(run_id: str, log_type: str, range_: str, quote: dict[str, object]) -> dict[str, object]`

- [x] **Step 1: 写归因核心门禁失败测试**

```python
def test_attribution_requires_contiguous_sequence_and_run_end():
    lines = [
        b'{"token":"t","seq":1,"event":"run_start"}',
        b'{"token":"t","seq":3,"event":"run_end"}',
    ]
    with pytest.raises(AttributionIncomplete):
        validate_attribution(lines, "done", True)

def test_active_simulation_may_lack_run_end():
    lines = [b'{"token":"t","seq":1,"event":"run_start"}']
    assert validate_attribution(lines, "active", True)["status"] == "complete"
```

- [x] **Step 2: 写普通日志边界测试**

```python
def make_log_fetcher(count: int, probe: str):
    rows = [{"seq": index} for index in range(count)]
    def fetch(offset: int):
        if offset < len(rows):
            return {"rows": rows[offset:offset + 1000], "end": False}
        if probe == "empty":
            return {"rows": [], "end": True}
        if probe == "free":
            return {"rows": rows[offset:], "end": True}
        return {"rows": [], "end": False, "blocked_free": True}
    return fetch

@pytest.mark.parametrize(
    ("count", "probe", "expected"),
    [(999, "empty", "complete"), (1000, "empty", "complete"), (1000, "blocked", "capped_free")],
)
def test_free_log_boundary(count, probe, expected):
    fetch = make_log_fetcher(count=count, probe=probe)
    _, status = collect_free_logs(fetch)
    assert status == expected

def test_free_page_after_1000_continues():
    rows, status = collect_free_logs(make_log_fetcher(count=1001, probe="free"))
    assert len(rows) == 1001
    assert status == "complete"
```

- [x] **Step 3: 运行测试并确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_archive.py tests/joinquant_sync/test_browser_research.py -v`

Expected: FAIL，日志接口不存在。

- [x] **Step 4: 实现 raw-first（原始优先）日志管道**

所有响应先调用 `write_raw_gzip`。回测归因日志校验单源 Token、序号、唯一运行边界和最终资产关联；模拟交易逐代码版本保存源 ID 到完整代码文件映射，但只读取启动生命周期实际初始化的单一路径。畸形 JSON 只恢复可明确分割的记录，并把错误偏移和恢复数量写入 manifest。

- [x] **Step 5: 实现免费边界和积分 preview/download 两阶段**

普通日志达到 1000 条后必须探测 1001；免费可取继续，明确空页/可信总数则 `complete`，其余才 `capped_free`。积分预览生成一次性 `preview_id`，下载要求同一运行、类型、范围、价格摘要和显式 `--confirm`。

- [x] **Step 6: 运行测试并确认通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_archive.py tests/joinquant_sync/test_browser_research.py -v`

Expected: PASS。

- [x] **Step 7: 获得用户 Git 授权后提交**

```powershell
git add .agents/skills/joinquant-archive-sync tests/joinquant_sync
git commit -m "feat: 完整校验聚宽日志"
```

---

### Task 6: 实现 Parquet、DuckDB 查询、CSV 和 Git LFS 恢复证明

**Files:**
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/query.py`
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/archive.py`
- Modify: `.agents/skills/joinquant-archive-sync/scripts/jq_sync.py`
- Create: `tests/joinquant_sync/test_query.py`

**Interfaces:**
- Produces: `write_parquet(rows: Iterable[dict[str, object]], destination: Path) -> dict[str, object]`
- Produces: `open_views(manifest_path: Path, connection: duckdb.DuckDBPyConnection) -> list[str]`
- Produces: `export_csv(manifest_path: Path, dataset: str, fields: list[str], start: str | None, end: str | None, destination: Path) -> dict[str, object]`

- [x] **Step 1: 写 Parquet/manifest 行数一致和按需 CSV 测试**

```python
def archive_with_rows(tmp_path, dataset, rows):
    data_path = tmp_path / f"{dataset}.parquet"
    file_record = write_parquet(rows, data_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "datasets": {dataset: {"status": "complete", "rows": len(rows), "files": [file_record]}}
    }), encoding="utf-8")
    return manifest

def test_duckdb_view_matches_manifest_rows(tmp_path):
    manifest = archive_with_rows(tmp_path, "orders", [{"id": 1}, {"id": 2}])
    con = duckdb.connect(":memory:")
    open_views(manifest, con)
    assert con.execute("select count(*) from orders").fetchone()[0] == 2

def test_csv_exports_only_requested_columns_and_range(tmp_path):
    manifest = archive_with_rows(tmp_path, "orders", [
        {"id": 1, "time": "2026-01-01", "price": 10.0},
        {"id": 2, "time": "2026-01-02", "price": 11.0},
    ])
    result = export_csv(manifest, "orders", ["id", "time"], "2026-01-02", "2026-01-02", tmp_path / "out.csv")
    assert result["filters"]["start"] == "2026-01-02"
    assert pd.read_csv(tmp_path / "out.csv").columns.tolist() == ["id", "time"]
```

- [x] **Step 2: 运行测试并确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_query.py -v`

Expected: FAIL，查询接口不存在。

- [x] **Step 3: 实现紧凑存储和内存视图**

PyArrow 写 `compression="zstd"` 的 Parquet；DuckDB 只对 manifest 引用路径创建临时 view；CSV 必须提供对象、数据集、字段和时间范围并记录来源 SHA256 与过滤条件。不得写 `.duckdb` 文件。

- [x] **Step 4: 运行测试并确认通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_query.py -v`

Expected: PASS。

- [x] **Step 5: 验证现有 `.gitattributes` 已覆盖归档格式**

```powershell
git check-attr filter -- joinquant/sample/raw/page.json.gz joinquant/sample/data/orders.parquet
git lfs env
```

Expected: 两个示例路径均返回 `filter: lfs`，`git lfs env` 成功。首次真实文件提交后，在临时干净检出中运行 `git lfs pull` 并逐文件复核 SHA256；无法恢复则阻断交付。

- [x] **Step 6: 获得用户 Git 授权后提交**

```powershell
git add .agents/skills/joinquant-archive-sync tests/joinquant_sync/test_query.py
git commit -m "feat: 提供紧凑查询与按需导出"
```

---

### Task 7: 实现模拟交易增量和关闭终态

**Files:**
- Modify: `.agents/skills/joinquant-archive-sync/scripts/jq_sync.py`
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/archive.py`
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/browser.py`
- Modify: `tests/joinquant_sync/test_archive.py`

**Interfaces:**
- Produces: `sync_active_simulations(simulations: Iterable[dict[str, object]], fetch_remote: Callable[[dict[str, object]], dict[str, object]]) -> list[dict[str, object]]`
- Produces: `next_increment(manifest: dict[str, object], remote: dict[str, object]) -> dict[str, object]`
- Produces: `finalize_closed_simulation(manifest: dict[str, object], remote: dict[str, object]) -> dict[str, object]`

- [x] **Step 1: 写独立游标、次日补齐和关闭最终同步测试**

```python
def test_simulations_advance_independent_cursors():
    simulations = [{"id": "sim-1"}, {"id": "sim-2"}]
    def fetch_remote(item):
        if item["id"] == "sim-2":
            raise ConnectionError("offline")
        return {"status": "active", "cursor": "2026-07-11T04:00:00+08:00"}
    results = sync_active_simulations(simulations, fetch_remote)
    assert results[0]["committed"] is True
    assert results[1]["committed"] is False

def test_closed_simulation_requires_one_final_sync():
    manifest = {"object": {"status": "active"}, "tracking": "active"}
    remote = {"status": "closed", "attribution": [{"event": "run_start"}, {"event": "run_end"}]}
    result = finalize_closed_simulation(manifest, remote)
    assert result["tracking"] == "stopped"
    assert result["final_sync"] == "complete"
```

- [x] **Step 2: 运行测试并确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_archive.py -v`

Expected: FAIL，模拟交易增量接口不存在。

- [x] **Step 3: 实现不可变日期/游标分片**

代码版本、快照、数据和日志各自保存最后已验证游标；每个模拟交易单独执行门禁和 manifest 提交。重试耗尽只记录失败，次日继续使用未变化的已验证游标。

- [x] **Step 4: 实现关闭终态**

远端由 active 变 closed 时执行一次最终同步；存在归因写入器时要求 `run_end`；成功后从活动索引移除，历史目录不删除。

- [x] **Step 5: 运行测试并确认通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_archive.py -v`

Expected: PASS。

- [x] **Step 6: 获得用户 Git 授权后提交**

```powershell
git add .agents/skills/joinquant-archive-sync tests/joinquant_sync/test_archive.py
git commit -m "feat: 增量归档聚宽模拟交易"
```

---

### Task 8: 实现北京时间计划任务和重试

**Files:**
- Modify: `.agents/skills/joinquant-archive-sync/scripts/jq_sync.py`
- Create: `tests/joinquant_sync/test_scheduler.py`

**Interfaces:**
- Produces: `scheduler_xml(python_exe: Path, cli: Path, task_name: str) -> str`
- Produces: `install_scheduler(task_name: str, command: list[str]) -> None`
- Produces: `scheduler_status(task_name: str) -> dict[str, object]`
- Produces: `uninstall_scheduler(task_name: str) -> None`
- Produces: `self_test_command(repo_root: Path) -> list[str]`
- Produces: `wait_for_task_result(task_name: str, timeout_seconds: int) -> int`

- [x] **Step 1: 写时区、04:00 和重试 XML 测试**

```python
def test_scheduler_xml_uses_beijing_0400_and_three_retries():
    xml = scheduler_xml(Path("python.exe"), Path("jq_sync.py"), "JoinQuantArchiveSync")
    assert "T04:00:00" in xml
    assert "<Interval>PT30M</Interval>" in xml
    assert "<Count>3</Count>" in xml
    assert "sync-active-simulations" in xml

def test_install_rejects_non_china_timezone(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "Pacific Standard Time")
    with pytest.raises(TimezoneError):
        install_scheduler("JoinQuantArchiveSync", ["python.exe", "jq_sync.py"])
```

- [x] **Step 2: 运行测试并确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_scheduler.py -v`

Expected: FAIL，计划任务接口不存在。

- [x] **Step 3: 使用标准库生成并安装任务 XML**

用 `xml.etree.ElementTree` 生成每日 04:00、`RestartOnFailure/PT30M/3` 的任务；安装前执行 `tzutil /g` 并要求 `China Standard Time`。Action（动作）必须指向仓库 `.venv\Scripts\python.exe` 和 Skill 内 `jq_sync.py sync-active-simulations`，不依赖 Codex 会话。

- [x] **Step 4: 运行测试并确认通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_scheduler.py -v`

Expected: PASS。

- [x] **Step 5: 获得用户 Git 授权后提交**

```powershell
git add .agents/skills/joinquant-archive-sync/scripts/jq_sync.py tests/joinquant_sync/test_scheduler.py
git commit -m "feat: 安排聚宽每日增量同步"
```

---

### Task 9: 完成仓库 Skill、Claude 符号链接和操作说明

**Files:**
- Create: `.agents/skills/joinquant-archive-sync/SKILL.md`
- Modify: `.agents/skills/joinquant-archive-sync/references/manifest.md`
- Modify: `.agents/skills/joinquant-archive-sync/references/operations.md`
- Create: `.claude/skills/joinquant-archive-sync` (SymbolicLink)
- Create: `tests/test_skill_layout.py`

**Interfaces:**
- Produces: Codex 调用 `$joinquant-archive-sync`
- Produces: Claude 调用 `/joinquant-archive-sync`
- Consumes: `jq_sync.py` 全部稳定命令

- [x] **Step 1: 写布局和同源摘要失败测试**

```python
def test_claude_skill_resolves_to_agents_skill(repo_root):
    source = repo_root / ".agents/skills/joinquant-archive-sync"
    claude = repo_root / ".claude/skills/joinquant-archive-sync"
    assert claude.resolve() == source.resolve()

def test_skill_contains_no_plugin_manifest(repo_root):
    assert not list(repo_root.glob("**/.codex-plugin/plugin.json"))
    assert not list(repo_root.glob("**/.claude-plugin/plugin.json"))
```

- [x] **Step 2: 运行测试并确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_skill_layout.py -v`

Expected: FAIL，SKILL.md 和 Claude 符号链接尚未创建。

- [x] **Step 3: 编写最小 SKILL.md**

SKILL 只描述：明确目标校验、认证、指定回测同步、模拟交易同步、按需积分日志、查询、CSV、计划任务、`self-test` 和状态解释；所有动作调用 `scripts/jq_sync.py`，不得内嵌抓取逻辑。操作说明覆盖 `auth_required`、`capped_free`、`missing_at_source`、重试耗尽和禁止提交凭证。

- [x] **Step 4: 创建相对目录符号链接**

```powershell
New-Item -ItemType Directory -Force .claude\skills | Out-Null
New-Item -ItemType SymbolicLink -Path .claude\skills\joinquant-archive-sync -Target ..\..\.agents\skills\joinquant-archive-sync
```

Expected: `Get-Item .claude\skills\joinquant-archive-sync` 显示 `LinkType: SymbolicLink`，目标为同仓库 Skill。

- [x] **Step 5: 运行布局测试并确认通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_skill_layout.py -v`

Expected: PASS，且两端 `SKILL.md` 和脚本 SHA256 相同。

- [x] **Step 6: 获得用户 Git 授权后提交**

```powershell
git add .agents/skills/joinquant-archive-sync .claude/skills/joinquant-archive-sync tests/test_skill_layout.py
git commit -m "feat: 提供双代理共用聚宽同步技能"
```

---

### Task 10: 实现全内存 self-test 和发布入口 E2E

**Files:**
- Modify: `.agents/skills/joinquant-archive-sync/scripts/jq_sync.py`
- Create: `tests/joinquant_sync/test_self_test.py`
- Modify: `.build-and-verify/config.json`

**Interfaces:**
- Produces: `run_self_test() -> dict[str, object]`
- Consumes: 生产的 archive、research、query 和 scheduler 函数

- [x] **Step 1: 写正式 CLI 自检失败测试**

```python
def test_self_test_runs_full_pipeline_without_network(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used")))
    result = run_self_test()
    assert result["gate"] == "pass"
    assert result["idempotent"] is True
    assert result["duckdb"] == ":memory:"
    assert result["csv_rows"] == result["manifest_rows"]
```

- [x] **Step 2: 运行测试并确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_self_test.py -v`

Expected: FAIL，`run_self_test` 尚不存在。

- [x] **Step 3: 实现固定小数据量的生产路径自检**

在进程内生成最小策略、回测和模拟交易对象，并覆盖完成/失败/取消运行、畸形 JSON、999/1000/1001 条普通日志、完整/缺页/断序/缺终止/无写入器归因日志和不支持接口版本。使用 `io.BytesIO`、DuckDB `:memory:` 和 `TemporaryDirectory`，调用同一门禁、原子 manifest、查询和 CSV 函数；不得启动 Playwright、访问网络或读取 `joinquant/` 历史目录。用 `time.perf_counter` 和 `tracemalloc` 报告耗时及峰值，不设置硬件相关阈值。

- [x] **Step 4: 把 Skill 路径加入仓库验证触发范围**

修改 `.build-and-verify/config.json` 的 Python 测试 `paths` 和 `inputs`，加入 `.agents/skills/joinquant-archive-sync/**`、`.claude/skills/joinquant-archive-sync` 和 `.gitattributes`，保持现有 pytest 命令不变。

- [x] **Step 5: 运行内存 E2E 两次并确认稳定**

```powershell
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py self-test
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py self-test
```

Expected: 两次均返回 0，输出相同功能结论，临时目录结束后不存在，且没有网络访问。

- [x] **Step 6: 从 Claude 路径执行同一入口**

```powershell
$a = (Get-FileHash .agents\skills\joinquant-archive-sync\scripts\jq_sync.py -Algorithm SHA256).Hash
$b = (Get-FileHash .claude\skills\joinquant-archive-sync\scripts\jq_sync.py -Algorithm SHA256).Hash
if ($a -ne $b) { throw 'Codex and Claude Skill sources differ.' }
& .\.venv\Scripts\python.exe .claude\skills\joinquant-archive-sync\scripts\jq_sync.py self-test
```

Expected: SHA256 相同，`self-test` 返回 0。

- [x] **Step 7: 运行测试并确认通过**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_self_test.py tests/test_skill_layout.py -v`

Expected: PASS。

- [x] **Step 8: 获得用户 Git 授权后提交**

```powershell
git add .agents/skills/joinquant-archive-sync .build-and-verify/config.json tests/joinquant_sync/test_self_test.py
git commit -m "test: 增加聚宽同步内存端到端回归"
```

---

### Task 11: 验证计划任务发布入口和完整交付

**Files:**
- Modify: `tests/joinquant_sync/test_scheduler.py`
- Modify: `.agents/skills/joinquant-archive-sync/references/operations.md`
- Modify: `openspec/changes/add-joinquant-archive-sync/tasks.md`

**Interfaces:**
- Consumes: `scheduler_xml`、`install_scheduler`、`scheduler_status`、`uninstall_scheduler`、`self-test`

- [x] **Step 1: 写临时计划任务 E2E 测试**

```python
@pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler only")
def test_schtasks_runs_self_test(repo_root):
    task_name = "JoinQuantArchiveSync-SelfTest"
    try:
        install_scheduler(task_name, self_test_command(repo_root))
        subprocess.run(["schtasks", "/Run", "/TN", task_name], check=True)
        result = wait_for_task_result(task_name, timeout_seconds=60)
        assert result == 0
    finally:
        uninstall_scheduler(task_name)
```

- [x] **Step 2: 运行测试并确认失败或跳过原因正确**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_scheduler.py::test_schtasks_runs_self_test -v`

Expected on Windows: 首次 FAIL，直到状态轮询和临时任务清理完成；非 Windows 仅允许明确 SKIP。

- [x] **Step 3: 实现最小状态轮询和必达清理**

通过 `subprocess.run(["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"])` 读取 `Last Run Result`，最长等待 60 秒。`finally` 始终删除仅用于验收的临时任务；生产任务名不在测试中使用。

- [x] **Step 4: 运行发布入口 E2E**

Run: `.\.venv\Scripts\python.exe -m pytest tests/joinquant_sync/test_scheduler.py::test_schtasks_runs_self_test -v`

Expected: PASS，临时任务已删除，聚宽未被访问。

- [x] **Step 5: 运行全套仓库验证**

```powershell
& .\.venv\Scripts\python.exe -m pytest
openspec validate --all --strict --no-interactive
& .\.venv\Scripts\python.exe .build-and-verify\runtime\build_and_verify.py verify --full
```

Expected: pytest、OpenSpec strict（严格校验）和仓库 full（完整验证）全部通过；不能用单元测试组合替代发布入口 E2E。

- [x] **Step 6: 逐项勾选 OpenSpec tasks 并记录外部证据**

只有相应测试、PoC 文件或命令输出存在时才把 `openspec/changes/add-joinquant-archive-sync/tasks.md` 的对应项改为 `[x]`。报告分别列出：已验证、一次性真实 PoC、外部受限和未验证；不得把 `capped_free` 或 `missing_at_source` 表述为全部日志完整。

- [x] **Step 7: 获得用户 Git 授权后提交**

```powershell
git add .agents/skills/joinquant-archive-sync tests .build-and-verify/config.json openspec/changes/add-joinquant-archive-sync/tasks.md docs/research/joinquant-archive-sync-poc.md
git commit -m "test: 验证聚宽归档完整流程"
```

---

## 计划自检

- Spec coverage（规格覆盖）：11 个任务覆盖真实 PoC、页面身份、明确回测目标、策略与全部运行代码、数据/日志、归因门禁、普通日志 1000 条边界、积分确认、增量模拟交易、04:00 计划任务、紧凑查询、Git LFS、双代理 Skill 和内存 E2E。

### Task 12: 修复归因所有权误判

- [x] **Step 1: RED — 模拟交易不得归档后续代码历史中的回测日志**

新增回归：代码历史含多个归因 Token 时，只允许启动生命周期代码的路径进入 Research 读取参数和 manifest；其余路径不得传输或落盘。

- [x] **Step 2: RED — 回测归因必须关联最终资产**

新增回归：Token、日期和边界均匹配，但 `run_end.total_value` 或 `cash` 与 Research 最终资金记录不一致时必须失败；删除模拟交易 `history_versions` 整个字段时离线校验也必须失败。

- [x] **Step 3: GREEN — 最小修复同步和离线门禁**

复用现有单路径 Research 读取、`validate_attribution` 和清单校验，删除多源模拟交易归因分支；不新增扫描器、缓存层或依赖。

- [x] **Step 4: 清理真实错误归档并回归**

严格重同步两个活动模拟交易和回测 115，确认 `etf_factor_rotation` 模拟交易仅引用 30 条所属日志、回测 115 仍引用 1631 条日志；随后运行定向测试、内存 E2E 和 full（完整）验证。
- Placeholder scan（占位扫描）：没有占位语句或未定义的“以后实现”；真实目标通过必填环境变量输入，避免猜测或隐式默认。
- Type consistency（类型一致性）：Browser/Research 只产证据，Archive 统一生成 manifest 和门禁，Query 只消费 manifest；`self-test` 与计划任务均调用相同生产接口。
- Scope check（范围检查）：没有 Plugin、外部市场、服务进程、持久 DuckDB、全历史默认同步或第二套测试实现。
