## Why

活动模拟交易的 `results`（结果）会把运行中的临时末行记录为清单高水位。该行随后从 Research（研究环境）响应消失时，同步器仍以它作为重叠游标，导致本可由既有来源链和稳定前序行证明连续的增量被严格门禁拒绝。

## What Changes

- 从已验证的 Research lineage（研究来源链）中选择 `results` 的稳定前序重叠时间，而不是直接使用可能临时的清单最高时间。
- 找不到可验证前序重叠时对 `results` 执行安全全量刷新；不放宽现有精确重叠、来源覆盖和原子提交门禁。
- 保持累计 `results` 事实和清单数据流游标单调，不删除已验证的临时末行，也不产生业务键重复。
- 增加真实形状回归和发布入口 `self-test`（自测试），覆盖临时游标消失后的连续增量。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `joinquant-archive-sync`: 明确活动模拟交易增量同步在最高游标行消失时，必须使用已验证前序重叠或安全全量刷新证明连续。

## Impact

- 受影响代码：`joinquant_sync.sync_pipeline`（同步流水线）的 Research 增量游标选择。
- 受影响验证：模拟交易生产同步核心、严格清单/来源链校验及仓库 Skill（技能）的 `self-test`（自测试）。
- 不新增依赖、配置或公开接口。
