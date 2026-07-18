---
change: automate-joinquant-active-simulation-archive-pr
design-doc: docs/superpowers/specs/2026-07-19-automate-joinquant-active-simulation-archive-pr-design.md
base-ref: b015eaa757a8a14c30cc8cb33b9471844322ea7a
---

# 活动模拟交易归档自动 PR 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让现有 04:00 活动模拟交易同步在仓库外固定 worktree（工作树）中完成严格门禁，并通过现有 PR Flow（拉取请求流程）进入主干。

**Architecture:** 保留现有计划任务、项目 `.venv`、同步、验证和 PR Flow 实现，只新增一个 `scheduled-sync-pr` 薄编排入口。编排器复用现有文件锁和公开 CLI（命令行），把运行状态保存在仓库外，并只对本轮可归属归档路径执行精确提交或受限回滚。

**Tech Stack:** Python 3.12 标准库、现有 Playwright（浏览器自动化）、Git、GitHub CLI（命令行）、Codex/Claude 官方插件清单、现有 PR Flow、pytest（测试框架）、Windows Task Scheduler（Windows 任务计划程序）。

## Global Constraints

- 所有 Python 命令使用 `.venv\Scripts\python.exe`；不使用系统 Python，不新增或升级依赖。
- 不修改现有任务 XML 架构、时区、04:00、30 分钟三次重试、单实例、`.venv`、CLI 路径或工作目录逻辑。
- 不操作用户当前工作区的状态、分支、暂存区或文件；所有同步、验证和 Git 操作只针对仓库外固定 worktree。
- 不直推 `main`、不强推、不自动 rebase（变基）、不解决冲突、不实现第二套 PR 生命周期。
- 不使用 `git add -A`、`git reset --hard`、无路径 `git clean` 或全仓库 restore（恢复）。
- 不安装、替换、启停或删除生产计划任务；生产迁移在实现 PR 合并后另行授权。
- 每个任务完成其目标测试后再提交；提交说明使用简体中文。

---

### Task 1: 为同步结果补齐路径归属身份

**Files:**
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/sync_pipeline.py:2724-2741`
- Test: `tests/joinquant_sync/test_sync_pipeline.py:2046-2055`

**Interfaces:**
- Consumes: `sync_all_active_simulations(page, repository) -> list[dict[str, object]]`
- Produces: `committed` 和 `unchanged` 结果都包含 `strategy_id: str`、`simulation_id: str`；`failed` 结果包含异常发生前已经确定的身份字段；其他字段和状态语义不变。

- [x] **Step 1: 扩展现有增量测试，先证明缺失身份**

在 `test_active_simulation_sync_is_incremental` 的第三次 `unchanged` 断言后加入：

```python
assert third[0]["strategy_id"] == "strategy-001"
assert third[0]["simulation_id"] == "simulation-001"
```

新增 `test_failed_result_keeps_identified_ids`：让 `_update_strategy_latest` 在身份已确定后抛出测试异常，再调用一次同步并断言 `failed` 结果仍包含两个 ID；这条测试证明失败对象自身的部分写入可被 Task 3 归属和回滚。

- [x] **Step 2: 运行目标测试并确认失败**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\joinquant_sync\test_sync_pipeline.py -k "active_simulation_sync_is_incremental or failed_result_keeps_identified_ids" -q
```

Expected: FAIL，`unchanged` 和身份已确定的 `failed` 结果缺少对应 ID。

- [x] **Step 3: 最小扩展 `unchanged` 和 `failed` 结构化结果**

```python
if synced["status"] == "unchanged":
    results.append(
        {
            "name": candidate["name"],
            "status": "unchanged",
            "strategy_id": strategy_id,
            "simulation_id": simulation_id,
        }
    )
    continue
```

每个 candidate（候选对象）进入 `try` 前把 `strategy_id`、`simulation_id` 初始化为 `None`，异常处理只把已经赋值的字段复制到现有 `failure` 字典。不得移动写入顺序、改变 `commit_simulation_evidence` 或新增映射层。

- [x] **Step 4: 运行目标回归**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\joinquant_sync\test_sync_pipeline.py -k "active_simulation" -q
```

Expected: PASS。

- [x] **Step 5: 提交**

```powershell
git add .agents/skills/joinquant-archive-sync/scripts/joinquant_sync/sync_pipeline.py tests/joinquant_sync/test_sync_pipeline.py
git commit -m "完善活动模拟交易同步身份"
```

---

### Task 2: 建立最小运行锁、状态和 worktree 前置检查

**Files:**
- Create: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/scheduled_sync.py`
- Create: `tests/joinquant_sync/test_scheduled_sync.py`

