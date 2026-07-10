# Build evidence（实施证据）

## 已验证

- 仓库 full（完整）验证：152 项通过；OpenSpec strict（严格规格）通过。JoinQuant 专项回归 149 项通过；覆盖 1000 条免费窗口滚动后的原始页累计、Research 来源链、发布编排入口，以及畸形响应先保存原文和恢复结果再报告失败。
- Codex（代码代理）与 Claude（代码代理）从各自仓库 Skill（技能）发布入口启动项目 `.venv` 子进程；均完成生产编排 `sync_all_active_simulations`、首次 `committed`、第二次 `unchanged`、DuckDB 内存查询和 CSV 导出，约 1.5 秒、峰值约 26.7 MB。
- 真实历史回测 115 的 PoC 已取得 1289 行结果、1289 行资金、2386 行持仓、378 行订单、1000 条完整普通日志和 1631 条可查询归因日志；这些数据证明外部链路可行。
- 真实活动模拟交易 `ETF动态调仓` 的 PoC 已取得连续 1000 条免费日志窗口并确认源代码未实现归因写入器，因此归因应为 `missing_at_source`。
- `etf_factor_rotation` 活动模拟交易的归因文件属于其他运行，系统返回 `AttributionIncomplete` 并拒绝提交，没有把陈旧归因误标完整。
- Windows 临时计划任务已真实创建、执行同一 `self-test`、读取退出码并清理；正式 `JoinQuantArchiveSync` 任务未安装。
- Git LFS（大文件存储）属性覆盖 gzip、Parquet 和 CSV；远端 LFS locks 接口可用。提交前隔离 detached checkout（分离检出）已把远端现有 LFS CSV 还原为真实内容而非指针，并从 Codex、Claude 两个入口通过 `self-test`。
- 未消费积分，未提交 Cookie、Token、密码或浏览器配置。

## 外部受限 / 待验证

- 安全改造后不再导出或加载 `storage-state.json`。当前专用 Persistent Context（持久化浏览器上下文）未完成重新登录，`auth` 在 240 秒后按约定返回 `auth_required`；因此新的 Jupyter 同源 AJAX Research 通道尚待登录后执行一次真实同步复验。
- 当前严格契约新增 Research 单次原始返回包、Browser + Research 双围栏以及普通日志原始页逐值复核。旧 PoC 归档生成于该契约之前，回测 115 和活动模拟交易当前均按预期返回 `research response evidence is invalid`；必须重新登录后用生产入口重同步，不能沿用旧清单声称已通过最新严格验证。
- 新归档 LFS 对象的远端恢复必须在重新登录生成最新严格归档、获得 push（推送）授权并实际上传后才能最终证明。
