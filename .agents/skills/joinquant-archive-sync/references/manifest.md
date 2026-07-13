# Manifest（归档清单）契约

`manifest.json` 是对象读取和提交的唯一权威入口，固定包含：

机器约束见同目录 `manifest.schema.json`。

```json
{
  "schema_version": 1,
  "object": {"kind": "backtest", "local_id": "4", "status": "done"},
  "source": {"url": "...", "aliases": [], "observed_at": "..."},
  "fence": {"before_sha256": "...", "after_sha256": "..."},
  "collection_fence": {"collection_before_sha256": "...", "collection_after_sha256": "..."},
  "research_response": {"path": "raw/research-response.json.gz", "sha256": "...", "format": "json.gz"},
  "research_lineage": [{"path": "raw/research-response.json.gz", "sha256": "...", "format": "json.gz"}],
  "code": {"path": "code.py", "sha256": "..."},
  "datasets": {},
  "gate": {"status": "pass", "exceptions": []}
}
```

## 稳定身份

- `strategy_id` 和 `simulation_id` 首次写入索引后不变。
- 回测的 `local_id` 是所属页面内的正整数序号。
- 远端 ID、详情 URL 和名称只追加到 `aliases`；刷新后远端 ID 改变不得新建目录。
- 回测的页面序号再次出现时，代码与参数指纹必须一致；指纹冲突立即停止，不能把新别名并入旧对象。
- 历史回测只接受明确页面序号或聚宽详情 URL；拒绝空目标、`latest`、`all` 和裸远端 ID。
- `fence` 对应已合并的代码、数据、日志和归因状态；重复同步只有该摘要一致才可返回 `unchanged`。
- 回测和模拟交易必须记录 `collection_fence`；浏览器证据和 Research 数据在采集前后任一变化都拒绝提交。
- 回测和模拟交易必须保存 Research 的单次原始返回包，并由每个结构化数据集的分页证据引用其 SHA256；模拟交易增量保留不可变 `research_lineage` 来源链，严格校验要求累计行都能追溯到至少一个原始返回，并复算 data/code/log stream（数据、代码、日志流）摘要。
- 模拟交易必须解析代码历史中每个 `sourceBacktestId` 的真实完整代码，不能把历史列表里的占位 `code` 字段当源码。清单逐条保存历史顺序/时间、当次观察到的 `sourceBacktestId` 到代码 SHA256/文件的映射；这些远端 ID 可能轮换，只作为别名，稳定围栏使用顺序、时间和代码摘要。归因所有权只来自启动生命周期实际初始化的代码版本，清单只能引用该版本的单一完整 JSONL；后续代码版本中仅出现、但没有生命周期所有权的历史回测或实验路径不得下载或作为排除证据保存。回测归因还必须保存并复核 `run_end` 最终资产与 Research 最终资金记录的关联证据。

索引最小结构：

```json
{
  "schema_version": 1,
  "objects": [
    {
      "kind": "backtest",
      "local_id": "4",
      "identity": {"page_ordinal": "4", "strategy_id": "strategy-001"},
      "aliases": [{"remote_id": "...", "url": "..."}]
    }
  ]
}
```

## 数据集状态

每个预期数据集必须显式使用一种状态：

- `complete`：已取得明确终止证据并通过字段、行数、排序和摘要校验。
- `capped_free`：普通日志免费范围已取完，但源端无法免费证明全量；必须保留分页和上限证据。
- `missing_at_source`：源端没有该数据；必须有代码或页面证据。
- `unsupported_api_version`：当前平台接口不提供该非核心数据；必须记录接口版本和响应证据。
- `failed`：下载、解析或验证失败。

预期数据集创建时默认是 `failed`，取得并校验证据后才能改为 `complete`。`complete` 必须引用至少一个已校验文件，或同时记录 `rows: 0` 与 `verified_empty: true`；空数据集映射和只有状态、没有证据的条目都不能通过门禁。

合法空表使用 `complete`、`rows: 0`、`verified_empty: true`，不能靠缺文件表示。

## 官方回测摘要

