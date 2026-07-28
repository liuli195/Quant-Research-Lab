## Why

每日 04:00 的计划任务、仓库外专用工作树和自动 PR（拉取请求）发布引入了额外的 Git（版本管理）恢复状态；失败后会阻塞后续同步，且不符合当前由用户按需执行归档的使用方式。

## What Changes

- **BREAKING**：移除活动模拟交易的每日计划任务、重试、专用工作树、自动分支和自动 PR Flow（拉取请求流程）发布。
- 保留 `sync-active-simulations`（活动模拟交易同步）作为唯一手动入口：在调用者当前仓库工作区同步、校验并返回结果，但不提交、推送或合并。
- 移除计划任务相关 CLI（命令行接口）、运行时状态和测试；更新 Skill（技能）说明、操作文档和端到端回归入口。
- 已安装的 `JoinQuantArchiveSync` 任务由用户单独授权卸载；本变更不自行启停 Windows 任务计划程序。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `joinquant-archive-sync`：将活动模拟交易归档从每日自动发布改为显式手动同步，移除所有计划任务、专用工作树和 PR Flow（拉取请求流程）职责。

## Impact

- 修改 `.agents/skills/joinquant-archive-sync/` 内的 CLI（命令行接口）、调度编排和操作文档，以及对应的同步测试。
- 修改 `openspec/specs/joinquant-archive-sync/spec.md` 的自动归档验收要求。
- 不改变 JoinQuant（聚宽）采集、归档清单、完整性门禁、查询或导出语义；不新增依赖、服务或配置。
