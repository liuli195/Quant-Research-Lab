# Worktree Python Environment

## Purpose

让开发者在仓库根目录集中维护一次 Python 环境，并由根目录 `.worktrees/` 中的工作树安全复用。

## Requirements

### Requirement: 工作树共享主仓库 Python 环境

仓库 SHALL 将主仓库根目录 `.venv` 作为唯一 Python 依赖实体，并让根目录 `.worktrees/` 中的工作树通过目录链接复用该环境，而不复制或安装独立依赖。

#### Scenario: 初始化主仓库环境

- **WHEN** 开发者在主仓库根目录运行 `scripts/setup-worktree.ps1`
- **THEN** 脚本创建或更新根目录 `.venv` 并记录其对应的依赖清单

#### Scenario: 初始化工作树环境

- **WHEN** 开发者在依赖清单一致的链接工作树中运行 `scripts/setup-worktree.ps1`
- **THEN** 脚本将工作树 `.venv` 链接到主仓库根目录 `.venv`，且不安装或修改共享环境中的依赖
### Requirement: 工作树拒绝不安全的共享环境

工作树初始化 MUST 在共享环境缺失、陈旧或依赖清单与主仓库不一致时停止，并提示开发者先在主仓库根目录准备环境。

#### Scenario: 共享环境不可复用

- **WHEN** 工作树运行 `scripts/setup-worktree.ps1`，且共享环境缺失、未对应当前依赖清单或工作树清单与主仓库不同
- **THEN** 脚本以失败状态停止，不创建工作树环境且不修改主仓库环境
### Requirement: 共享环境支持仓库验证入口

Build and Verify SHALL 仅通过工作树的共享 `.venv` 链接执行构建和验证，不负责安装依赖。

#### Scenario: 从工作树执行验证

- **WHEN** 工作树已链接主仓库共享环境并运行 Build and Verify
- **THEN** 验证通过该链接完成真实检查流程，且不会安装依赖