`data/official-summary.csv` 是回测详情页“导出 CSV”入口产生的官方页面源数据，不是 Research（研究环境）返回，也不是本地人工分析报告。保存它有三个目的：保留聚宽页面展示口径、为 Research 数据提供独立来源的交叉校验、在页面或接口变化时保留可复核证据。它与 Research 数据部分重复，但显示精度和聚合口径不同，不能替代结构化明细。

`reports/` 只允许保存人工或 Agent（代理）生成的分析报告。回测清单必须把官方摘要记录为：

```json
{
  "official_summary": {
    "required": true,
    "status": "complete",
    "rows": 1289,
    "files": [
      {
        "path": "data/official-summary.csv",
        "sha256": "...",
        "bytes": 66969,
        "format": "csv"
      }
    ],
    "evidence": {
      "evidence_version": 1,
      "source": {
        "kind": "joinquant_backtest_detail_export",
        "url": "<回测详情链接>",
        "action": "export_csv"
      },
      "encoding": "gb18030",
      "header": ["时间", "基准收益", "策略收益", "当日盈利", "当日亏损", "当日买入", "当日卖出", "超额收益(%)"],
      "rows": 1289,
      "related_datasets": ["results", "balances", "orders"]
    }
  }
}
```

字段关系分为三类：

| 关系 | 官方摘要字段 | Research 数据 | 使用边界 |
|---|---|---|---|
| 可对齐或近似推导 | `时间`、`基准收益`、`策略收益`、`超额收益(%)` | `results.time`、`benchmark_returns`、`returns` | 可核对日期和累计收益；官方文件存在百分比展示和舍入，不能反向恢复 Research 精度 |
| 仅可交叉校验 | `当日盈利`、`当日亏损` | `balances.total_value` 的相邻交易日差额 | 可复核方向和页面口径；首日还需要初始资金，不能用摘要替代资金明细 |
| 仅可交叉校验 | `当日买入`、`当日卖出` | `orders` 按交易日和方向汇总 | 可复核页面聚合值；手续费、成交状态、舍入和聚合口径使摘要不能替代订单明细 |
| 不可由摘要推导 | 无对应字段 | `cash`、`aval_cash`、持仓、单笔订单、`records`、`risk`、`period_risks`、日志 | 必须读取对应 Research 或日志数据集 |

分析某日盈亏时，以 `balances.total_value` 的交易日差额为明细来源，并结合 `orders` 解释交易构成；`official_summary` 只复核聚宽页面展示口径。查询单笔交易、现金、持仓、风险或日志时不得读取官方摘要代替。

既有归档采用一次性迁移：在保持文件字节和 SHA256（完整性摘要）不变的前提下，将 `reports/official-summary.csv` 移到 `data/official-summary.csv`，同步更新清单路径和证据。读取方始终通过 `manifest.json`（清单）定位文件；迁移完成后不保留旧文件、旧引用或双路径兼容。

## 门禁

- 必需数据集只接受 `complete`。
- 有归因写入器时，`attribution_log` 是必需数据集；只接受 `complete`。
- 无归因写入器时，`attribution_log` 使用带代码证据的 `missing_at_source`。
- 失败运行的 `error_log` 是必需数据集。取消运行如果页面没有错误记录，使用带取消状态证据的 `missing_at_source`，不得伪造错误。
- `normal_log` 可接受有分页证据的 `capped_free`，但必须出现在 `gate.exceptions`。
- 非核心数据的 `missing_at_source` 和 `unsupported_api_version` 只有在证据存在时才可作为例外。
- 任一 `failed`、缺少状态、未知状态或必需数据集例外都会使门禁失败。

`gate.status=pass` 不代表每项都是全量完整；调用者必须读取 `exceptions` 和各数据集状态。

## 模拟交易增量

模拟交易分别保存 `code`、`snapshots`、`data` 和 `logs` 的最后已验证游标与 SHA256。游标相同但摘要变化仍须补取；一个对象失败时不得推进其任何游标，也不得阻止其他对象独立提交。远端变为 `closed` 后执行一次最终同步；存在归因写入器时必须取得 `run_end`，成功后将 `tracking` 改为 `stopped`，历史目录保留。
