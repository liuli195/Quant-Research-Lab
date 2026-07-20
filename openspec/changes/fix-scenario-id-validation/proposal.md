## Why

本地研究只拒绝空场景标识，而标准量化分析拒绝含下划线等不兼容标识。无效配置会在耗时运行完成后才失败，产生无法分析的结果包。

## What Changes

- 在共同结果契约中定义场景标识格式，并在本地研究的配置加载阶段复用。
- 保持标准量化分析现有格式约束，增加回归以防两端规则漂移。

## Capabilities

### New Capabilities

- 无。

### Modified Capabilities

- 无；这是既有配置校验职责的缺陷修复，不改变规格验收要求。

## Impact

- `scripts/research/result_contract.py`、本地研究配置加载和既有回归测试。
- 不新增公开接口、依赖或结果包格式。
