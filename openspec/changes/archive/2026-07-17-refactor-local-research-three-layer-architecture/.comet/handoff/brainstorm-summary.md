# Brainstorm Summary

- Change: refactor-local-research-three-layer-architecture
- Date: 2026-07-17

## 确认的技术方案

采用 vectorbt 执行底层、Skill 通用能力层、唯一公开 Strategy Module 的三层单向架构：

```text
Strategy Module
      ↓
Skill 通用能力层
      ↓
vectorbt 执行底层
```

- `.agents/skills/run-local-quant-research/SKILL.md` 保持薄编排，生产实现集中在 `scripts/research/local_quant_research/`。
- 海龟策略只公开 `turtle_etf.strategy:MODULE`，Numba 内核、归因和延迟语义保留为私有文件。
- vectorbt 是即时和延迟路径唯一账户账本；不使用 `from_signals()` 替代海龟自定义回调，不保留延迟 Python 手工账本。
- 公共结果包保持后端中立，结果、资金、持仓、订单四张核心事实与策略扩展分离；共享分析同时读取本地结果和聚宽归档。
- `.local` 完成结果直接形成 archive-ready package，`promote` 逐字节复制、复核 SHA256 并原子发布到 `research/archives/<analysis_id>/`。
- 共享行情不复制，档案保存完整 snapshot identity、来源、范围、字段、价格口径和摘要。
- `report/` 只包含机械执行报告，不生成策略推荐、稳健性结论或实盘准入判断。

Strategy Module Interface 收敛为：

```python
class StrategyModule(Protocol):
    descriptor: StrategyDescriptor

    def prepare(self, snapshot, config) -> PreparedStrategy: ...
    def followup_program(
        self, prepared, primary_run
    ) -> OrderProgram | None: ...
    def build_extensions(
        self, prepared, execution_bundle
    ) -> tuple[ResultExtension, ...]: ...
```

- `prepare` 校验策略配置并返回 `LedgerInput`、primary `OrderProgram` 和策略私有 context。
- `followup_program` 是唯一可选第二阶段：普通策略返回 `None`；海龟延迟为正时根据 primary run 冻结计划并返回 delayed `OrderProgram`。共享 runner 最多执行两个阶段，不引入工作流 DSL 或无限循环。
- `build_extensions` 只生成策略证据，不写文件。
- `StrategyDescriptor` 声明 strategy id、contract version、源文件和扩展名，同时用于代码身份和档案源码快照。

vectorbt runtime 使用项目自有 Numba contracts：

- `SegmentView`：row、group、cash、value、positions 和 valuation prices 等稳定只读账本视图。
- `OrderBuffer`：按证券预分配 enabled、side、size、price、fixed fees、size granularity、allow partial 和 priority arrays。
- `FillEvent`：status、side、size、price、fees、cash after 和 position after。
- 共享 callback Adapter 把 vectorbt context 映射为项目结构，把 `OrderBuffer` 转换为 `nb.order_nb`，再把成交结果映射为 `FillEvent`。
- runtime 按 `(priority class, original column)` 原地构造稳定 call sequence，不在逐日回调中新建数组。
- callback binding 按固定 hook identity 在进程内缓存；同一策略的不同场景不得创建新 wrapper 身份。

执行结果为：

```python
ExecutionBundle(primary: ExecutionRun, final: ExecutionRun, stages: tuple[str, ...])
```

- 无延迟时 `primary is final`，不复制账本。
- 有延迟时 primary vectorbt run 生成并冻结计划，final vectorbt run 执行延迟账本。
- 核心事实只读取 final ledger；策略扩展可以读取两阶段轨迹。
- `ExecutionLedger` 私有持有 Portfolio，orders/assets/cash/value 惰性计算一次并返回只读缓存。

共享文件形状：

```text
scripts/research/local_quant_research/
├── contracts.py
├── strategy_loader.py
├── vectorbt_runtime.py
├── scenario.py
├── performance.py
├── result_package.py
├── archive.py
├── runner.py
├── evidence.py
└── cli.py

joinquant/strategies/strategy-003/research/turtle_etf/
├── __init__.py
├── strategy.py
├── _kernel.py
├── _attribution.py
└── _delayed.py
```

`analysis_data` 继续拥有后端中立 manifest 读取、验证和查询视图；本地 writer 和 promote 保留在 `local_quant_research`。

## 关键取舍与风险

- 采用项目自有 OrderProgram 加共享 vectorbt 包装器，拒绝策略直接提供 vectorbt-native callback，也拒绝预生成完整订单矩阵。
- 每个策略允许一个固定 Numba 包装特化，但不得按场景动态生成回调；先用第二个最小策略验证特化数量和冷热性能。
- 延迟场景需要两次 vectorbt 执行：第一次生成和冻结计划，第二次维护最终延迟账本；两次都不得使用手工账户循环。
- `ExecutionLedger` 实施惰性缓存和只读所有权移交，避免 orders/assets/cash/value 被多层重复生成或复制。
- 新旧生产入口不并存；每次删除旧入口前必须先通过结果摘要和真实性能门禁。
- `analysis_id` 是目标目录身份，不改写完成包内部 `run_id`，保证 archive-ready package 可逐字节复制。
- 档案可独立分析但不 vendoring 共享运行时、第三方依赖或行情；通过 Git、依赖、运行时和 snapshot 摘要支持按锁定环境重放。

## 测试策略

- Characterization：冻结即时、17 ETF 和延迟场景的逐笔成交、费用、现金、持仓、净值、策略状态和逻辑摘要。
- Contract：第二个最小策略通过同一 Strategy Module Interface、vectorbt runtime、结果包和停止状态执行。
- TDD：每个新 Interface、结果包、晋升和迁移任务先观察失败测试，再写最小实现。
- E2E：从固定共享 CLI 完整运行 Strategy Module → vectorbt → 结果包 → promote。
- 性能：主场景使用 3 个冷启动新进程和 5 次预热，扩展到 17 ETF 并覆盖延迟场景；结果证据零差异，时间、峰值内存和体积不超过 5% 测量噪声。
- 原子性：晋升时把引擎调用设为调用即失败，验证逐文件 SHA256、幂等复用、内容冲突和中途失败清理。
- 错误映射：输入和身份缺失为 `evidence_insufficient`；执行、摘要、性能、结果或清理异常为 `failed`；正常订单拒绝保留为执行事实。

## Spec Patch

- 将 `local-research-archive-promotion` 中的“研究报告”收敛为“机械执行报告”。
- 新增验收场景：机械执行报告只包含可从完成包复核的运行身份、参数、数据范围、成交/持仓统计、净值摘要、性能和完整性事实，不得生成策略推荐、稳健性结论或实盘准入判断。