**Interfaces:**
- Consumes: `archive.object_lock`、`subprocess.run`、Git、`gh`、`codex plugin list --json`、`claude plugin list --json`
- Produces: `run_scheduled_sync(repository: Path, *, python_exe: Path, cli: Path) -> tuple[int, dict[str, object]]`
- Internal: `_runtime_root() -> Path`、`_write_state(root, payload) -> None`、`_discover_pr_flow() -> Path`、`_prepare_worktree(repository, root) -> Path`

- [x] **Step 1: 写运行锁、状态和插件发现失败测试**

创建 `tests/joinquant_sync/test_scheduled_sync.py`，至少包含：

```python
def test_locked_run_skips_without_external_work(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    calls = []
    monkeypatch.setattr(scheduled_sync, "_discover_pr_flow", lambda: calls.append("plugin"))
    with object_lock(tmp_path / "QuantResearchLab" / "joinquant-archive-sync"):
        code, state = scheduled_sync.run_scheduled_sync(
            tmp_path / "repo", python_exe=Path(sys.executable), cli=Path("jq_sync.py")
        )
    assert code == 0
    assert state["status"] == "skipped"
    assert state["reason"] == "run_locked"
    assert calls == []
```

另覆盖：Codex 成功时不调用 Claude；Codex 失败后 Claude 成功；两端失败为 `pr_flow_unavailable`；原子状态可被 `json.loads` 读取且不含原始子进程输出。

- [x] **Step 2: 写 worktree 身份测试并确认失败**

用临时 Git 仓库覆盖首次创建、干净 detached HEAD（分离头）、固定自动化分支、未知分支和预先脏状态。关键断言：

```python
assert prepared == runtime_root / "worktree"
assert git(prepared, "branch", "--show-current").stdout.strip() == ""
assert git(prepared, "rev-parse", "HEAD").stdout.strip() == origin_main
```

未知分支或脏状态必须抛出 `ScheduledSyncError`，原文件仍存在。

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\joinquant_sync\test_scheduled_sync.py -k "locked or discovery or worktree" -q
```

Expected: FAIL，模块尚不存在。

- [x] **Step 3: 实现无抽象层的最小运行基础**

```python
AUTOMATION_BRANCH = "codex/joinquant-archive-auto"
RUNTIME_PARTS = ("QuantResearchLab", "joinquant-archive-sync")


class ScheduledSyncError(RuntimeError):
    pass


def _runtime_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())).joinpath(*RUNTIME_PARTS).resolve()


