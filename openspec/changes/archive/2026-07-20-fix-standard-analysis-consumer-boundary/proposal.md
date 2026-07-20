## Why

标准量化分析的现有读取器仍会进入遗留来源类型分支；分析计划还依赖仓库内的基线配置，并允许计划场景缺少结果包时继续交付。上述行为与已生效的标准结果包流程不一致，可能使结论依赖隐藏来源或不完整比较。

## What Changes

- 让量化分析只消费现有标准结果包契约，不再经由遗留来源类型读取分支。
- 让分析计划包自行解析其声明的基线配置；`--repository` 只继续作为本地交付目录。
- 在生成交付前要求结果包场景集合完整覆盖分析计划。
- 补齐对应的定向和真实 Skill（技能）入口端到端验证。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `standard-strategy-analysis-workflow`：补充生产者无关读取和计划场景完整覆盖的验收场景。
- `standard-strategy-analysis-data`：补充可移动分析计划包的验收场景。

## Impact

- `.agents/skills/analyze-quant-robustness`（标准量化分析 Skill）读取、计划解析与命令说明。
- 现有标准结果包校验器及量化分析测试。
- 不新增依赖，不修改 JoinQuant（聚宽）云端流程；规格增量只澄清现有要求的可验收边界。
