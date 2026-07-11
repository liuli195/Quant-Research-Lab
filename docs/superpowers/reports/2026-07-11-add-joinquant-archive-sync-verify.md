---
comet_change: add-joinquant-archive-sync
result: pass
implementation_head: 3289e2c3cdf196efa4414bbc17cb9ab99364c21a
---

# 聚宽归档与增量同步验证报告

## 结论

| 维度 | 结果 | 证据 |
|---|---|---|
| 完整性 | PASS | OpenSpec（规格）任务 36/36 完成；13/13 条要求和 43/43 个场景均有实现、测试或真实 PoC（概念验证）证据 |
| 正确性 | PASS | full（完整）验证 194 项通过；OpenSpec strict（严格规格）通过；内存 E2E（端到端）门禁通过 |
| 一致性 | PASS | 实现遵守单一仓库 Skill（技能）、页面对象目录、单一路径归因所有权、原始证据加 Parquet（列式文件）、无持久 DuckDB（分析数据库）和 Windows 04:00 调度设计 |

未发现 CRITICAL（严重阻断）、IMPORTANT（重要阻断）或 WARNING（警告）问题，可以进入分支收尾。

## 关键验收证据

- 最终提交头 `3289e2c` 上运行仓库 full（完整）入口：194 项测试全部通过，OpenSpec strict（严格规格）通过，退出码为 0。
- `self-test` 使用生产同步核心在临时目录完成首次 `committed`、第二次 `unchanged`、DuckDB 内存查询和 CSV（表格文件）导出；门禁通过，约 1.62 秒，峰值约 26.7 MB，临时目录已删除。
- 真实回测 115 通过离线严格校验：归因日志 1631 条，`run_end.total_value` 和 `cash` 与 Research（研究环境）最终资金关联，门禁无例外。
- 真实活动模拟交易 `etf_factor_rotation` 通过离线严格校验：只保留启动生命周期拥有的 `default-full-run` 单一路径 30 条归因记录；另外 4 个历史回测/实验文件共 4913 条未进入模拟交易目录。
- 模拟交易清单强制保存完整代码历史映射；删除映射、加入非所属归因源文件、断序、边界冲突或路径/Token（标识）不符都会使离线校验失败。
- 正式 `JoinQuantArchiveSync` 计划任务已安装并复核：北京时间每天 04:00，失败后每 30 分钟重试，最多 3 次，调用仓库内同一 Skill 脚本。
- Git LFS（大文件存储）执行 `git lfs fsck` 通过；大文件恢复验证和压缩比记录见 build evidence（实施证据）。

## 归因所有权专项复核

- 模拟交易归因写入器只从生命周期开始时最后生效的代码版本解析，后续代码历史中出现的回测或实验路径不下载、不落盘。
- 回测归因只读取目标源码指定的单一路径，不扫描替代文件；完成回测还必须关联最终资产。
- 当前模拟交易归因源文件只有一份原始 JSONL（逐行数据）和一份查询 Parquet，不存在旧的 `attribution-source-jsonl.gz` 多源归档。
- 增量合并中的 `compacted_after_time_overlap`（合并后的时间重叠）只属于内存结构化数据；不可变原始 Research 响应仍保存并校验 `after_time_overlap`（时间重叠）。定向提交后复核测试通过，未放宽离线校验器。

## 审查与安全

- Cross-Agent Review（跨代理审查）以 `spec-alignment`（规格对齐）和 `implementation-correctness`（实现正确性）两个独立角色执行。
- 审查指出的命令分派落空和模拟交易索引未强制落盘问题已按 TDD（测试驱动开发）修复；新增测试先失败后通过。
- 审查对代码历史空值防护和增量合并模式的两项判断，经当前源码和定向复现确认不成立，均未通过放宽门禁处理。
- 变更文件中未发现硬编码账号、密码、Token、API Key（接口密钥）、明文 Cookie（浏览器凭证）或 `storage_state`（浏览器状态导出）。认证密文位于仓库外并受 Windows DPAPI（数据保护接口）保护。

## 设计一致性说明

- OpenSpec delta spec（增量规格）是需求事实源；技术设计中的页面边界、单一 CLI（命令行入口）、日志分层、增量同步、紧凑存储和内存 E2E 均已实现。
- 技术设计第 2 节的目录示意只列核心数据模块，`scheduler.py` 和 `selftest.py` 在第 10、12 节分别定义并已落地；这属于示意省略，不构成行为或规格漂移。
- 本地能力只归档和复盘聚宽云端结果，没有把本地计算声明为正式回测或模拟交易。

## 已知外部状态

- `etf_factor_rotation` 活动模拟交易页面没有性能面板，清单以页面能力证据标记 `performance_profile: unsupported_api_version`；这是允许的显式例外，不代表日志遗漏。
- 积分日志没有默认下载，也未在本次验证中消费积分；只有调用者指定对象、类型和范围并确认聚宽当次价格后才允许执行。
