# 工作树安装脚本设计

## 目标

为每个 Orca 工作树创建独立的 Python 虚拟环境，并安装仓库声明的运行与开发依赖。

## 范围

- 新增仓库版本管理的 PowerShell 脚本：`scripts/setup-worktree.ps1`。
- 在合入 `main` 后，Orca 的项目 Setup script 调用该脚本。
- 脚本在工作树根目录创建缺失的 `.venv`，使用 Python 3.12。
- 脚本在该 `.venv` 中升级 pip，并安装 `requirements.txt` 与 `requirements-dev.txt`。

## 非目标

- 不运行构建、测试或浏览器安装。
- 不读写 `.local`、Cookie、聚宽认证信息或其他本地私密状态。
- 不修改主工作树、Git 分支或 Orca 工作树目录。
- 不共享虚拟环境；每个工作树拥有自己的 `.venv`。

## 脚本行为

1. 从脚本自身路径推导仓库根目录，避免依赖调用时的当前目录。
2. 若 `.venv\Scripts\python.exe` 不存在，执行 `py -3.12 -m venv .venv`。
3. 通过 `.venv\Scripts\python.exe` 更新 pip。
4. 通过同一解释器安装 `requirements.txt` 与 `requirements-dev.txt`。
5. 任一步骤失败即以非零退出状态停止，供 Orca 报告工作树初始化失败。

## Orca 配置

项目 Setup script 设置为：

```text
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup-worktree.ps1
```

Orca 当前的 `pnpm install` 不适用于本仓库，因为仓库没有根级 Node.js package manifest。

## 合入后验证

合入 `main` 后，在 Orca 项目设置中将 Setup script 更新为本文件所列命令。随后创建一个基于 `main` 的新工作树，确认 Orca 成功运行该脚本、生成 `.venv\Scripts\python.exe` 并完成依赖安装；验证工作树完成后移除。
