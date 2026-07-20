## Why

本地研究工作流的实现与回归已经覆盖共享行情导入、快照创建、公开运行入口和全量验证，但现行规格仍引用已不在仓库中的 Skill 初始化工具，并把组合式 E2E（端到端）描述为单一公开命令，同时保留过期的 30 秒性能门槛。

## What Changes

- 将本地研究 E2E（端到端）验收明确为：测试可组合共享行情导入与快照创建，再通过 Skill（技能）公开 `run` 入口验证研究执行链路。
- 删除对 `init_skill.py` 和 `quick_validate.py` 的验收依赖，改为现行的布局、契约、确定性与 E2E（端到端）回归。
- 将全量验证的性能描述对齐当前 60 秒预算和超预算告警语义。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `local-quant-research-workflow`: 对齐 Skill（技能）验收边界和全量验证性能契约。

## Impact

- 修改 `openspec/specs/local-quant-research-workflow/spec.md`。
- 不修改本地研究 CLI（命令行接口）、运行器、测试、依赖或验证配置。
