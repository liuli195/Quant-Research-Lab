## ADDED Requirements

### Requirement: 活动模拟交易必须由显式手动命令同步
系统 MUST 只在调用者显式运行 `sync-active-simulations --repository <仓库路径>` 时扫描和增量同步全部活动模拟交易。该入口 MUST 复用既有采集、归档清单和完整性门禁；它 MUST NOT 创建或调用 Windows 任务计划程序、专用 worktree（工作树）、自动分支、Git（版本管理）提交或 PR Flow（拉取请求流程）。

#### Scenario: 用户手动同步活动模拟交易
- **WHEN** 调用者在目标仓库显式运行 `sync-active-simulations --repository .`
- **THEN** 系统同步活动对象并返回每个对象的归档结果，且不暂存、提交、推送、创建 PR 或修改任务计划

#### Scenario: 手动同步遇到对象失败
- **WHEN** 任一活动模拟交易在手动同步中未通过采集或完整性门禁
- **THEN** 系统返回失败对象和原因，不创建 Git 提交或 PR，并保留既有已验证归档

## MODIFIED Requirements

### Requirement: 仓库 Skill 必须提供统一同步入口
仓库 MUST 只在 `.agents/skills/joinquant-archive-sync/` 保存一份真实 Skill（技能）目录，并在其中自包含 `SKILL.md`、Python CLI（命令行接口）、运行依赖和参考资料。`.claude/skills/joinquant-archive-sync` MUST 使用同仓库相对 SymbolicLink（符号链接）指向该目录；不得创建 Plugin（插件）、marketplace（市场）、第二份同步脚本或缓存更新层。认证、抓取、解析、落盘、完整性校验和查询逻辑 MUST 只实现一次，Codex、Claude 和人工命令 MUST 调用该同一入口；系统 MUST NOT 提供 Windows 任务计划程序调用入口。

#### Scenario: Agent 调用 Skill 同步回测
- **WHEN** Agent（代理）通过 Skill 传入策略和一个回测详情链接
- **THEN** Skill 调用同一 CLI 完成同步并返回对象目录、各数据集状态和例外，不包含第二套抓取逻辑

#### Scenario: Codex 和 Claude 发现同一 Skill
- **WHEN** 分别从 Codex 和 Claude 项目入口加载 `joinquant-archive-sync`
- **THEN** 两端解析到同一仓库目录，且 `SKILL.md` 与执行脚本的 SHA256（完整性摘要）完全一致

### Requirement: 端到端回归必须覆盖手动同步入口
实现完成前 MUST 从 Codex、Claude 的仓库 Skill 或手动同步入口执行 `self-test`（自检）端到端回归。`self-test` MUST 在进程内生成小型证据并复用生产同步核心，覆盖明确目标选择、完整性门禁、临时归档、重复同步、查询和 CSV（逗号分隔值）输出；DuckDB（嵌入式分析数据库）使用内存数据库，归档只写系统临时目录，不得访问网络或加载历史归档。单元测试组合不得替代该主流程回归。真实聚宽能力只由首次实施 PoC（概念验证）验证，不进入常规端到端回归。

#### Scenario: 仓库 Skill 内存端到端回归
- **WHEN** Codex 和 Claude 分别通过仓库 Skill 调用同一 `self-test`
- **THEN** 两端使用进程内小型证据完成临时页面目录和清单、第二次幂等同步、DuckDB 查询及指定 CSV 导出，且不访问聚宽或历史数据

#### Scenario: 关键边界端到端回归
- **WHEN** 内存证据包含失败或取消运行、不合法日志响应、普通日志 1000 条边界、归因日志完整或缺页或断序或无终止事件或无写入器，以及不支持接口版本
- **THEN** 每种情况分别产生正确状态且没有任何对象被误报为全量完整

#### Scenario: 手动模拟交易入口回归
- **WHEN** 验收从 `sync-active-simulations` 入口使用受控外部边界执行活动模拟交易同步
- **THEN** 入口验证同一 `.venv`、CLI 路径和完整性门禁，且不访问真实聚宽、创建 Git 提交、PR、专用工作树或 Windows 任务

## REMOVED Requirements

### Requirement: 活动模拟交易必须按北京时间增量同步
**Reason**: 活动模拟交易改由调用者按需手动同步，不再运行每日 04:00 的后台任务。

**Migration**: 卸载 `JoinQuantArchiveSync` 后，调用者使用 `sync-active-simulations --repository .` 执行同步。

### Requirement: 自动归档只能发布通过严格门禁的归档变化
**Reason**: 手动同步不再承担 Git（版本管理）提交和发布职责；归档完整性门禁仍由同步核心保留。

**Migration**: 手动同步后由调用者决定是否按普通 Git 和 PR Flow（拉取请求流程）处理工作区修改。

### Requirement: 有效归档变化必须通过唯一 PR Flow 闭环进入主干
**Reason**: 日常同步不再自动创建分支、PR 或合并。

**Migration**: 调用者在审阅手动同步结果后，按仓库既有 PR Flow 手动发布需要保留的修改。

### Requirement: 自动归档失败状态必须可恢复且不得包含凭证
**Reason**: 移除后台发布编排后，不再维护自动化运行状态和恢复命令。

**Migration**: 手动命令直接返回无凭证的结果；调用者按输出重新认证、复核或重试。

### Requirement: 自动归档闭环必须从计划任务发布命令端到端回归
**Reason**: 计划任务发布入口已移除。

**Migration**: 使用手动 `sync-active-simulations` 入口和 `self-test`（自检）完成回归。