def _write_state(root: Path, payload: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".last-run.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, root / "last-run.json")
    finally:
        temporary.unlink(missing_ok=True)
```

`_discover_pr_flow` 严格匹配官方清单的插件 ID、安装/启用状态和脚本相对路径，不扫描目录。`_prepare_worktree` 只用显式 `git -C <repo> worktree add --detach`、`fetch origin main`、`status --porcelain` 和 `rev-parse`，不读取或修改用户工作区状态。

- [x] **Step 4: 运行目标测试**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\joinquant_sync\test_scheduled_sync.py -k "locked or discovery or worktree" -q
```

Expected: PASS。

- [x] **Step 5: 提交**

```powershell
git add .agents/skills/joinquant-archive-sync/scripts/joinquant_sync/scheduled_sync.py tests/joinquant_sync/test_scheduled_sync.py
git commit -m "建立归档自动化隔离运行基础"
```

---

### Task 3: 实现同步门禁、精确提交和受限回滚

**Files:**
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/scheduled_sync.py`
- Modify: `tests/joinquant_sync/test_scheduled_sync.py`

**Interfaces:**
- Consumes: Task 1 的同步结果身份、现有 `sync-active-simulations`、现有 `verify`
- Produces: `_allowed_prefixes(results) -> tuple[set[str], set[str]]`、`_rollback(worktree, baseline, tracked, untracked, allowed) -> str`；变化路径直接由 Git 原生命令产生，不新增状态解析器。
- Extends: `run_scheduled_sync(repository: Path, *, python_exe: Path, cli: Path) -> tuple[int, dict[str, object]]` 完成 `noop`、同步失败、验证失败、范围失败和有效提交。
- Test helper: `run_scenario(name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]` 只在本测试文件内建立临时 Git 远端并替换外部进程边界。

- [x] **Step 1: 写批次、门禁和回滚失败测试**

在真实临时 Git worktree 上准备已跟踪/未跟踪归档文件及范围外文件，覆盖：

```python
def test_partial_sync_rolls_back_only_identified_archive_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = run_scenario("sync_failed_with_partial_archive", tmp_path, monkeypatch)
    assert result["code"] != 0
    assert result["state"]["reason"] == "sync_failed"
    assert result["failed_result"]["strategy_id"] == "strategy-001"
    assert result["failed_result"]["simulation_id"] == "simulation-001"
    assert result["tracked_archive"].read_text(encoding="utf-8") == "baseline\n"
    assert not result["untracked_archive"].exists()
    assert result["out_of_scope"].read_text(encoding="utf-8") == "preserve\n"


def test_out_of_scope_change_blocks_commit_and_is_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = run_scenario("out_of_scope", tmp_path, monkeypatch)
    assert result["code"] != 0
    assert result["state"]["reason"] == "path_out_of_scope"
    assert result["out_of_scope"].exists()
    assert result["head"] == result["baseline"]
```

另覆盖：全部 `unchanged` 且 Git 空为 `noop`；manifest gate 非 pass；`verify` 非零；有效变化只暂存允许路径；失败时没有提交或 PR 调用。

- [x] **Step 2: 运行测试并确认失败**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\joinquant_sync\test_scheduled_sync.py -k "sync or verify or scope or rollback or noop" -q
```

Expected: FAIL，编排路径尚未实现。

- [x] **Step 3: 实现精确允许清单和回滚**

```python
def _allowed_prefixes(results: list[dict[str, object]]) -> tuple[set[str], set[str]]:
    files = {"joinquant/strategies/strategy_index.csv"}
    directories: set[str] = set()
    for result in results:
        strategy_id = str(result.get("strategy_id") or "")
        if not strategy_id:
            continue
        strategy = f"joinquant/strategies/{strategy_id}"
        files.update(
            {
                f"{strategy}/manifest.json",
                f"{strategy}/default_code.py",
                f"{strategy}/simulations/index.json",
            }
        )
        simulation_id = str(result.get("simulation_id") or "")
        if simulation_id:
            directories.add(f"{strategy}/simulations/{simulation_id}/")
    return files, directories
```

已跟踪变化直接读取 `git diff --name-only -z HEAD --`，未跟踪文件直接读取 `git ls-files --others --exclude-standard -z`；只按 NUL（空字符）分隔路径，不解析 porcelain（机器状态文本）或 rename（重命名）状态。已跟踪路径用精确 `git restore --source <baseline> --worktree -- <paths>`；未跟踪路径逐文件 `unlink`，只清理允许范围内空目录。

- [x] **Step 4: 实现固定编排顺序**

新批次顺序为：认证检查 → 基线/干净检查 → 调用现有同步 CLI → 全部结果成功 → 逐变化对象调用现有 verify → 路径门禁 → noop 或固定分支 → `git add -- <逐文件>` → 中文提交。提交前失败先写状态，再受限回滚并更新 `rollback_status`。

- [x] **Step 5: 运行目标回归并提交**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\joinquant_sync\test_scheduled_sync.py -k "sync or verify or scope or rollback or noop" -q
git add .agents/skills/joinquant-archive-sync/scripts/joinquant_sync/scheduled_sync.py tests/joinquant_sync/test_scheduled_sync.py
git commit -m "实现归档同步提交门禁"
```

Expected: PASS。

---

### Task 4: 接入唯一 PR Flow 与现有计划任务入口

**Files:**
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/scheduled_sync.py`
- Modify: `.agents/skills/joinquant-archive-sync/scripts/jq_sync.py:72-138,155-178,461-476`
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/scheduler.py:145-176`
- Modify: `tests/joinquant_sync/test_scheduled_sync.py`
- Modify: `tests/joinquant_sync/test_scheduler.py:135-193,221-289`

**Interfaces:**
- Consumes: 发现到的 `pr_flow.py`、固定分支 `codex/joinquant-archive-auto`
- Produces: CLI `scheduled-sync-pr --repository <repo>`；旧/新任务动作都可严格识别；PR Flow stop state 提升到 `last-run.json`。
- Test helper: 继续使用 Task 3 定义的 `run_scenario(name, tmp_path, monkeypatch) -> dict[str, object]`。

- [x] **Step 1: 写固定分支优先恢复和 PR Flow 失败测试**

```python
def test_fixed_branch_without_pr_status_resumes_complete_without_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = run_scenario("pushed_without_pr", tmp_path, monkeypatch)
    assert result["code"] == 0
    assert result["state"]["status"] == "complete"
    assert result["calls"] == ["pr-flow-complete"]


def test_merged_cleanup_state_resumes_cleanup_without_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = run_scenario("merged_cleanup_pending", tmp_path, monkeypatch)
    assert result["code"] == 0
    assert result["calls"] == ["pr-flow-cleanup:123"]


def test_pr_flow_stop_is_recoverable_and_never_uses_alternate_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = run_scenario("checks_failed", tmp_path, monkeypatch)
    assert result["code"] != 0
    assert result["state"]["reason"] == "pr_flow_stopped"
    assert result["state"]["pr"] == 123
    assert all("gh pr merge" not in " ".join(call) for call in result["calls"])
```

另覆盖：只有最新 `origin/main` 的干净 detached HEAD 才同步；固定分支不查询 PR 数量；运行期 PR 参数不含 `--fixes` 或 `#11`；成功后 worktree 为 detached HEAD。

- [x] **Step 2: 写 CLI 与任务兼容测试并确认失败**

```python
assert installed[0][1][2:] == [
    "scheduled-sync-pr",
    "--repository",
    str(tmp_path.resolve()),
]
args = build_parser().parse_args(["scheduled-sync-pr", "--repository", "D:/repo"])
assert args.command == "scheduled-sync-pr"
```

分别生成旧/新动作 XML，断言两者 `owned is True`；现有伪造字段测试继续证明其他改动会失去所有权。

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\joinquant_sync\test_scheduled_sync.py tests\joinquant_sync\test_scheduler.py -k "pr_flow or fixed_branch or cleanup_state or scheduled_sync or production_contract" -q
```

Expected: FAIL。

- [x] **Step 3: 只把固定分支交回现有 `complete` 或 `cleanup`**

默认只调用：

```text
<项目 .venv python> <发现到的 pr_flow.py> complete
  --project <专用 worktree>
  --summary 活动模拟交易归档
  --scope joinquant/strategies
```

固定分支的 `.pr-flow/last-status.json` 只有在 `command == "cleanup"`、`details.sourceBranch == "codex/joinquant-archive-auto"` 且 `details.pr` 存在时，才改为调用：

```text
<项目 .venv python> <发现到的 pr_flow.py> cleanup
  --project <专用 worktree>
  --pr <编号>
```

其他固定分支状态（包括没有状态文件、提交后尚未创建 PR、push/检查失败）都重跑 `complete`。不得执行状态文件中的命令文本，不得直接查询 PR 数量，不得传 `--fixes`，不得调用 `gh pr merge`。非零退出只读取 `<worktree>/.pr-flow/last-status.json` 的必要字段。

- [x] **Step 4: 接入 CLI，保持调度架构不变**

`jq_sync.py` 只新增 parser/调用/JSON 输出并替换 `schedule-install` 动作名。`scheduler._owned_task` 的生产动作集合只允许：

```python
{
    ("sync-active-simulations", "--repository"),
    ("scheduled-sync-pr", "--repository"),
}
```

其他 XML、Python 路径、CLI 路径、工作目录、时区和重试判断逐字保留。

- [x] **Step 5: 运行目标回归并提交**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\joinquant_sync\test_scheduled_sync.py tests\joinquant_sync\test_scheduler.py -q
git add .agents/skills/joinquant-archive-sync/scripts/joinquant_sync/scheduled_sync.py .agents/skills/joinquant-archive-sync/scripts/joinquant_sync/scheduler.py .agents/skills/joinquant-archive-sync/scripts/jq_sync.py tests/joinquant_sync/test_scheduled_sync.py tests/joinquant_sync/test_scheduler.py
git commit -m "接入活动归档自动 PR 流程"
```

Expected: PASS。

---

### Task 5: 完成发布入口 E2E、验证配置与操作文档

**Files:**
- Modify: `tests/joinquant_sync/test_scheduled_sync.py`
- Modify: `.build-and-verify/config.json:114-116`
- Modify: `.agents/skills/joinquant-archive-sync/SKILL.md`
- Modify: `.agents/skills/joinquant-archive-sync/references/operations.md`
- Modify: `openspec/changes/automate-joinquant-active-simulation-archive-pr/tasks.md`

**Interfaces:**
- Consumes: 完整 `scheduled-sync-pr` CLI、临时 Git 远端、可控 JoinQuant/GitHub/PR Flow 边界
- Produces: 四条完整发布路径证据、临时 Windows 计划任务发布形态证据、Full Verify 覆盖。

- [x] **Step 1: 从真实 CLI 分派补齐四条端到端场景**

每个场景调用：

```python
exit_code = jq_sync.main(
    ["scheduled-sync-pr", "--repository", str(source_repository)]
)
```

使用真实临时 Git 仓库和 worktree，仅替换 JoinQuant、GitHub 和 PR Flow 外部进程边界，分别证明：

```text
noop           → 无分支、无提交、无 PR
valid change   → 精确提交、PR Flow 合并、分支清理、detached main
sync failure   → 无提交、已识别归档回滚、范围外文件保留
checks failure → 不合并、固定分支保留、下次只恢复 PR Flow
```

- [x] **Step 2: 让现有 Full Verify 执行新测试**

只把现有命令改为：

```json
"command": "set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\\.venv\\Scripts\\python.exe -m pytest tests\\joinquant_sync\\test_scheduler.py tests\\joinquant_sync\\test_scheduled_sync.py -k \"not schtasks_runs_self_test\""
```

不得新增 check、依赖或超时配置。

- [x] **Step 3: 更新 Skill 与恢复说明**

只增加：`scheduled-sync-pr` 用途、`noop/run_locked/failed`、`last-run.json`、同一命令恢复、Codex 优先/Claude 回退、生产任务迁移需另行授权。保留 `sync-active-simulations` 手动入口，不改认证和 `.venv` 说明。

- [x] **Step 4: 运行发布入口与临时计划任务验证**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\joinquant_sync\test_scheduled_sync.py -q
& .\.venv\Scripts\python.exe -m pytest tests\joinquant_sync\test_scheduler.py -q -k "not schtasks_runs_self_test"
```

生产动作通过现有 `scheduler_xml` 和所有权测试精确断言 `.venv`、实际 `jq_sync.py scheduled-sync-pr --repository <repo>`、工作目录、04:00 和重试字段。真实 Task Scheduler 验收继续运行已有 `test_schtasks_runs_self_test`；它只验证同一 `.venv`、CLI 路径、工作目录和返回码，不占用生产运行锁，也不访问 JoinQuant、GitHub 或 Git 远端。验收后只删除该临时任务，不触碰生产任务。

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\joinquant_sync\test_scheduler.py::test_schtasks_runs_self_test -q
```

- [x] **Step 5: 运行组件回归和 Full Verify**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\joinquant_sync -q
& .\.venv\Scripts\python.exe .build-and-verify\runtime\build_and_verify.py build --project .
& .\.venv\Scripts\python.exe .build-and-verify\runtime\build_and_verify.py verify --project . --full
```

Expected: 全部通过；真实 JoinQuant、真实 GitHub PR 和生产任务迁移若未执行，必须明确记录为未在线验证。

- [x] **Step 6: 完成任务清单并提交**

逐项验收后勾选 `tasks.md`，不得提前勾选未运行的临时任务或 Full Verify。

```powershell
git add .build-and-verify/config.json .agents/skills/joinquant-archive-sync/SKILL.md .agents/skills/joinquant-archive-sync/references/operations.md tests/joinquant_sync/test_scheduled_sync.py openspec/changes/automate-joinquant-active-simulation-archive-pr/tasks.md
git commit -m "完成归档自动 PR 发布验证"
```

## 自审结果

- Spec coverage：运行锁、前置检查、专用 worktree、批次门禁、受限回滚、noop、唯一 PR、检查阻断、无敏感状态、四条 E2E 和 Windows 发布形态均有对应任务。
- Placeholder scan：无占位步骤或未定义的生产接口。
- Type consistency：唯一公开新增接口为 `run_scheduled_sync(repository: Path, *, python_exe: Path, cli: Path) -> tuple[int, dict[str, object]]`；Task 1 产生路径身份，Task 3 消费。
- Ponytail：没有新依赖、配置框架、第二 Python 环境、通用 Git/PR 抽象或生产计划任务操作。
