# 聚宽归档同步操作说明

当前统一入口：

```powershell
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py <command>
```

首次认证使用可见浏览器：

```powershell
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py auth
```

认证状态保存在 `%LOCALAPPDATA%\QuantResearchLab\joinquant-playwright\storage-state.json`。该文件含敏感 Cookie（浏览器凭证），只能留在仓库外，禁止提交、打印或复制到归档。已有有效状态时可用 `auth --headless` 做无界面复核；失效时返回 `auth_required`，重新执行可见认证。

## 边界

- 历史回测必须同时指定 `--strategy` 和 `--target`，不扫描或默认同步全部回测。
- 普通日志先走免费分页；到 1000 条时继续探测下一页，源端明确结束才标记 `complete`。
- 归因日志单独校验。策略没有归因写入器时标记 `missing_at_source`，不能以普通日志代替。
- 积分下载只做所选数据集和范围的价格预览；用户确认前不得提交下载。
- 自动下载失败时，用 `verify --import-file` 导入同一目标的人工下载文件；两条路径都失败则停止提交。

## 人工补录

```powershell
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py verify `
  --import-file <下载文件> `
  --stage-only .local\joinquant-sync\manual-import
```

命令输出文件路径、字节数和 SHA256（摘要）。后续任务会把这些证据纳入正式 manifest（清单）。

## 已验证来源

- 聚宽回测详情页：代码、免费日志分页、页面结构化结果和官方下载。
- 聚宽 Research（研究环境）官方 `get_backtest`：状态、参数、收益、持仓、订单、`record`、风险和分期风险。
- 官方 API 文档：<https://cdn.joinquant.com/help/img/JoinQuantAPI.pdf>

真实验证记录见 `docs/research/joinquant-archive-sync-poc.md`。
