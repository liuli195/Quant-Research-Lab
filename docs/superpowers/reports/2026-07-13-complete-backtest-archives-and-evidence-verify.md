# complete-backtest-archives-and-evidence 验证报告

## 结论

| 维度 | 状态 | 证据 |
|---|---|---|
| 完整性 | PASS（通过） | 6/6 个任务完成，2/2 条需求已实现 |
| 正确性 | PASS（通过） | 7/7 个规格场景有实现、测试、文档或真实归档证据 |
| 一致性 | PASS（通过） | 实现符合 OpenSpec design（开放规格设计）和既有聚宽归档技术设计 |

未发现 CRITICAL（严重）、WARNING（警告）或 SUGGESTION（建议）问题。变更可进入 archive（归档）确认。

## 完整性

- `openspec status --change complete-backtest-archives-and-evidence --json` 显示 `spec-driven`（规格驱动）流程完整，proposal、design、specs、tasks 四类产物均为 `done`。
- `openspec instructions apply --change complete-backtest-archives-and-evidence --json` 显示 6/6 个任务完成。
- `openspec validate complete-backtest-archives-and-evidence --strict` 严格校验通过。
- 从 `base_ref` 到当前分支共 417 个变更文件，范围与官方摘要全量迁移、7 个历史对象补录、同步契约、文档和测试一致；两份无关研究文档未纳入变更。

## 正确性

### 历史运行同步必须由明确目标驱动

- `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/sync_pipeline.py:229` 对 `latest_backtest_id`（最新回测编号）采用单调更新；`tests/joinquant_sync/test_sync_pipeline.py:11` 覆盖 115 补录 88 后仍保持 115。
- `tests/joinquant_sync/test_archive.py:23`、`:30`、`:207` 覆盖缺失目标、`latest`（最新）选择器、有效序号或详情链接和非页面目标。
- 7 个目标 `1、9、75、76、83、87、88` 均通过发布入口逐项同步并逐项校验，没有使用隐式批量选择器。
- 策略 001 本地目录连续覆盖 1–115，缺失编号为 0；`strategy_index.csv`（策略索引）的 `latest_backtest_id` 仍为 115。

### 官方回测摘要必须保留来源和用途边界

- `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/sync_pipeline.py:1577` 生成版本化证据，记录官方详情页来源、导出动作、编码、表头、行数及 `results`、`balances`、`orders` 关联数据集。
- `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/archive.py:1457` 和 `references/manifest.schema.json:169` 将 `data/official-summary.csv` 固定为唯一合法路径，并严格校验证据字段。
- `tests/joinquant_sync/test_sync_pipeline.py:1248`、`:1263`、`:1283` 覆盖新路径、拒绝旧位置和拒绝不完整证据；`tests/joinquant_sync/test_archive.py:226` 覆盖 schema（清单结构）契约。
- 全仓现有 127 个官方摘要文件全部位于 `data/`；旧位置文件为 0，旧完整路径文本引用为 0。120 个既有摘要迁移时保持原字节和 SHA256（完整性摘要），新旧清单均通过验证。
- `.agents/skills/joinquant-archive-sync/references/operations.md:31` 和 `references/manifest.md:66` 明确区分官方页面源数据、Research（研究环境）明细和人工报告；当日盈亏使用 `balances.total_value` 相邻交易日差额，并用 `orders` 解释交易构成。

## 一致性

- OpenSpec design（开放规格设计）的 6 项决策均已落实：索引单调更新、唯一数据路径、版本化证据、内容不变迁移、先迁移后逐项同步、保持单一 tweak（轻量变更）。
- 实现遵循 `docs/superpowers/specs/2026-07-11-joinquant-archive-sync-design.md` 的既有边界：唯一 CLI（命令行入口）、明确历史目标、manifest（清单）权威指针、部分归档门禁、Git LFS（大文件存储）和正式 self-test（自检）入口。
- 本次是既有能力的 tweak（轻量变更），没有新增独立架构；变更设计由当前 OpenSpec `design.md` 记录，既有技术设计仍可定位且无矛盾。
- `.comet.yaml` 配置为 `review_mode: off`，因此按工作流规则跳过自动 code review（代码审查）；本报告仍完成了完整规格映射、构建、测试、安全和边界检查。

## 执行证据

- Build（构建）：`.venv/Scripts/python.exe .build-and-verify/runtime/build_and_verify.py build --project .`，退出码 0。
- Full verify（完整验证）：`.venv/Scripts/python.exe .build-and-verify/runtime/build_and_verify.py verify --project . --full`，9 组检查全部通过；230 个 Pytest（测试）用例通过，3 个 OpenSpec（开放规格）对象通过，0 失败。
- 发布入口 E2E（端到端）：`.venv/Scripts/python.exe .agents/skills/joinquant-archive-sync/scripts/jq_sync.py self-test`，`gate=pass`、`idempotent=true`、`temporary_removed=true`。
- 策略 001 全量归档校验：115/115 个对象通过完整性检查，其中 108 个完整归档、7 个有来源失败证据的部分归档，校验错误为 0。
- 安全检查：新增 7 个归档目录未发现账号、密码、Cookie（浏览器凭证）、Authorization（授权头）或 access token（访问令牌）特征；认证状态仍位于仓库外。

## 最终评估

全部检查通过。没有必须修复或需要接受的偏差，已具备 archive（归档）条件；归档前仍需按 Comet 工作流完成分支处理选择和最终归档确认。
