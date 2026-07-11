---
comet_change: add-joinquant-archive-sync
role: technical-design
canonical_spec: openspec
archived-with: 2026-07-11-add-joinquant-archive-sync
status: final
---

# 聚宽归档与增量同步技术设计

## 1. 设计边界

需求和验收场景以 `openspec/changes/add-joinquant-archive-sync/specs/joinquant-archive-sync/spec.md` 为唯一事实源。本文只说明如何实现，不建立第二份需求规格。

正式回测和模拟交易继续只在聚宽云端运行。本地系统只负责认证后的读取、证据归档、完整性校验、增量同步、查询和按需 CSV 输出。

首个实施步骤是一次真实 PoC（概念验证）：选择一个明确指定、代码存在归因写入器的已完成回测，验证已登录页面、Research API（研究接口）、官方导出、Playwright（浏览器自动化）下载事件、完整归因日志和本地落盘。自动下载失败时验证人工导入同一证据包协议；两条路径都失败则停止后续实现。PoC 只证明外部链路可行，不进入常规端到端回归。

## 2. 发布结构

仓库只保留一份真实 Skill（技能）：

```text
.agents/skills/joinquant-archive-sync/
├── SKILL.md
├── requirements.txt
├── scripts/
│   ├── jq_sync.py
│   └── joinquant_sync/
│       ├── browser.py
│       ├── research.py
│       ├── research_cloud.py
│       ├── archive.py
│       └── query.py
└── references/
    ├── manifest.md
    └── operations.md

.claude/skills/joinquant-archive-sync
└── SymbolicLink -> ../../.agents/skills/joinquant-archive-sync
```

`jq_sync.py` 是唯一 CLI（命令行入口）。四个模块对应已确认的四个责任边界：

| 模块 | 责任 | 不负责 |
|---|---|---|
| `browser.py` | 登录、页面对象、代码、日志、官方下载 | 结构化结果校验、归档提交 |
| `research.py` | 结构化分页和事实表纯校验 | 页面登录、云端执行 |
| `research_cloud.py` | 通过已验证 Research 链路执行单次原始导出 | Browser 页面数据、归档提交 |
| `archive.py` | 导入、完整性校验、增量判定、清单提交 | 浏览器协议、查询展示 |
| `query.py` | Parquet 查询、DuckDB 视图、CSV 输出 | 抓取、修改清单 |

模块之间传递普通 Python 数据结构和文件路径，不增加抽象基类、Provider（提供方）框架或消息服务。Plugin（插件）、marketplace（市场）、缓存版本和外部发布均不创建。

仓库 `.venv` 是唯一 Python 运行时。Skill 的 `requirements.txt` 记录可复现依赖；实现复用现有 Playwright、Pandas 和 PyArrow，只新增已确认的 DuckDB。

## 3. CLI 契约

最小命令面如下：

```text
jq_sync.py auth
jq_sync.py list-targets --strategy <strategy_id>
jq_sync.py sync-backtest --strategy <strategy_id> --target <ordinal-or-url>
jq_sync.py sync-active-simulations
jq_sync.py verify --object <path>
jq_sync.py query --object <path> --dataset <name> [filters]
jq_sync.py export-csv --object <path> --dataset <name> [filters]
jq_sync.py paid-log preview --run <id> --type <type> --range <range>
jq_sync.py paid-log download --preview-id <id> --confirm
jq_sync.py scheduler install|status|uninstall
jq_sync.py self-test
```

历史同步没有无目标默认值，也不接受 `latest`。`list-targets` 只读取轻量列表；只有显式 `sync-backtest` 才下载目标对象。积分日志的 preview（预览）和 download（下载）分成两个命令，确认只对一次预览结果有效。

CLI 使用稳定退出状态供 Agent（代理）和计划任务判断：成功、参数/目标错误、`auth_required`、完整性失败和外部服务失败分别返回不同非零码。错误输出只包含对象、阶段和可操作原因，不输出 Cookie（浏览器凭证）或响应中的秘密字段。

## 4. 单次同步数据流

```text
明确目标
  → 读取页面对象和数据集预期清单
  → 保存前置远端清单
  → Browser + Research 分别写入隔离暂存目录
  → Research 单次原始返回先压缩保存并记录 SHA256，再解析为结构化事实
  → 逐数据集校验和状态判定
  → 重读后置远端清单和浏览器证据，并再次读取 Research 返回
  → 仅重试发生变化的数据集
  → 写入不可变文件
  → 原子替换 manifest.json
  → 刷新 DuckDB 视图或按需 CSV
```

Browser 和 Research 产生同一个 evidence bundle（证据包）格式。人工导入只替换“如何获得证据包”，后续导入、校验和提交完全复用生产路径。

PoC 完成前不猜测聚宽内部端点、选择器或下载名称。PoC 把真实请求、分页终止信号、文件格式和页面定位规则记录到 `references/operations.md`，生产适配器只实现已被真实对象证明的路径。

## 5. 对象身份与目录

