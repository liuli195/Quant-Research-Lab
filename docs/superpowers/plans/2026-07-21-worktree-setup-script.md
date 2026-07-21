# 工作树安装脚本 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个 Orca 工作树创建独立 Python 虚拟环境并安装项目声明的运行和开发依赖。

**Architecture:** 仓库根目录的 `scripts/setup-worktree.ps1` 从自身位置确定项目根目录，避免依赖 Orca 的工作目录。脚本只在缺失时创建 `.venv`，之后始终通过该环境的 Python 安装版本化 requirements；Orca 仅运行这个版本管理脚本。

**Tech Stack:** PowerShell 7、Python 3.12、venv、pip、Orca 项目 Setup script。

## Global Constraints

- 脚本必须位于 `scripts/setup-worktree.ps1`，随仓库版本管理。
- 仅支持 Windows 上的 Python 3.12，使用 `py -3.12` 创建缺失的 `.venv`。
- 不运行构建、测试、浏览器安装或聚宽操作。
- 不访问 `.local`、Cookie、聚宽认证信息或其他私密本地状态。
- 每个工作树创建并使用自身 `.venv`；不得共享主工作树环境。
- 任何失败必须停止并以非零退出状态返回给 Orca。

---

### Task 1: 添加可重复执行的工作树依赖安装脚本

**Files:**
- Create: `scripts/setup-worktree.ps1`
- Test: PowerShell 在临时工作树中执行 `scripts/setup-worktree.ps1`

**Interfaces:**
- Consumes: 当前工作树内的 `requirements.txt`、`requirements-dev.txt` 与 Windows Python launcher `py`。
- Produces: 当前工作树内可执行的 `.venv\Scripts\python.exe`，其中已安装 requirements 声明的依赖。

- [x] **Step 1: 添加脚本失败前提检查**

在工作树中调用缺失的 `scripts\setup-worktree.ps1`，确认 PowerShell 以“找不到脚本”错误失败。

- [x] **Step 2: 创建最小安装脚本**

创建 `scripts/setup-worktree.ps1`，使其从脚本位置推导仓库根目录，仅在 `.venv\Scripts\python.exe` 缺失时创建 Python 3.12 虚拟环境，随后更新 pip 并安装两个 requirements 文件。

- [x] **Step 3: 在隔离工作树运行脚本**

在 Orca 工作树 `add-worktree-setup-script` 运行脚本。

Expected: 命令以状态码 0 退出，且生成 `.venv\Scripts\python.exe`。

- [x] **Step 4: 验证虚拟环境安装了声明的开发依赖**

运行：

```powershell
& .\.venv\Scripts\python.exe -c "import jsonschema, pytest, ruff, vectorbt; print('worktree setup ok')"
```

Expected: 输出 `worktree setup ok` 并以状态码 0 退出。

- [x] **Step 5: 验证依赖安装失败会传递非零退出状态**

临时移走 `requirements-dev.txt` 后运行脚本，观察 pip 因找不到该文件失败并返回状态码 `1`；随后恢复原文件。确认脚本没有吞掉该非零退出状态。

- [x] **Step 6: 提交脚本与设计文档**

```powershell
git add scripts/setup-worktree.ps1 docs/superpowers/specs/2026-07-21-worktree-setup-script-design.md docs/superpowers/plans/2026-07-21-worktree-setup-script.md
git commit -m "新增：工作树依赖安装脚本"
```

Expected: Git 创建包含脚本、已确认设计和计划的提交。

### Task 2: 合入 main 后启用 Orca 项目初始化

**Files:**
- Modify: Orca 本地项目设置（项目 `Quant-Research-Lab` 的 Setup script）
- Test: 一个新 Orca 工作树的 setup 运行记录

**Interfaces:**
- Consumes: `scripts/setup-worktree.ps1`。
- Produces: Orca 新建工作树时执行的命令 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup-worktree.ps1`。

- [x] **Step 1: 记录现有配置为不适用**

`orca repo show` 显示当前 Setup script 为 `pnpm install`，与无根 `package.json` 的 Python 仓库不匹配。

- [ ] **Step 2: 合入 main 后在 Orca 项目设置中替换 Setup script**

在 Orca 的 Quant-Research-Lab 项目设置中，将 Setup script 设置为：

```text
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup-worktree.ps1
```

保存设置，不修改其他 hook、分支或工作树选项。

- [ ] **Step 3: 通过 Orca 新建验证工作树**

```powershell
orca worktree create --repo path:"D:\My Project\Quant-Research-Lab" --name verify-orca-setup --no-parent --setup run --json
```

Expected: Orca setup 以状态码 0 完成，工作树中存在 `.venv\Scripts\python.exe`。

- [ ] **Step 4: 移除验证工作树**

```powershell
orca worktree rm --worktree path:<验证工作树绝对路径> --force --json
```

Expected: Orca 返回成功，验证工作树被移除。

## Plan Self-Review

- Spec coverage: Task 1 实现并验证仓库脚本；Task 2 替换并验证 Orca 的错误 `pnpm install` 配置；非目标在全局约束中明确排除。
- Placeholder scan: 无占位标记、模糊实现步骤或未定义接口。
- Consistency: 所有 Orca 调用都使用同一仓库路径和 `scripts/setup-worktree.ps1`；所有 Python 安装均通过工作树自己的 `.venv`。
