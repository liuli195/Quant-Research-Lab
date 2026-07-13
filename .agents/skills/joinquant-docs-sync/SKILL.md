---
name: joinquant-docs-sync
description: Use when 用户或 Agent 需要预览、同步或校验聚宽官方帮助文档，生成可离线查询的 API（接口）索引，检查文档变化，或维护 `docs/joinquant-api/` 本地快照。
---

# 聚宽文档同步

所有动作只调用 `scripts/jq_docs_sync.py`；不要在对话中另写抓取脚本。只同步 `references/sources.json` 明确列出的聚宽官方公开页面，不登录、不读取 Cookie（浏览器凭证）或 Token（访问令牌）。

## 执行流程

1. 先运行 `preview`（预览）。读取结果中的新增、修改、删除文档和 API（接口）稳定键；预览不得写入目标目录。
2. 只有调用者已明确要求同步时才运行 `sync`（同步）。任一来源未通过正文、最小长度或错误页门禁时停止，不覆盖上次完整版本。
3. 同步后立即运行 `verify`（校验），从文件重新计算 SHA-256（完整性摘要）并核对清单与 API（接口）索引。
4. 修改脚本、来源清单或转换规则后运行 `self-test`（自检）。该入口使用内置离线页面，在临时目录覆盖预览、首次同步、重复同步、校验和篡改检测，不访问网络。

统一命令：

```powershell
& .\.venv\Scripts\python.exe .agents\skills\joinquant-docs-sync\scripts\jq_docs_sync.py <command> --help
```

默认目标目录是 `docs/joinquant-api/`。需要试验其他来源或目标时，使用 `--sources` 和 `--output` 传入明确路径；不要临时修改生产清单。

## 结果判断

- `preview`（预览）：只报告差异；出现 `failed` 时不得继续同步。
- `sync`（同步）：`changed` 只列出实际写入文件；内容摘要不变时必须是空列表。
- `verify`（校验）：只有 `status=ok` 才能声称本地快照完整。
- API（接口）稳定键使用 `source_id:kind:name`，同名函数在不同来源必须保留为不同条目。

## 安全边界

- 不安装浏览器或 Python（编程语言）依赖；缺失时报告项目 `.venv`（虚拟环境）的具体缺项。
- 不把本地文档或索引当作正式回测、模拟交易或实时 JQData（聚宽本地数据）结果。
- 不自动删除来源清单之外的文件，不自动提交 Git（版本管理）变更。
- 首次实际同步或 API（接口）数量明显下降时，先展示预览并等待用户确认。

默认官方来源和输出文件映射见 `references/sources.json`。
