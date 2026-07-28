## Context

现有 `JoinQuantArchiveSync` 计划任务直接使用活动开发仓库作为 `--repository` 和工作目录。`sync-active-simulations` 已经负责发现全部活动对象、按对象原子提交 manifest（清单）、校验文件摘要并返回 `committed`、`unchanged` 或 `failed`；PR Flow（拉取请求流程）的 `complete` 入口已经负责推送、创建或更新 PR、等待检查、执行 review gate（审查门禁）、合并和安全清理。缺口不是新的同步器或 GitHub（代码托管）状态机，而是一个把这两个现有发布入口放进隔离工作区并进行失败封锁的薄编排层。

本变更同时受以下约束：用户当前工作区不得被切换、暂存、提交或清理；正式回测和模拟交易仍只在 JoinQuant（聚宽）云端运行；自动化不得直推 `main`、强制推送或保存凭证；当前生产计划任务只有在实现合并后获得用户单独授权才能迁移。

## Goals / Non-Goals

**Goals:**

- 让每天 04:00 的活动模拟交易同步只在仓库外的自动化专用 worktree（工作树）中写文件。
- 在任何归档写入前完成运行锁、JoinQuant 认证、GitHub 凭证和远端 `main` 基线检查。
- 只提交通过现有 manifest、文件摘要和变更范围门禁的归档变化；无变化时不产生 Git 或 PR 副作用。
- 复用 PR Flow 完成唯一自动化 PR 的推送、检查、审查、合并和分支清理。
- 为每次停止保留不含敏感信息的结构化状态和可执行恢复入口，并从计划任务发布命令覆盖关键端到端场景。

**Non-Goals:**

- 不改变 JoinQuant 抓取、游标、归因、manifest、摘要或查询语义。
- 不实现第二套 PR、检查、审查、合并或清理逻辑。
- 不自动解决远端分叉、合并冲突、预先存在的脏 worktree 或受保护分支例外。
- 不引入新依赖、通用任务框架、可插拔后端、数据库运行历史或新配置文件。
- 不在本变更中替换、启停或删除当前生产计划任务。

## Decisions

### 1. 在现有 Skill 中增加一个薄发布入口

新增 `scheduled-sync-pr` CLI（命令行）命令和单一编排模块。计划任务继续调用仓库现有 `.venv` 与 `jq_sync.py`，编排模块只负责外部状态、Git/worktree 和两个既有发布入口的顺序，不导入或复制同步内部规则。

备选方案是新建独立服务或通用自动化框架；这会复制 CLI、认证和配置边界，且当前只有一个调用者，因此拒绝。

### 2. 使用仓库外的固定专用 worktree 和标准库运行锁

运行根目录固定为 `%LOCALAPPDATA%\QuantResearchLab\joinquant-archive-sync\`，其中只保存持久 worktree、文件锁和 `last-run.json`。编排器通过 Windows 标准库文件锁覆盖整个运行；Windows Task Scheduler（Windows 任务计划程序）的 `MultipleInstancesPolicy=IgnoreNew` 保留为第一层保护，文件锁覆盖手动调用及其他入口。

固定 worktree 避免每次运行创建和删除目录。每次开始时只允许两类已知状态：最新远端 `main` 上的干净 detached HEAD（分离头），或可交回 PR Flow 继续的干净固定自动化分支。任何其他预先存在的修改都原样保留并停止，不执行通用清理。

### 3. 先恢复未完成发布，再开始新的同步批次

固定自动化分支为 `codex/joinquant-archive-auto`。如果 worktree 已在该分支，编排器不得同步或增加提交；`.pr-flow/last-status.json` 若表明该分支的合并后清理尚未完成且包含 PR 编号，则调用现有 `cleanup`，其他情况重跑现有 `complete`。这覆盖提交后尚未创建 PR、推送或检查失败，以及 PR 已合并但清理未完成。只有 worktree 是最新 `origin/main` 的干净 detached HEAD 时才开始同步。

该顺序以 Git 和 `.pr-flow/last-status.json` 为事实源，不新增 GitHub PR 数量判断。编排器只选择现有 `complete` 或 `cleanup`，不执行状态文件中的命令文本；PR 查找、创建、歧义、远端分叉或基线变化都由 PR Flow 处理。编排器不 rebase（变基）或强制推送。

### 4. 同步批次在提交前执行三重门禁

编排器从发布入口调用 `sync-active-simulations` 并解析其 JSON（结构化数据）结果。只有所有对象均未返回 `failed`，才继续：

1. 对发生变化的模拟交易目录调用现有 `verify`；
2. 要求对象 `gate.status=pass` 且 manifest 引用文件摘要通过；
3. 要求 Git 变化只属于策略索引、对应策略 manifest/default code（默认代码）及对应 `simulations/` 归档目录。

任一门禁失败时不暂存、不提交、不推送。若 worktree 在本次运行开始前干净，编排器在写出失败状态后只回滚本次同步产生的已识别归档路径，使计划任务的下一次重试从同一已验证 Git 基线开始；它不得清理运行前已经存在的脏状态，也不得对范围外路径执行删除。

为完成归属，`unchanged` 结果补充已有的策略和模拟交易 ID；`failed` 结果只携带异常前已经确定的身份字段。尚未取得身份且未写入归档的失败结果不扩大回滚范围。

备选方案是重新实现 SHA256（摘要算法）或信任 `git add -A`；前者重复现有能力，后者可能提交同步器范围外内容，因此拒绝。

### 5. 无变化直接结束，有变化才创建分支和提交

新批次在 detached HEAD 上完成同步和校验。Git 无变化时写入 `noop` 并返回成功，不创建分支、提交或 PR。有变化时才创建固定分支，按已经校验的精确路径暂存，使用简体中文提交说明，然后调用 PR Flow `complete`。

PR Flow runtime（运行时）在 `schedule-install` 时解析并把绝对路径写入任务参数；运行时先验证该文件仍存在。这样既不复制约 2000 行 PR Flow 实现，也不依赖 Agent（代理）会话。插件升级使固定路径失效时，任务在同步前停止并要求重新安装计划任务。

### 6. PR Flow 是唯一 PR 生命周期实现

编排器传入固定摘要和范围，不给日常归档 PR 添加 `Fixes #11`。PR Flow 根据现有 `.pr-flow/config.yaml` 自动推送、创建或更新当前分支的唯一 PR、等待 Full Verify（完整验证）、CodeQL（代码扫描）和 GitHub review gate，全部通过后合并并清理分支；编排器不重复解析检查名称或调用 `gh pr merge`。

