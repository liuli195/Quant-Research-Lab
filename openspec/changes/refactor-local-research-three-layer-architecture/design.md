## Context

当前本地研究已经具有共享行情快照、通用 runner、后端中立分析视图和一套可运行的海龟 ETF vectorbt 实现，但职责分布不一致：

- `.agents/skills/run-local-quant-research/SKILL.md` 已声明 Skill 只做薄编排，实际项目仍需提供自己的 CLI、单场景、性能和结果固化代码。
- `turtle_etf` 目录包含指标、输入、vectorbt 回调、即时执行、延迟执行、性能、结果适配和 CLI 九类公开文件；共享能力与策略语义相互导入。
- 即时路径正确使用 `Portfolio.from_order_func()`，延迟路径却先用 Python 手工维护现金、持仓、费用和权益，再通过 `Portfolio.from_orders()` 重放，形成双账本。
- 标准结果完成后只发布到 `.local/quant-research`，策略目录没有可以脱离运行缓存直接复盘的不可变研究档案。

本变更由一个 OpenSpec change 完成三层重构和档案晋升。虽然两部分可独立描述，但它们共享同一 Strategy Module、结果包和清单契约；分开实施会引入中间兼容格式和双生产路径，因此用户明确选择单 change 原子迁移。

既有 `build-turtle-etf-local-research-workflow` 设计中“vectorbt 只属于 strategy-003”和“延迟使用 from_orders”的实现约束由本变更显式取代；聚宽正式回测和模拟交易仍只在云端运行。

## Goals / Non-Goals

**Goals:**

- 建立 vectorbt 执行底层、Skill 通用能力层、Strategy Module 三层单向依赖。
- 让 vectorbt 成为即时和延迟路径唯一账户账本，不重复维护成交、现金、持仓、费用或净值。
- 让所有策略复用同一单场景、性能、结果包和归档实现，并以第二个最小策略证明接缝真实存在。
- 让每个策略只有一个公开入口，同时允许私有实现文件保持 Numba 编译内核和归因的局部性。
- 在不重新回测和不重复复制共享行情的前提下，生成策略目录内可独立查询、比较和生成报告的不可变研究档案。
- 保持现有策略语义、指标口径、结果摘要和性能不发生实质退化。

**Non-Goals:**

- 不修改海龟突破、退出、加仓、风险单位、共同止损、全量再分配和延迟冻结规则。
- 不把本地研究结果声明为聚宽正式回测或模拟交易结果。
- 不修改聚宽定时归档、归档格式、同步逻辑、`backtests/` 或 `simulations/` 数据模型，也不在本 change（变更）实现定时任务运行目录隔离；该运维问题另行处理。
- 不复制 vectorbt、共享 Skill 实现、Python 环境或共享行情文件到每个档案。
- 不引入新的回测框架、策略 DSL、Rust 执行内核或多场景批处理器。
- 不支持完整 Arrow 类型体系，不为 dictionary/nested/union/run-end encoded（字典/嵌套/联合/游程编码）编写规范化解释器。
- 不把本地同一用户仓库当作敌对文件系统，不实现扫描后并发替换防御状态机。
- 不保留旧 CLI、旧结果 writer 或旧延迟账本作为兼容生产路径。

## Decisions

### 1. 三层依赖只允许自上而下

目标结构为：

```text
Strategy Module
      ↓
Skill 通用能力层
      ↓
vectorbt 执行底层
```

vectorbt 底层是唯一允许导入 `Portfolio`、vectorbt 上下文、订单枚举和记录结构的 Module。Skill 层同时依赖稳定的策略 Interface 和底层执行 Interface，但不得导入具体策略内部实现。策略只依赖仓库自有 contracts，不得操作 vectorbt 内部数组。

备选方案是保留每个策略自己的 vectorbt 接线，仅抽公共文件 writer；这仍会让新策略复制执行、性能和错误处理，无法获得足够 Leverage，因此拒绝。

### 2. vectorbt 底层提供小而深的执行 Interface

共享底层提供等价于以下形状的 Interface：

```python
run_vectorbt(ledger_input: LedgerInput, program: OrderProgram) -> ExecutionRun
```

`LedgerInput` 保存日期、证券、估值价格、初始现金和现金分组；`OrderProgram` 保存项目自有的 Numba 决策函数、只读策略输入、可变策略状态和审计轨迹；`ExecutionRun` 通过只读 `ExecutionLedger` 暴露订单、持仓、现金、净值、交易和收益视图。原始 `Portfolio` 不向策略或结果层公开。

即时和延迟程序都由同一个 `from_order_func()` Adapter 执行。延迟策略保留信号冻结、执行日可交易性复核、现金/持仓机械截断、确定性优先级和到期证据，但删除手工账户循环。

备选方案 `from_signals()` 无法完整表达共享现金、跨资产再分配、共同止损和实际成交后状态推进；`from_orders()` 只能重放已确定订单，不能成为延迟执行的唯一账本，因此拒绝。

### 3. Skill 是公开入口，共享代码保留在仓库 scripts

