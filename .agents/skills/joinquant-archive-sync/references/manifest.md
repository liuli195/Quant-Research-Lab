# Manifest（归档清单）契约

`manifest.json` 是对象读取和提交的唯一权威入口，固定包含：

机器约束见同目录 `manifest.schema.json`。

```json
{
  "schema_version": 1,
  "object": {"kind": "backtest", "local_id": "4", "status": "done"},
  "source": {"url": "...", "aliases": [], "observed_at": "..."},
  "fence": {"before_sha256": "...", "after_sha256": "..."},
  "code": {"path": "code.py", "sha256": "..."},
  "datasets": {},
  "gate": {"status": "pass", "exceptions": []}
}
```

## 稳定身份

- `strategy_id` 和 `simulation_id` 首次写入索引后不变。
- 构建和回测的 `local_id` 是所属页面内的正整数序号。
- 远端 ID、详情 URL 和名称只追加到 `aliases`；刷新后远端 ID 改变不得新建目录。
- 构建和回测的页面序号再次出现时，代码与参数指纹必须一致；指纹冲突立即停止，不能把新别名并入旧对象。
- 历史回测只接受明确页面序号或聚宽详情 URL；拒绝空目标、`latest`、`all` 和裸远端 ID。

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
- 失败或取消运行的 `error_log` 是必需数据集。
- `normal_log` 可接受有分页证据的 `capped_free`，但必须出现在 `gate.exceptions`。
- 非核心数据的 `missing_at_source` 和 `unsupported_api_version` 只有在证据存在时才可作为例外。
- 任一 `failed`、缺少状态、未知状态或必需数据集例外都会使门禁失败。

`gate.status=pass` 不代表每项都是全量完整；调用者必须读取 `exceptions` 和各数据集状态。