```text
joinquant/strategies/<strategy_id>/
├── default_code.py
├── manifest.json
├── backtests/<page_ordinal>/
└── simulations/<simulation_id>/
```

- `strategy_id` 和 `simulation_id` 从仓库索引分配后不再变化。
- 回测使用策略页面内可复核序号。
- 远端传输 ID、详情 URL、名称和历史别名只进入清单。
- 发现远端 ID 变化时先用页面身份、所属策略、序号和代码摘要复核；匹配后更新别名，不创建重复目录。
- 策略、回测和模拟交易各自维护 manifest；仓库索引只负责从页面列表定位稳定本地键，不复制数据集明细。

可行性复核确认：聚宽策略页面只提供回测历史和模拟交易对象，官方 `get_backtest` 也只接受回测 ID 或模拟交易 ID；没有独立构建详情页、构建 ID 或下载接口。因此目录和 CLI 不创建 `builds/`、`sync-build`，避免把内部开发阶段误建模为远端页面对象。

## 6. Manifest 与完整性门禁

`manifest.json` 是读取和提交的权威指针，至少包含：

```json
{
  "schema_version": 1,
  "object": {"kind": "backtest", "local_id": "12", "status": "done"},
  "source": {"url": "...", "aliases": [], "observed_at": "..."},
  "fence": {"before_sha256": "...", "after_sha256": "..."},
  "code": {"path": "code.py", "sha256": "..."},
  "datasets": {
    "orders": {
      "required": true,
      "status": "complete",
      "rows": 0,
      "bytes": 0,
      "time_range": null,
      "pagination": {},
      "files": []
    }
  },
  "gate": {"status": "pass", "exceptions": []}
}
```

同步开始时根据对象类型、远端运行状态和代码证据生成预期数据集全集，之后每项都必须有记录，不能靠“文件不存在”表达空或缺失。

门禁算法保持单一：

1. 必需数据集只接受 `complete`。
2. 归因日志存在写入器时是必需核心数据集，只接受 `complete`；无写入器且有代码摘要证据时允许 `missing_at_source`，并在对象报告中保留例外。
3. 失败运行的错误日志是必需数据集；状态语义允许为空的结构化数据仍记录为 `complete`、`rows: 0` 和空结果证据。
4. 普通日志允许 `capped_free`；非核心数据允许有证据的 `missing_at_source` 或 `unsupported_api_version`。
5. 任一 `failed`、任一缺少状态的数据集、任一必需数据集的不可接受状态都使 `gate.status = fail`。
6. `gate: pass` 不等于“所有日志全量完整”；报告必须逐项显示例外。

所有数据文件使用不可变名称或分片。新文件校验后移入对象目录，最后用 `os.replace` 原子替换 manifest。读取端只信任 manifest 引用，因此中断批次不会覆盖上一次完整视图；未被清单引用的暂存或孤儿文件可在下次运行清理。

## 7. 结构化数据与分页

Research 导出结果、资金、持仓、订单、自定义记录、风险和分期风险。每个数据集分别校验：

- 远端声明的总数、页大小、空页或结束游标；
- 必要字段和类型；
- 业务唯一键、时间排序和重复行；
- 开始/结束时间、交易日关联和运行状态语义；
- 原始文件与 Parquet 行数、字段和 SHA256 对应关系。

页大小刚好填满而没有空页、总数或结束游标时不得标记 `complete`。同步结束重新获取远端对象清单；清单变化时只重取变化的数据集，第二次仍漂移则批次失败。

## 8. 日志管道

所有日志响应都先以原始字节写入 gzip，再解析。标准 JSON 失败时，容错解析只能恢复可明确分割的记录，并把错误位置、原始条数和恢复条数写入清单；原始证据始终保留。

归因日志单独处理：

- 回测完整逐行解析目标源码指定的单一 JSONL，校验 Token、连续序号、唯一运行边界，并把 `run_end` 最终资产与 Research 最终资金记录关联；
- 模拟交易根据代码历史的每个 `sourceBacktestId` 取回实际源码，保存历史顺序、操作时间、源 ID、代码 SHA256 和代码文件路径映射；
- 模拟交易归因所有权只来自启动生命周期实际初始化的代码版本，只读取和保存该版本的单一归因路径；后续代码版本中仅出现的回测或实验路径不得下载；
- 归因文件必须且只能有一个 `run_start`；活动运行不得有 `run_end`，已结束运行必须且只能有一个末尾 `run_end`；
- 已验证历史源码按 `sourceBacktestId` 缓存，后续增量只下载新增版本；同一次围栏复核复用本轮代码缓存。

普通控制台日志按免费接口持续分页；启用 `enable_profile()` 的回测等待页面性能页签明确就绪后保存完整面板文本：

1. 少于 1000 条且有明确结束证据，标记 `complete`。
2. 达到 1000 条必须请求下一页或读取可信总数。
3. 下一页免费可取时继续，直到明确结束。
4. 明确存在后续但免费不可取，或无法证明结束时，标记 `capped_free`。

