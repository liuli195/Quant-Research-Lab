---
comet_change: automate-joinquant-active-simulation-archive-pr
role: technical-design
canonical_spec: openspec
archived-with: 2026-07-18-automate-joinquant-active-simulation-archive-pr
status: final
---

# 活动模拟交易归档自动 PR 技术设计

## 1. 设计边界

需求和验收场景以 `openspec/changes/automate-joinquant-active-simulation-archive-pr/specs/joinquant-archive-sync/spec.md` 为唯一事实源。本文只说明问题 #11 的最小实现方式，不建立第二份需求规格。

本变更只解决一个根因：现有 04:00 计划任务把用户当前仓库直接作为 `sync-active-simulations --repository` 的写入目标，批次失败时可能在该工作区留下部分归档变化，并且没有把有效变化送入既有 PR Flow（拉取请求流程）。

以下现有行为保持不变：

- Windows Task Scheduler（Windows 任务计划程序）的 04:00 触发、北京时间检查、30 分钟间隔三次重试、单实例策略和任务所有权边界；
- 项目 `.venv` 的 Python（运行环境）解析、`jq_sync.py` 脚本路径和工作目录；
- JoinQuant（聚宽）认证、活动对象发现、增量游标、manifest（清单）、摘要、归因和关闭对象最终同步语义；
- `.pr-flow/config.yaml` 中的检查、审查、合并与清理规则。

计划任务最终只把现有动作参数从 `sync-active-simulations --repository <repo>` 改为 `scheduled-sync-pr --repository <repo>`；不重构任务 XML（配置文本）生成器，不创建第二套 Python 环境。

## 2. 最小改动面

| 文件 | 必要改动 |
|---|---|
| `.agents/skills/joinquant-archive-sync/scripts/jq_sync.py` | 增加 `scheduled-sync-pr` 命令并把 `schedule-install` 的动作参数指向该命令 |
| `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/scheduled_sync.py` | 新增一个薄编排模块，负责运行锁、专用 worktree（工作树）、前置检查、调用现有 CLI、Git 门禁、受限回滚和 PR Flow 调用 |
| `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/sync_pipeline.py` | 让 `unchanged` 结果补充已有的 `strategy_id` 和 `simulation_id`，并让 `failed` 结果携带失败前已经确定的身份字段，供路径门禁和受限回滚归属变化；不改变同步或门禁语义 |
| `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/scheduler.py` | 仅让任务所有权识别同时接受新动作和旧动作，保证迁移时仍能安全识别并卸载旧任务 |
| `tests/joinquant_sync/` | 增加编排端到端回归，并只更新与新任务动作直接相关的现有调度测试 |
| `.build-and-verify/config.json` | 只把新的编排测试追加到现有 `verify.scheduler-unit` 命令，不新增检查或验证框架 |
| Skill 操作文档 | 说明状态、恢复命令和生产任务迁移仍需单独授权 |

`scheduled_sync.py` 是唯一新生产模块。它不定义接口、Provider（提供方）、状态机框架或可插拔后端；把这段非平凡编排塞进 `jq_sync.py` 会混入现有命令分派，拆成一个模块是最小可测试边界。

## 3. 运行目录与现有能力复用

固定运行根目录为：

```text
%LOCALAPPDATA%\QuantResearchLab\joinquant-archive-sync\
├── worktree\
├── .sync.lock
└── last-run.json
```

- `worktree` 是同一 Git（版本管理）仓库的固定 linked worktree（关联工作树），不创建独立 clone（克隆）。
- 运行锁直接复用现有 `archive.object_lock`，不增加锁依赖或第二套锁实现。
- `last-run.json` 使用临时文件加 `os.replace` 原子更新；只保留最后一次状态，不建立历史数据库。
- 计划任务仍用主项目 `.venv\Scripts\python.exe` 执行主项目中的 `jq_sync.py`。只有同步、验证、Git 和 PR Flow 的目标目录是专用 worktree；用户当前工作区不作为归档输入，也不执行 `status`、切分支、暂存、提交或清理。

