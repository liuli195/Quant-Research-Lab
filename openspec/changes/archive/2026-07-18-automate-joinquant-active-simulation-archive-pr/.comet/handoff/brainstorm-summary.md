# Brainstorm Summary

- Change: automate-joinquant-active-simulation-archive-pr
- Date: 2026-07-19
- Status: 已确认

## 确认的技术方案

- 保持一个端到端 change（变更），不拆分同步隔离与 PR（拉取请求）发布。
- 复用现有 `sync-active-simulations`、`verify` 和 PR Flow `complete`（完成流程），不重写同步、摘要或 GitHub（代码托管）状态机。
- 自动化只在仓库外专用 worktree（工作树）运行，不切换、暂存、提交或清理用户当前工作区。
- 用户已确认：当自动化 worktree 在运行前干净、但本次批量同步部分写入后失败时，只回滚本轮产生且位于已识别归档范围内的变化；不清理运行前脏状态、范围外路径或用户工作区。
- 不直推 `main`、不强制推送、不自动解决冲突；生产计划任务迁移需要实现合并后的单独授权。
- 已有唯一自动化 PR 未完成时，下一次计划任务只恢复该 PR 的检查、审查、合并或清理，不启动新同步，也不向该 PR 追加新的归档提交。
- 用户已确认 PR Flow runtime（运行时）每次运行动态发现：优先读取 Codex 官方插件清单，Codex 不可用时回退 Claude 官方插件清单；两端均不可用或目标脚本不存在时，在同步前失败。
- 用户已确认使用仓库外固定持久 worktree（工作树）；不采用每次临时 worktree 或固定独立 clone（克隆）。
- 用户已澄清计划任务架构和 Python（运行环境）逻辑不属于本变更目标：沿用现有 `schedule-install`、项目 `.venv` 解析、CLI（命令行）脚本路径、工作目录、重试和所有权识别；只把核心同步与 Git（版本管理）操作编排到专用 worktree，不把 `jq_sync.py` 执行路径迁入 worktree，也不创建第二套虚拟环境。
- 已由现有计划任务、脚本、仓库规则或 OpenSpec（开放规格）明确的行为不再作为选择题重复询问；只有真实缺失或冲突才请求用户决定。
- 实现只改问题 #11 核心需求直接需要的部分；不借机调整周边架构、增加镀金需求、通用化或冗余抽象。

## 候选技术方案

- 候选 A（已选）：固定持久 worktree、固定自动化分支、已有 PR 优先恢复、薄编排器调用现有发布入口。
- 候选 B：每次运行创建临时 worktree，结束后删除；隔离更强但清理、失败现场和 Git worktree 元数据更复杂。
- 候选 C：固定独立 clone（检出副本）而非 linked worktree；共享状态更少，但远端同步、凭证和磁盘维护重复。
- PR Flow runtime 候选已收敛为复用官方 CLI（命令行）发现：`codex plugin list --json` 返回 `source.path`，`claude plugin list --json` 返回 `installPath`；当前两端均解析到版本 `0.1.41` 且目标脚本存在，无需扫描缓存目录。

## 待确认设计问题

无。

## 关键取舍与风险

- 持久 worktree 最小化目录生命周期，但必须严格识别干净 detached HEAD（分离头）与唯一自动化分支状态。
- 当前批次受限回滚保证计划任务重试可从已验证基线重新采集；路径归因不确定时必须停止而不是扩大清理。
- 固定唯一 PR 可避免重复发布，但未完成 PR 的恢复优先级会影响数据新鲜度与检查稳定性。
- 已选择检查稳定性优先：未完成 PR 会阻塞新归档发布，直到其成功收尾或人工恢复。
- 官方插件发现可随插件升级解析当前路径，但增加对本机 `codex` 或 `claude` CLI 可执行文件的运行前依赖；发现命令只读取本地插件清单，不启动 Agent 会话。

## 测试策略

- 从 `scheduled-sync-pr` 发布入口覆盖无变化、成功合并、同步失败和检查失败。
- 使用临时 Git 远端与可控 JoinQuant/PR Flow 外部边界；常规回归不访问真实 JoinQuant 或创建真实 GitHub PR。
- 使用临时 Windows 计划任务验证发布形态、参数、工作目录和返回状态。

## Spec Patch

当前无。若 brainstorming 发现缺失验收边界，只补充对应场景或澄清歧义，不扩大 change 范围。
