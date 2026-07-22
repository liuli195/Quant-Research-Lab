# AGENTS.md

本仓库是 Vibe-Trading（AI 量化研究助理）+ vectorbt（本地回测框架） + JoinQuant（聚宽云端平台）+ 本地归档复盘的量化研究工作台。

## 通用规则

### 环境与工具

- **JoinQuant**：正式回测和模拟交易只在 JoinQuant（聚宽）云端运行；本地只做策略编写、本地回测、资料整理、结果归档和复盘。
- **Vibe-Trading**：只作为研究、策略草稿、归因和报告工具；不作为正式回测或模拟交易裁判。
- **vectorbt**：本地唯一回测框架，用于模拟交易和策略验证；禁止自研本地回测框架，必须使用本仓库提供的框架。
- **Python**：默认必须提权使用项目 `.venv`，不使用系统 Python。
- **构建与验证**：使用 `build-and-verify` Skill（技能）执行构建检查和验证；禁止自行新增构建和验证入口。

### 工作边界

- **先行调查**：进行任务前，先行调研，不推测未知内容；不确定时先验证。
- **执行边界**：只执行用户明确授权的操作；未授权时先给方案或草案等待确认，禁止自行安装、改配置、重构、删除、提交、切换分支或启停服务。
- **文件规范**：优先编辑现有文件；非必要不新建；任务后清理临时产物。

### Git 与 PR

- **Git（版本管理）**：分支名使用 ASCII（英文字符）模板，提交说明用简体中文。
- **PR 纪律**：进入主干须通过 PR；用户显式授权才可直写主干；禁止把功能分支本地合入主干。
- **安全性**：破坏性操作前必须确认，包括删除、覆盖、强制推送、硬重置、`--no-verify`。
- **密钥安全**：禁止提交账号、密码、token（访问令牌）、cookie（浏览器凭证）和任何私密配置。

### Review 与验证

- **完成验证**：逐项复核要求，说明已验证与无法验证的部分。
- **完整集成测试**：必须运行覆盖对应主流程的端到端回归；端到端回归必须从用户入口或发布形态跑完整业务流程，不能用几个单元测试拼接替代。

### 输出与引用

- **输出**：简体中文，简洁直白；英文技术名词后跟中文释义。
- **引用**：引用本地文件时使用可点击路径。

<comet-ambient-resume>
<!-- Managed by Comet. Edits inside this block may be replaced by comet init/update. -->
<!-- Contract: comet.resume_probe.v2 -->

## Comet Ambient Resume

在这个仓库中，开始处理需要改动或调查的任务前，如果可能存在活跃 Comet workflow，把当前用户请求传入只读探针：`comet resume-probe . --stdin --json`。

- 只信任返回的 `workflow`、`skill` 和 `entrySource`；它们只由项目配置或无配置兼容回退决定。不得扫描或切换另一套 workflow。
- 如果 probe 返回 `auto_resume`，简短说明选中的 active change，并进入 `nextCommand` 指向的永久入口。不要把状态命令当作恢复入口直接推进。
- 如果 probe 返回 `ask_user`，只问一个简短问题并等待用户回复。
- 如果 probe 返回 `out_of_scope` 或 `none`，不要进入 Comet workflow。
- 如果配置或状态无效且没有 `nextCommand`，停止并报告原因；不要猜测另一个 workflow。
- 不能只因为存在 active change 就把无关任务挂到该 change。Native 的未提交改动由 Native 入口检查，不由探针自动归因。
</comet-ambient-resume>