PR Flow 返回非零状态时，编排器读取其现有结构化状态并提升到本次 `last-run.json`，保留分支和干净 worktree 供原命令重试。

### 7. 最小结构化状态，不保存原始外部输出

`last-run.json` 只记录运行编号、开始/结束时间、阶段、状态、原因、worktree、分支、PR 编号和恢复命令，并通过临时文件加原子替换写入。状态值只需区分 `complete`、`noop`、`skipped` 和 `failed`；不建立历史数据库或独立 schema（结构定义）。

编排器不得把环境变量、Cookie、token、浏览器 profile、完整命令行或未经筛选的 stdout/stderr 写入状态。详细 PR 恢复信息继续由 `.pr-flow/last-status.json` 保存。

### 8. 端到端回归从发布命令进入

测试从 `scheduled-sync-pr` 入口运行完整本地编排，使用临时 Git 远端和可控的 JoinQuant/PR Flow 外部边界覆盖无变化、成功合并、同步失败和检查失败。Windows 测试用单元测试精确验证生产任务 XML，并继续用现有无外部访问的临时 `self-test` 任务验证 Task Scheduler 实际执行；不占用生产运行锁，不访问真实 JoinQuant，也不创建真实 GitHub PR。

## Risks / Trade-offs

- [PR Flow 固定路径在插件升级后失效] → 安装时固定、运行前检查，失败发生在任何同步写入前，并给出重新安装任务的恢复命令。
- [同步部分成功后另一个对象失败] → 不提交部分结果；记录失败后只回滚本次运行已识别的归档路径，下一次计划任务重试重新采集。
- [自动化 worktree 在运行前已经脏或身份异常] → 不自动清理，保留现场并返回精确路径和恢复入口。
- [远端 `main` 或自动化分支在运行期间变化] → 依赖 Git 基线检查和 PR Flow 的提交身份门禁停止；不 rebase、不强推。
- [检查时间超过 PR Flow 等待窗口] → 保留唯一 PR 和分支，下一次调用继续同一 PR，不创建重复 PR。
- [持久 worktree 占用本地磁盘] → 只保留一个固定目录；成功合并后 PR Flow 清理分支但不删除 worktree。

## Migration Plan

1. 在普通功能分支实现并通过目标测试、计划任务发布入口回归和 Full Verify。
2. 通过正常 PR 合并实现；实现 PR 使用 `Fixes #11`，运行期归档 PR 不使用。
3. 合并后先只读验证 GitHub 凭证、JoinQuant 登录、PR Flow runtime 路径和新任务 XML（配置文本）。
4. 获得用户对计划任务变更的单独授权后，使用兼容旧任务所有权识别的 `schedule-uninstall` 删除旧任务，再用新 `schedule-install` 安装新任务。
5. 使用临时自测任务验证发布形态；生产首次运行若失败则保留结构化状态并停止，不恢复会写入活动仓库的旧任务。

回滚只卸载新任务并保留自动化 worktree 供检查；不得自动恢复旧的直接写活动仓库任务。代码回滚仍通过正常 PR 完成。

## Open Questions

无待用户决定的需求问题。进入深度设计阶段后仍需用当前安装状态验证 PR Flow runtime 的定位方式、GitHub 实际必需检查和 Windows 文件锁行为；验证结果若与上述边界冲突，必须回到设计确认点而不是静默扩大实现。