固定自动化分支为 `codex/joinquant-archive-auto`。worktree 只接受两种运行前身份：

1. 干净的 detached HEAD（分离头），指向最新 `origin/main`，可以开始新批次；
2. 干净的固定自动化分支，可以把既有发布交回 PR Flow 恢复。

其他分支、远端分叉或预先存在的脏状态全部停止并保留现场，不尝试自动修复。编排器不查询或判断 PR 数量；PR 发现和歧义处理继续由 PR Flow 负责。

## 4. 单次运行流程

```text
scheduled-sync-pr
  → 获取现有文件锁
  → 发现 PR Flow runtime（运行时）并检查 GitHub/远端
  → 创建或验证固定 worktree
  → 若处于固定自动化分支：按 PR Flow 状态只恢复 complete 或 cleanup
  → 否则检查 JoinQuant 登录并定位到最新 origin/main
  → 调用现有 sync-active-simulations
  → 调用现有 verify + 精确 Git 路径门禁
  → 无变化：noop
  → 有变化：固定分支 + 精确暂存 + 中文提交
  → 调用现有 PR Flow complete
  → 成功后由 PR Flow 合并、删除分支并把 worktree 留在最新 main 提交的 detached HEAD
```

锁冲突返回 `skipped/run_locked` 且退出码为 0，避免 Task Scheduler 把“已有同一运行”当作失败再次制造重试。其他未完成状态返回非零码，使现有 30 分钟重试继续调用同一个入口。

## 5. 前置检查与 PR Flow 发现

所有可能写归档的步骤之前依次完成：

1. 验证源仓库、`origin` 和 `main`；执行只读远端查询或 `fetch origin main`，记录基线提交；
2. 运行 `gh auth status` 验证当前用户 GitHub 凭证；
3. 通过官方插件清单发现 PR Flow：优先解析 `codex plugin list --json` 中精确的 `pr-flow@my-agent-skills-marketplace`，Codex 不可用时回退 `claude plugin list --json`；
4. 只从官方返回的插件根目录拼接固定相对路径 `skills/pr-flow/scripts/pr_flow.py`，并验证文件存在；
5. 没有待恢复发布时，复用现有 `auth --headless --timeout-seconds 0` 检查 JoinQuant 登录，再开始同步。

发现逻辑不扫描 Codex 或 Claude 缓存目录、不比较版本号、不选择“最新版”、不复制 PR Flow，也不启动 Agent（代理）会话。两端均不可用时在任何归档写入前失败。

恢复未完成发布不需要 JoinQuant 登录，因为该路径不会读取或写入新的归档；它只需要 Git、GitHub 和 PR Flow 可用。

## 6. 未完成发布优先恢复

每次运行先看专用 worktree 的本地身份，不再建立 GitHub PR 数量判断：

- 干净 detached HEAD 且等于刚获取的 `origin/main`：开始新同步批次。
- 干净固定自动化分支：不得同步或增加提交；若 `.pr-flow/last-status.json` 表明上次命令是该分支的 `cleanup` 且包含 PR 编号，则调用现有 `cleanup --pr <编号>`，否则重跑现有 `complete`。这同时覆盖提交后尚未创建 PR、推送或检查失败，以及 PR 已合并但清理未完成。
- 其他身份或脏状态：记录异常并停止。

编排器只白名单选择 `complete` 或 `cleanup`，不执行状态文件中的命令文本。`complete` 自己负责推送、查找或创建 PR，并在 PR 歧义、远端分叉等情况下返回停止状态。

PR Flow 是唯一 PR 生命周期实现。编排器不重复实现 push（推送）、PR 创建、检查等待、review gate（审查门禁）、合并或分支删除；现有配置继续要求 Full Verify（完整验证）、CodeQL（代码扫描）和 GitHub review gate 全部通过。PR Flow 已支持在源分支合并后把当前 worktree detach（分离）到远端 base（目标分支）提交，再安全删除源分支，因此不需要签出已被用户工作区占用的本地 `main`。

