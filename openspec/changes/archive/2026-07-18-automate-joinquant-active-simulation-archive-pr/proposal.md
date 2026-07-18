## Why

当前每天 04:00 的 JoinQuant（聚宽）活动模拟交易同步直接写入用户正在使用的仓库；同步失败会留下已跟踪修改和孤儿文件，需要人工恢复。需要把任务收敛为隔离、可校验且受 PR Flow（拉取请求流程）门禁约束的自动归档闭环，使任何失败都不污染用户当前工作区，也绝不无人值守直推 `main`。

## What Changes

- 将计划任务发布入口改为在仓库外的专用持久 worktree（工作树）中运行，并使用跨进程运行锁阻止计划任务与手动调用重叠。
- 在同步写入前检查 JoinQuant 登录状态、GitHub 凭证和远端 `main` 基线；任一检查失败时安全停止。
- 复用现有 `sync-active-simulations` 和 `verify` 发布入口同步全部活动模拟交易，只允许通过 manifest（清单）、文件摘要和 Git 变更范围校验的归档变化进入提交。
- 没有归档变化且没有待恢复 PR 时，以成功无操作状态结束，不创建提交、远端分支或 PR。
- 有有效变化时使用固定 ASCII（英文字符）自动化分支和简体中文提交说明，调用现有 PR Flow `complete`（完成流程）创建或更新唯一活动 PR，等待必需检查和 review gate（审查门禁），通过后自动合并并清理分支。
- 任一阶段失败时禁止后续合并，原子记录不含凭证的结构化状态、失败阶段和恢复入口。
- 从计划任务发布命令增加端到端回归，覆盖无变化、成功合并、同步失败和检查失败路径。

## Non-Goals

- 不重写 JoinQuant 采集、增量游标、manifest 或文件摘要算法。
- 不另建 GitHub PR、检查、审查、合并或分支清理状态机，也不绕过现有 PR Flow。
- 不自动 rebase（变基）、强制推送、解决合并冲突或清理异常脏 worktree；这些情况必须安全停止并提供恢复入口。
- 不在本地执行正式回测或模拟交易，不保存账号、密码、token（访问令牌）、Cookie（浏览器凭证）或浏览器 profile（配置目录）。
- 本变更不直接替换当前生产计划任务；实现合并后仍需用户单独授权迁移。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `joinquant-archive-sync`: 将既有 04:00 活动模拟交易同步要求扩展为隔离工作区、运行前置检查、严格归档变更门禁、PR Flow 自动合并与结构化失败恢复的一体化发布闭环。

## Impact

- 修改仓库级 `joinquant-archive-sync` Skill（技能）的计划任务 CLI（命令行）入口、Windows Task Scheduler（Windows 任务计划程序）安装与所有权识别逻辑，并新增一个薄编排模块。
- 复用现有 JoinQuant 同步与校验实现、`.pr-flow/config.yaml`、PR Flow runtime（运行时）、Full Verify（完整验证）、CodeQL（代码扫描）和 GitHub review gate。
- 专用 worktree、运行锁和最后一次运行状态位于 `%LOCALAPPDATA%\QuantResearchLab\joinquant-archive-sync\`，不进入 Git。
- 不新增第三方依赖，不修改现有归档格式、同步数据口径或 GitHub workflow（工作流）。