积分下载不参与基础同步。只有调用者明确给出运行、类型和范围，接受聚宽当次返回的价格后才执行指定部分。

## 9. 存储与查询

- 原始页面和接口证据：`*.json.gz`、`*.jsonl.gz`。
- 结构化事实：Parquet + Zstd，按对象、数据集和不可变日期/游标分片。
- 查询：DuckDB `read_parquet` 视图，按 manifest 枚举文件；仓库不保存 `.duckdb` 副本。
- Vibe-Trading：只有明确对象、数据集、字段和时间范围后才生成 CSV，并记录过滤条件和来源摘要。
- Git：代码、manifest、报告摘要和说明使用普通 Git；`joinquant/**/raw/**`、`*.json.gz`、`*.jsonl.gz` 和 `*.parquet` 使用 Git LFS。

首次提交数据前必须验证远端 LFS 支持，并通过一次干净检出确认指针能恢复为真实文件。LFS 配额不足是明确阻断，不能把指针文件当作完整归档。

## 10. 增量与调度

历史对象只响应明确目标。再次同步先校验已有文件摘要，再比较远端摘要和数据集游标；无变化时不下载，有缺失或变化时只更新对应数据集。

模拟交易按不可变日期/游标分片追加：

- 每个模拟交易独立保存代码版本、快照、数据和日志游标；一个对象失败不阻塞其他对象提交。
- Windows Task Scheduler 每天北京时间 04:00 调用 `sync-active-simulations`。
- 安装时验证 Windows 时区为 `China Standard Time`；不满足时拒绝安装并提示修正，避免把本地 04:00 误当北京时间。
- 使用 Task Scheduler 原生 `RestartOnFailure`：间隔 30 分钟、重试 3 次。
- 重试耗尽后保留上一次 manifest，次日从同一已验证游标继续。
- 远端状态变为关闭时执行一次最终同步；归因写入器存在时验证 `run_end`，完成后从活动索引移除。

不实现守护进程、轮询服务或第二套重试器。

## 11. 认证、并发与故障

`auth` 使用仓库外专用 Playwright persistent context（持久上下文）打开可见浏览器，用户自行登录。实测 Chromium 正常关闭不会跨 CLI 进程保留聚宽会话 Cookie，因此脚本只把聚宽域名 Cookie 交给 Windows DPAPI 加密，并在仓库外保存仅当前 Windows 用户可解密的密文；不保存账号、密码、明文 Cookie、`storage_state` 或非聚宽 Cookie，不打印或提交凭证。生产任务只引用该目录路径；密文失效或重定向登录页立即返回 `auth_required`。

对象写入使用 Windows 标准库文件锁，同一对象同一时间只允许一个写者；不同模拟交易可以由同一次调度顺序处理。首版不增加进程池或分布式锁。

网络失败可以按任务规则重试；认证失败不盲目重试；解析或完整性失败保留原始暂存证据并阻止 manifest 提交。错误报告给出对象、数据集、阶段、状态和恢复动作。

## 12. 端到端回归与性能

`self-test` 是正式 CLI 子命令，不是若干单元测试的拼接。它在进程内生成最小证据，调用与生产同步相同的归档、门禁、查询和导出函数：

- 一个完整对象执行两次，验证幂等和摘要；
- 普通日志生成 999、1000、1001 条边界证据；
- 归因日志生成最小完整序列，以及缺页、断序、缺 `run_end`、无写入器场景；
- 覆盖完成、失败、取消、畸形 JSON、`capped_free`、`missing_at_source` 和 `unsupported_api_version`；
- DuckDB 使用 `:memory:`，归档仅写 `TemporaryDirectory`，结束后清理；
- 不启动浏览器、不访问网络、不扫描或载入历史归档。

Codex 从 `.agents/skills`、Claude 从符号链接入口分别调用同一 `self-test`，并先核对 `SKILL.md` 和脚本 SHA256。计划任务回归创建临时任务，通过 `schtasks /Run` 调用 `self-test`，检查 `LastTaskResult` 后删除临时任务。

`self-test` 使用固定小数据量，避免为性能测试再引入框架。它输出总耗时和 Python `tracemalloc` 峰值供 full（完整）验证记录，但不设置依赖硬件的脆弱阈值。真实在线链路只由一次性 PoC 验证。

## 13. 实施顺序与回滚

1. 真实 PoC；失败时人工导入兜底，两者均失败则停止。
2. manifest、稳定身份、原始证据和原子提交。
3. Research 结构化数据、分页与逐数据集门禁。
4. 归因日志、错误日志、普通日志 1000 条边界和积分预览。
5. Parquet、DuckDB、CSV 与 Git LFS 恢复验证。
6. 模拟交易增量和 Windows Task Scheduler。
7. 仓库 Skill、Claude 符号链接和全内存 `self-test`。

回滚只停用计划任务并回退 Skill 代码。已由 manifest 引用且摘要通过的归档不删除；未提交暂存和孤儿文件可在确认未被任何 manifest 引用后清理。