`.agents/skills/run-local-quant-research/SKILL.md` 继续只描述一次通用调用和停止状态。生产 Implementation 位于 `scripts/research/local_quant_research/`，统一提供 contracts、策略加载、共享 CLI、单场景、vectorbt runtime、性能、结果包和晋升。

项目运行配置不再接受策略专属 `project_entry` 和任意 `command`，改为声明仓库内 `strategy_root`、`strategy_module` 和 `strategy_symbol`。runner 始终使用项目 `.venv` 启动固定共享 CLI，并继续冻结输入、清理环境和隔离子进程。

把生产代码放进 Skill 目录会把发现说明、执行依赖和测试绑定在 Agent 资产中，也与现有 Skill 契约冲突，因此拒绝。

### 4. 每个策略只有一个公开 Strategy Module

海龟策略公开 `turtle_etf.strategy:MODULE`，实现可按以下私有文件组织：

```text
turtle_etf/
├── __init__.py
├── strategy.py
├── _kernel.py
├── _attribution.py
└── _delayed.py
```

公开 Module 负责配置校验、输入准备、订单程序构造和归因扩展。私有 `_kernel.py` 保存模块级固定 Numba 函数，禁止根据配置动态创建闭包或 lambda，避免增加编译特化。`_delayed.py` 先保留海龟专属的冻结语义；出现第二个真实复用实例前不把它强行泛化。

父进程先在受限策略根内解析 module，再以其顶层包目录（单文件 module 则为文件所在目录）为源码边界，静态发现并排序全部普通 `.py` 文件。该集合同时驱动运行身份和档案 `code/`，不得越界扫描 `research/archives/` 或相邻目录，`StrategyDescriptor` 不再声明第二份 `source_files`。每次 `_execute` 是只加载一个策略的全新子进程，因此子进程只需把冻结策略根放到 `sys.path` 首位并调用标准 `importlib.import_module()`；不建立 UUID 命名空间、全局导入锁或手工模块缓存生命周期。

Strategy Module 是仓库内受版本管理和代码审查的可信代码。当前运行边界使用受限源码路径、冻结输入、清理环境、全新子进程和超时；不安装 Python audit hook（审计钩子），也不把加载器扩展成不可靠的应用层沙箱。第三方不可信策略执行不属于本 capability（能力）。

把全部策略逻辑合并为一个物理文件会形成约 150 KB 大文件、扩大导入和代码身份变化面，因此拒绝。

### 5. 公共结果包后端中立，策略证据作为扩展注入

共享结果包定义结果、资金、持仓和订单四张核心表，归因等策略证据通过版本化 `ResultExtension` 注入。公共 writer 负责 Schema、跨表校验、Parquet、逻辑摘要、清单、回读验证、暂存清理和原子发布；不得导入海龟动作码。

`ResultExtension.table` 只接受扁平 `string/bool/int64/float64` 列；浮点缺失使用 Arrow null，NaN 和任何其他类型在冷/热比较前固定返回 `result_contract_failed`。共享层使用 PyArrow `Table.validate(full=True)` 与 `Table.equals(check_metadata=True)`，完成文件使用 SHA256；不实现递归 Arrow 类型解码或任意类型逻辑摘要。

writer 只物化一次、回读一次，并直接复用该次回读事实完成内部校验、报告和最终清单。公开 validator 只从文件读取，供复用、晋升和外部查询；writer 不通过 `preloaded_*` 参数调用它，也不写 provisional/final 两套完整包。

`ExecutionLedger` 的 orders、assets、cash 和 value 按需生成一次并缓存；公共事实只物化一次。策略轨迹只分配其声明字段，不用全量 vectorbt logs 替代轻量拒单和归因状态。

共享分析继续读取本地结果和聚宽归档。vectorbt returns/stats 可以作为独立交叉校验，但不得静默替换现有 Alpha、Information Ratio、CVaR 等公式版本。

### 6. 自包含档案通过完成后独立晋升产生

共享 CLI 增加显式 `promote` 动作。它只接受已经通过 `complete` 门禁的不可变 `.local` 运行，目标为：

```text
joinquant/strategies/<strategy_id>/research/archives/<analysis_id>/
├── manifest.json
├── code/
├── config/
├── data/
├── evidence/
└── report/
```

档案包含完整策略源码、运行配置、代码身份、四张核心表、策略扩展、性能与环境证据、行情快照身份和报告。共享运行时代码、第三方依赖和行情文件只通过版本、Git 提交、文件摘要和 snapshot_id 锁定，不逐档案复制。

晋升先扫描源树并拒绝链接、目录连接和非普通文件，使用标准 `shutil.copy2` 复制到目标同级暂存目录，逐文件复核 SHA256 后使用 `os.replace` 原子发布。目标不存在时发布；目标内容完全相同时返回复用；同一 analysis_id 内容不同时失败且不覆盖。晋升不得调用策略、vectorbt、事实转换或 Parquet writer，也不防御同一用户在扫描后敌对替换源树。

### 7. 正确性零容忍，性能门禁区分目标与测量噪声

