# 聚宽归档同步操作说明

当前统一入口：

```powershell
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py <command>
```

先用 `<command> --help` 查看实时参数，不从文档复制过时参数。

首次认证使用可见浏览器：

```powershell
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py auth
```

认证状态只保存在 `%LOCALAPPDATA%\QuantResearchLab\joinquant-playwright`。`auth` 成功后只筛选 JoinQuant 域的会话 Cookie（浏览器凭证），立即用 Windows DPAPI（数据保护接口）加密；只有当前 Windows 用户能解密。实现不保存账号密码，不导出或加载明文 `storage-state.json`，也不打印 Cookie。`--profile` 指向仓库内时会在浏览器启动前拒绝。已有有效状态时可用 `auth --headless` 做无界面复核；失效时返回 `auth_required`，重新执行一次可见认证。

## 边界

- 日志传输或 JSON 解析失败时，生产入口先把 `responseText` 原文、SHA256、可明确恢复的记录和错误位置保存到 `.local/joinquant-sync/failures/`，再返回失败；该目录不会推进对象游标。

- 历史回测必须同时指定 `--strategy` 和 `--target`，不扫描或默认同步全部回测。
- 普通日志先走免费分页；到 1000 条时继续探测下一页，源端明确结束才标记 `complete`。
- 回测代码启用 `enable_profile()` 时保存完整“性能分析”面板文本；活动模拟交易页面没有性能页签时记录页面能力证据并标记 `unsupported_api_version`。
- 归因日志单独校验。策略没有归因写入器时标记 `missing_at_source`，不能以普通日志代替。
- 模拟交易完整保存全部代码历史映射，但只读取启动生命周期实际初始化的单一归因路径；后续代码版本中仅出现、却不属于该模拟交易生命周期的历史回测或实验路径，不得下载或保存到模拟交易目录。
- 积分下载只做所选数据集和范围的价格预览；用户确认前不得提交下载。聚宽当前按完整日志固定收费，不支持远端分段计价；命令会明确显示该限制，下载后只保留指定行范围。
- 自动下载失败时，用 `verify --import-file` 导入同一目标的人工下载文件；两条路径都失败则停止提交。

## 官方摘要与报告目录

回测同步通过 Browser（浏览器）在详情页点击官方“导出 CSV”入口，下载结果只保存为 `data/official-summary.csv`。该文件属于官方源数据；`reports/` 只存放人工或 Agent（代理）生成的分析报告。生产入口和校验入口均拒绝 `reports/` 下的旧位置，也不提供双路径读取。

官方摘要用于保留页面展示口径和交叉校验，不是 Research（研究环境）明细的替代品：

- 分析累计策略收益或基准收益时，以 `results` 为结构化来源，官方摘要用于核对页面展示值。
- 分析当日盈亏时，读取 `balances.total_value` 的相邻交易日差额，并用 `orders` 解释买卖构成；官方摘要只作页面口径复核。
- 分析现金、持仓、单笔订单、自定义记录、风险、分期风险或日志时，读取对应 Research 或日志数据集，官方摘要不能推导这些内容。

迁移既有归档时，移动文件后必须保持 SHA256（完整性摘要）不变，并同时更新 `manifest.json`（清单）的文件路径、来源、编码、表头、行数和关联数据集。迁移完成后必须全仓确认旧文件与旧引用均为零。

## 常用操作

```powershell
# 只读列出候选；不会触发历史下载
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py list-targets --strategy <策略>

# 只同步一个明确历史目标
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py sync-backtest --strategy <策略> --target <页面序号或详情URL>

# 增量同步全部活动模拟交易
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py sync-active-simulations --repository .

# 计划任务实际发布入口；同一命令也用于恢复未完成的 PR Flow
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py scheduled-sync-pr --repository .

# 查询和按需导出
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py query --object <对象目录> --dataset <数据集>
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py export-csv --object <对象目录> --dataset <数据集> --fields <字段列表> --destination <文件>

# 只有 capped_free 才允许生成当次价格预览；范围为起始行:结束行，左闭右开
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py paid-log preview --object <对象目录> --type normal_log --range 1000:1200

# 逐字确认预览中的积分、完整日志远端收费范围和本地保留范围后才执行
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py paid-log download --preview-id <预览ID> --confirm --destination <补充日志.jsonl.gz>
```

`scheduled-sync-pr` 先获取 `%LOCALAPPDATA%\QuantResearchLab\joinquant-archive-sync\` 的运行锁，再检查 GitHub（代码托管）凭证、PR Flow runtime（运行时）、远端 `main` 和 JoinQuant（聚宽）登录。它优先使用 `codex plugin list --json` 发现 PR Flow，Codex 不可用时回退 `claude plugin list --json`；两者都不可用时在同步前失败。运行结果原子保存在同目录的 `last-run.json`：`noop` 表示没有变化，`run_locked` 表示已有运行，`failed` 的 `recovery_command` 仍指向同一入口。不要把外部输出或凭证复制进该文件。

安装计划任务前，`self-test` 和目标回归必须成功。安装命令会校验 Windows 时区为 `China Standard Time`，沿用仓库 `.venv`、每天 04:00、每 30 分钟最多重试 3 次的原生任务；不写常驻轮询或无限重试。`sync-active-simulations` 继续作为手动入口。现有生产任务只有在实现 PR 合并后获得单独授权，才能用 `schedule-uninstall` 和 `schedule-install` 迁移；实现或测试阶段不得替换、启停或删除它。

```powershell
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py self-test
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py schedule-install --repo-root .
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py schedule-status
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py schedule-uninstall
```

## 人工补录

```powershell
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py verify `
  --import-file <下载文件> `
  --stage-only .local\joinquant-sync\manual-import
```

命令输出暂存文件路径、字节数和 SHA256（摘要）。暂存本身不会修改 manifest（清单）；只有对象同步或明确的对象级补录流程把该文件登记进清单并再次运行 `verify --object` 后，查询入口才会读取它。

## 已验证来源

- 聚宽回测详情页：代码、免费日志分页、页面结构化结果，以及保存到 `data/official-summary.csv` 的官方下载。
- 聚宽 Research（研究环境）官方 `get_backtest`：状态、参数、收益、持仓、订单、`record`、风险和分期风险。
- 官方 API 文档：<https://cdn.joinquant.com/help/img/JoinQuantAPI.pdf>

真实验证记录见 `docs/research/joinquant-archive-sync-poc.md`。
