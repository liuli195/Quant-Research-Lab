---
name: joinquant-archive-sync
description: Use when 用户或 Agent 需要认证聚宽、列出候选对象、同步明确指定的历史回测、增量同步活动模拟交易、补充日志、验证归档、查询 Parquet、导出 CSV 或管理北京时间计划任务。
---

# 聚宽归档同步

所有动作只调用 `scripts/jq_sync.py`；不要在对话中另写抓取脚本。正式回测和模拟交易仍只在聚宽云端运行。

## 执行流程

1. 使用仓库 `.venv` 运行 `auth`。返回 `auth_required` 时停止同步并让用户重新登录；不要读取或打印 Cookie（浏览器凭证）。
2. 历史对象先运行 `list-targets`，再让调用者或 Agent 指定页面序号或详情 URL（链接）。拒绝空目标、`latest`、`all` 和裸远端 ID；只对明确目标运行 `sync-backtest`。
3. 手动模拟交易同步仍通过 `sync-active-simulations` 扫描全部活动对象；计划任务发布通过 `scheduled-sync-pr` 在仓库外专用 worktree（工作树）中完成批次门禁和 PR Flow（拉取请求流程）。
4. 使用 `verify` 检查人工补录或现有归档。只有 manifest（清单）门禁通过后，才可用 `query` 或 `export-csv`。
5. 使用 `self-test` 做无网络内存回归。生产入口通过后，才使用 `schedule-install`；用 `schedule-status` 查看，用 `schedule-uninstall` 卸载。

统一命令：

```powershell
& .\.venv\Scripts\python.exe .agents\skills\joinquant-archive-sync\scripts\jq_sync.py <command> --help
```

## 日志与积分

- 归因日志默认全量同步。回测只读取目标源码指定的单一文件，并把结束资产与 Research（研究环境）最终资金记录关联。模拟交易完整保存代码历史映射，但只读取启动生命周期实际初始化的单一归因路径；后续代码版本中仅出现的回测或实验路径不得下载。日志必须校验 Token（标识）、连续序号、时间顺序、唯一 `run_start` 和终态 `run_end`。启动代码没有写入器时只能标记 `missing_at_source`，普通日志不能替代归因日志。
- 普通日志默认取得 1000 条并探测下一页。有结束证据才是 `complete`；免费范围无法证明结束时是 `capped_free`。
- 代码启用 `enable_profile()` 时，回测必须等待“性能分析”页签就绪并保存完整面板文本；模拟交易页面没有该能力时用页面证据标记 `unsupported_api_version`，不得统一硬编码。
- 积分下载必须绑定运行、日志类型、范围和当次报价。先预览，再取得对该报价的明确确认；只下载确认部分。不得默认消费积分或复用旧确认。
- 聚宽当前只按“完整日志”固定报价，不提供远端分段价格。`paid-log preview` 会同时显示完整日志积分和本地保留范围；只有调用者接受这项事实并执行带 `--confirm` 的 `paid-log download` 才会扣分，最终只保留所选行范围。

## 快速索引

| 目的 | 命令 |
|---|---|
| 登录/复核登录 | `auth` |
| 只读列出候选 | `list-targets` |
| 同步指定回测 | `sync-backtest` |
| 同步活动模拟交易 | `sync-active-simulations` |
| 计划任务归档发布/恢复 | `scheduled-sync-pr` |
| 校验/人工补录 | `verify` |
| 查询归档 | `query` |
| 按范围导出 | `export-csv` |
| 积分日志预览/确认下载 | `paid-log preview` / `paid-log download --confirm` |
| 内存端到端自检 | `self-test` |
| 安装/查看/卸载 04:00 任务 | `schedule-install` / `schedule-status` / `schedule-uninstall` |

## 状态解释

- `complete`：有明确终止和摘要证据。
- `capped_free`：免费范围已尽，不能声称全量。
- `missing_at_source`：源端确实没有，并有证据。
- `unsupported_api_version`：当前接口明确不支持。
- `failed`：保留上次完整版本，从未推进的游标重试。
- `noop`：本批次没有可发布变化，不创建分支、提交或 PR（拉取请求）。
- `run_locked`：已有同一发布入口持锁，本次安全跳过。

需要命令示例、认证恢复和调度操作时读 `references/operations.md`；需要目录、数据集状态和门禁语义时读 `references/manifest.md`。