重构前在固定环境建立 3,432 日 × 11 ETF 主场景基线，并覆盖 17 ETF 扩展场景和 `additional_delay_days=1` 延迟场景。重构后 Schema、行数、成交、净值和逻辑摘要必须完全一致。

性能目标为零退化。引擎冷启动使用三个新进程中位数，引擎预热使用同一进程五次中位数；完整 CLI 发布另使用三个独立冷进程，每个样本使用隔离输出根并禁止复用，计时到原子发布和发布后校验完成，不把同进程预热伪装成重复 CLI 启动。Task 10 必须在旧入口删除前用同一协议补齐旧完整 CLI 总时间与 Parquet 载荷 baseline；协议、环境、起止点或 baseline 摘要不一致时拒绝比较。时间、峰值内存和同逻辑核心/扩展 Parquet payload 体积允许最多 5% 测量噪声；固定代码、配置、证据和报告开销单独报告，不与旧 v1 整包直接比较。任何超过噪声带的变化阻断完成。现有冷/热各 180 秒绝对门禁继续保留。包内 `performance.json` 只记录不自指的预最终化阶段；日常绝对门禁使用 writer 返回时的完整耗时；完整 CLI 的 5% 门禁只比较重构前后相同起止点的总耗时，`finalize_publish` 只作归因。外部验证报告必须用协议版本、场景、样本、PID、run_id、package_sha256、非复用与发布后校验状态、baseline 和环境摘要绑定实际包，并证明写报告没有修改结果包。不得为把最终总耗时写回同一个不可重写包而恢复双包、二次元数据写入或旁路清单。策略准备、vectorbt、公共事实、策略归因、Parquet、最终发布和晋升分别计时，避免在相同观测边界之间移动工作隐藏退化。

`max_logs=0` 和回调缓冲预分配属于候选优化，只有结果摘要完全一致且真实基准不退化时才保留。保留 vectorbt 默认持仓记录并直接复用其 trades/positions accessor；没有实测瓶颈时不关闭记录，也不在共享层重建交易或持仓。

## Risks / Trade-offs

- [Numba 高阶回调可能产生额外特化或冷启动编译] → 使用模块级固定函数和稳定数组类型；禁止动态回调；分别测量导入、准备、编译和预热阶段。
- [抽取公共账本时可能无意复制大型矩阵] → `ExecutionLedger` 实行单次惰性缓存和只读所有权移交；测试验证共享内存或唯一明确复制。
- [延迟路径迁移改变最低佣金、部分成交或冻结语义] → 先建立现有行为特征测试，再逐笔比较计划、成交、费用、现金、单位、共同止损和原因码。
- [一次 change 范围较大] → 按可独立验收的迁移任务逐步替换，每一步保持共享 CLI 可运行；不在生产配置中同时开放新旧入口。
- [旧 `.local` 结果不能写入新档案] → 旧结果保持只读；只有通过新清单校验的运行可晋升，不进行隐式升级或伪造缺失证据。
- [策略源码自包含但运行环境未 vendoring] → 档案明确区分“可独立分析”与“按锁定环境可重放”，清单保存运行时、依赖、Git 和行情快照身份。

## Migration Plan

1. 冻结当前主场景、扩展场景和延迟场景的结果、时间、内存及体积基线，增加分阶段计时。
2. 定义 Strategy Module 与执行 contracts，并用一个最小测试策略证明第二个 Adapter。
3. 抽取共享结果包，让现有海龟执行结果通过新 writer 和统一分析视图测试。
4. 完成 archive-ready package 和 promote 端到端测试。
5. 抽取共享单场景、性能和 CLI，仅让测试配置走新入口，生产配置留到单次切换任务。
6. 收窄扩展表并把 writer 收敛为单次回读事实链，删除自定义递归 Arrow 解释器和内部 validator 双路径。
7. 统一静态源码身份并改用标准 importlib，删除 descriptor 源码清单、UUID 命名空间、v2 audit hook 沙箱和重复测试夹具内容。
8. 把晋升收敛为扫描、标准复制、摘要复核和原子发布，删除敌对并发树状态机。
9. 建立通用 vectorbt 唯一账本 runtime，只用通用 primary/follow-up fixture 证明共享接线。
10. 在海龟 Strategy Module 内同时迁移即时与延迟 OrderProgram，逐笔一致后删除手工账本。
11. 单次切换生产配置，更新测试、代码身份、Skill 文档和旧 OpenSpec 约束，删除所有旧生产文件、共享 `adapter_guard.py` 和 runner v1 command 路径。
12. 运行共享 CLI → vectorbt → 标准结果包 → 自包含档案的完整端到端回归和真实规模性能验证。

回滚以任务提交为单位进行：在旧生产入口尚未删除前，可回退最近迁移提交；删除旧入口后只允许整体回退到最后一个已验证提交，不提供运行时双路径开关。

## Open Questions

无待用户决策的开放问题。`OrderProgram`、`ExecutionLedger` 和 Numba 上下文的精确字段将在 Superpowers Design Doc 中根据现有回调需要收敛，但不得改变本设计确定的依赖方向、唯一账本、唯一公开策略 Module、结果零差异和归档不重算约束。
