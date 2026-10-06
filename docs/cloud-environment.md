# 云端环境准备

本仓库复用 my-agent-skills 的公共初始化入口，不复制另一套安装器。

```sh
python /path/to/my-agent-skills/scripts/cloud_bootstrap.py init --root /workspace/shared/cloud-skills/managed --project "$PWD"
```

更新声明版本后，将 init 换成 update。升级公共工具或技能则在同一入口使用 update --latest --only 指定项；不自动升级本项目全部依赖。

项目入口对应关系放在技能仓库公共初始化清单中，实际依赖使用本仓库的 requirements、锁文件和版本清单。Windows 仍执行原有 PowerShell 准备入口；Linux 仅准备适用的 Python/npm 依赖，不替代 Windows 专属工具和验收。

工作区使用 Git 原生命令创建、进入与清理；创建后显式运行统一初始化。无需 environment.toml 或自定义 .codex 配置。不要把整套虚拟环境复制到工作树。

MCP 不安装、不配置。详见技能仓库 docs/cloud-environment.md。