运行期归档 PR 不传 `Fixes #11`；只有本功能的实施 PR 关闭问题 #11。

## 7. 同步、验证与精确提交

新批次记录开始时的 `HEAD`，并用 `git status --porcelain` 只判断 worktree 是否为空。随后用当前 `sys.executable` 和当前 `jq_sync.py` 作为子进程调用：

```text
sync-active-simulations --repository <专用 worktree>
```

编排器只解析该命令的结构化 JSON（结构化数据），不导入或复制同步内部规则。当前 `unchanged` 结果在判定对象未变化前仍可能更新策略索引或 `default_code.py`，因此使其与 `committed` 一样返回已有的 `strategy_id` 和 `simulation_id`；`failed` 结果只补充异常发生前已经确定的 `strategy_id`、`simulation_id`。尚未取得身份且未写入归档的失败结果可以安全跳过路径归属。除此之外不改同步语义。只要任一对象为 `failed` 或顶层状态不是 `complete`，整个批次失败。

对每个发生变化的模拟交易目录调用现有 `verify --object <path>`。Git 变化必须同时满足：

- 位于 `joinquant/strategies/strategy_index.csv`；或
- 位于本轮结果对应策略的 `manifest.json`、`default_code.py`、`simulations/index.json`；或
- 位于本轮结果对应的 `simulations/<simulation_id>/` 目录。

门禁直接使用 `git diff --name-only -z HEAD --` 获取已跟踪变化，并使用 `git ls-files --others --exclude-standard -z` 获取未跟踪文件；不编写 `git status --porcelain` 解析器，不使用 `git add -A`，也不增加可配置 allowlist（允许清单）。任何无法归属到本轮同步结果的变化都视为范围外变化并停止。

若 Git 无变化，记录 `noop` 并结束，不创建分支、提交或 PR。若存在全部通过门禁的变化，才从 detached HEAD 创建固定分支，按逐文件清单暂存，并创建简体中文提交。随后调用发现到的 PR Flow `complete`；摘要和范围固定描述“活动模拟交易归档”，不传问题关闭参数。

## 8. 当前批次受限回滚

受限回滚只在以下条件全部成立时运行：

- worktree 在本次运行开始时干净；
- 尚未创建提交、推送或进入 PR 生命周期；
- 变化来自本轮同步之后的 Git 原生路径清单；
- 路径属于上述已识别归档范围。

回滚步骤：

1. 先原子记录失败阶段、原因和待回滚路径；
2. 对已跟踪路径执行以本轮基线 `HEAD` 为来源的精确 `git restore --worktree -- <paths>`；
3. 对本轮产生的未跟踪文件逐文件删除，并只向上清理已变空且仍位于允许归档范围内的目录；
4. 再次读取 Git 状态，把回滚结果写回 `last-run.json`。

不得执行 `git reset --hard`、无路径的 `git clean`、全仓库 restore（恢复）或递归删除。范围外路径即使在运行期间出现也原样保留并阻止继续；路径归因不确定时不扩大清理范围。

创建提交以后不再回滚文件。PR Flow 失败时保留干净固定分支；下一次计划任务把该分支重新交给 PR Flow，不自行判断 PR 是否已经创建。

## 9. 最后运行状态与恢复

`last-run.json` 只包含：

```text
run_id, started_at, finished_at, phase, status, reason,
worktree, branch, pr, recovery_command, rollback_status
```

状态只使用 `complete`、`noop`、`skipped` 和 `failed`。`reason` 使用稳定短码，例如 `run_locked`、`auth_required`、`worktree_dirty`、`sync_failed`、`verify_failed`、`path_out_of_scope` 和 `pr_flow_stopped`。

