# 云端环境准备

本仓库复用 my-agent-skills（代理技能仓库）的项目环境准备入口，不复制另一套安装器。按仓库规则使用已准备且获准的项目 `.venv`（虚拟环境）中的 Python（编程语言）3.12，不使用系统解释器；环境不可用时停止并核对原有准备流程，不自行安装或绕过权限要求。

Linux（开源系统）示例：

```sh
.venv/bin/python /path/to/my-agent-skills/scripts/cloud_bootstrap.py init --project "$PWD"
.venv/bin/python /path/to/my-agent-skills/scripts/cloud_bootstrap.py update --project "$PWD"
```

Windows（视窗系统）示例：

```powershell
.\.venv\Scripts\python.exe C:\path\to\my-agent-skills\scripts\cloud_bootstrap.py init --project (Get-Location).Path
.\.venv\Scripts\python.exe C:\path\to\my-agent-skills\scripts\cloud_bootstrap.py update --project (Get-Location).Path
```

`--project` 必填；可用 `--manifest <项目环境清单.json>` 指定清单，省略时使用技能仓库脚本旁的生产清单。`init`（初始化）和 `update`（同步）只准备已声明的项目依赖，不表示升级全部依赖或公共工具。旧公共安装目录、运行包、技能导出与选择升级参数已移除；旧参数、旧配置空键或未知项目明确失败，不执行准备。安装阶段失败返回非零且不报告就绪，可能保留真实部分状态，不保证回滚。

项目入口对应关系放在技能仓库项目环境清单中，实际依赖使用本仓库的依赖文件、锁文件和版本清单。Windows 仍执行原有 PowerShell（命令解释器）准备入口；Linux 仅准备适用的项目依赖，不替代 Windows 专属工具、现有正式验证或业务验收。本仓项目不安装未声明的软件包。

插件安装、更新及发现使用原生插件能力与 plugin-sync（插件同步）现有流程；环境准备不下载插件、不安装受控全局软件包、不恢复旧安装状态或导出技能链接。数据存储稳定绑定或显式运行目录接入按当前 data-store（数据存储）技能自身公开能力单独执行；初始化不自动绑定、不迁移业务数据，也不修改现有项目桥接。任务临时资源不能作为永久项目链接，真实项目接入仍须单独核对和授权。

工作区使用 Git（版本管理）原生命令创建、进入与清理，创建后显式运行统一初始化。无需 environment.toml（环境声明文件）或自定义 .codex（客户端配置目录）配置。不要把整套虚拟环境复制到工作树：主仓库维护依赖实体，工作树在依赖清单一致且主环境有效时通过链接复用；缺失、陈旧或不一致时停止，先准备主环境，不从链接工作树改写共享依赖。构建验证仅使用现有共享环境，不负责安装依赖。

环境准备不启动量化业务，不执行交易，也不改变正式回测、模拟交易和本地研究的原有平台边界。MCP（模型上下文协议）不安装、不配置。准备成功只表示项目依赖准备成功，不表示插件、数据接入或量化业务已验收。详见技能仓库 docs/cloud-environment.md（云端环境说明）。
