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
- 模拟交易归因必须解析代码历史中每个 `sourceBacktestId` 的真实完整代码，不能把历史列表里的占位 `code` 字段当源码。所有不同归因路径的完整 JSONL 原始文件都要保存并逐源校验；查询视图只保留模拟交易结果日期范围内的行，范围外历史回测行也必须作为排除证据保留。

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