编排器只保存自己生成的短原因和 PR Flow 已有结构化状态中的必要字段，不保存环境变量、完整命令行、Cookie（浏览器凭证）、token（访问令牌）、浏览器 profile（配置目录）或未经筛选的 stdout/stderr。

恢复命令始终指向同一个 `scheduled-sync-pr --repository <repo>` 入口。需要人工处理的分叉、冲突、脏 worktree 或多 PR 情况只报告条件，不生成破坏性自动修复命令。

## 10. 计划任务兼容迁移

`scheduler_xml`、`install_scheduler`、时区检查、重试和 `.venv` 路径逻辑不改。必要改动只有：

- `schedule-install` 生成的新动作参数改为 `scheduled-sync-pr --repository <repo>`；
- `_owned_task` 同时严格识别旧的 `sync-active-simulations --repository <repo>` 和新的动作，确保旧任务仍可由 `schedule-uninstall` 安全删除；
- 新任务安装后只认新动作，不放宽任务命名、Python 路径、CLI 路径、工作目录或 XML 结构。

本功能通过正常 PR 合并后，仍须获得用户对生产计划任务变更的单独授权，才卸载旧任务并安装新任务。实现阶段不得自行替换、启停或删除生产任务。

## 11. 验证策略

### 11.1 最小目标测试

- 官方插件 JSON 解析：Codex 成功、Codex 失败后 Claude 成功、两端失败；
- worktree 身份：首次创建、干净 detached HEAD、固定自动化分支、脏状态和未知分支；
- PR Flow 恢复：提交后尚未创建 PR 时重跑 `complete`，已合并但清理未完成时按结构化状态调用 `cleanup`；
- 精确路径门禁与受限回滚：已跟踪、未跟踪、范围外路径和提交后不回滚；
- 调度兼容：现有 XML 结构不变，新旧动作均严格识别。

### 11.2 发布入口端到端回归

测试从 `jq_sync.main(["scheduled-sync-pr", ...])` 的真实命令解析和完整编排进入，使用临时 Git 远端并只替换 JoinQuant、GitHub 和 PR Flow 外部边界，不直接拼接内部函数测试。四条必测路径为：

1. 无变化：`noop`，没有分支、提交或 PR；
2. 有效变化：精确提交，PR Flow 成功合并并清理；
3. 同步失败：没有提交，已识别归档变化完成受限回滚；
4. 检查失败：保留固定分支，下一次运行只恢复 PR Flow。

Windows 验收分成两个现有边界：单元测试精确验证生产任务 XML 使用 `.venv`、`jq_sync.py scheduled-sync-pr --repository <repo>`、既有工作目录和重试设置；现有临时 `self-test` 任务继续实际经过 Task Scheduler 验证 `.venv`、CLI 路径、工作目录和返回码。两者合起来覆盖发布形态，但不占用生产运行锁、不访问真实 JoinQuant、不创建真实 GitHub PR、不读取历史归档。

最后运行 `tests/joinquant_sync`、Skill 发布入口回归和仓库 Full Verify（完整验证）。

## 12. Ponytail 精简审查

保留的新增内容都由问题 #11 的验收直接要求：一个编排命令、一个固定 worktree、一个复用现有锁的运行锁、一份最后状态、官方插件发现、严格路径门禁和受限回滚。

明确不做：

- 不修改计划任务架构或 Python 环境；
- 不新增依赖、服务、守护进程、队列、数据库或历史运行中心；
- 不新增通用 Git/PR 框架、插件发现框架、配置文件或多后端抽象；
- 不扫描插件缓存、不固定插件绝对路径、不内置 PR Flow；
- 不重写同步、verify、push、检查、审查、合并或清理逻辑；
- 不实现自动 rebase（变基）、冲突解决、强推、异常 worktree 清理或多个 PR 的猜测选择；
- 不创建临时 worktree 生命周期或独立 clone。

如果未来出现多个仓库、多个自动化分支或并发吞吐需求，再以真实问题单独设计；问题 #11 不为这些假设预留接口。
