## Why

当前本地研究把通用运行、vectorbt 执行、结果固化和海龟策略语义混在策略目录中，导致策略改动经常伴随框架开发，延迟执行还重复维护账户账本；同时 `.local` 中的完成结果不能在策略目录内独立复盘。现在需要用一个统一变更建立可复用的三层架构，并让完成研究形成不可变、自包含的策略档案，同时保持结果口径和性能不退化。

## What Changes

- 建立三层单向依赖：vectorbt 执行底层负责唯一账本，Skill 通用能力层负责运行、性能、结果包和归档，每个策略只暴露一个公开 Strategy Module。
- 将即时和延迟执行统一到 `Portfolio.from_order_func()`，删除延迟路径中手工维护的现金、持仓、费用和净值账本。
- 把单场景编排、行情身份校验、冷热确定性检查、标准事实表、Parquet 固化、清单和原子发布从具体策略目录迁入共享能力层。
- 让共享结果层继续兼容本地 vectorbt 结果和聚宽归档，策略专属归因通过扩展注入，避免共享层反向依赖海龟语义。
- 将策略扩展收窄为扁平 `string/bool/int64/float64` Arrow 表，复用 PyArrow 的校验/判等与 Parquet SHA256，不实现完整 Arrow 类型解释器或自定义递归逻辑哈希。
- 在迁移 vectorbt 账本前收敛已实现共享层：结果 writer 单次回读、策略源码单一静态身份、标准 importlib 加载和标准文件复制；删除只服务敌对并发或重复权威来源的分支。
- 增加完成运行的独立晋升命令，把完整代码、配置、标准结果、归因、性能证据和报告原样发布到 `joinquant/strategies/<strategy_id>/research/archives/<analysis_id>/`；共享行情只保存快照身份和摘要，不重复复制。
- 建立真实规模、扩展资产和延迟场景的结果一致性、时间、内存与同逻辑 Parquet 数据载荷体积门禁；固定自包含证据开销单独报告。
- **BREAKING**：项目运行配置不再声明策略专属 `project_entry` 和任意 `command`，改为声明仓库内 Strategy Module；所有项目通过固定共享 CLI 执行。
- **BREAKING**：删除海龟策略旧的公开 CLI、引擎、性能和结果适配入口，不保留双生产路径；外部只允许加载 `turtle_etf.strategy:MODULE`。
- **BREAKING**：本地结果清单和代码身份升级为共享运行时、策略内核与归档可验证的新版本，不兼容写入旧格式。

## Capabilities

### New Capabilities

- `local-quant-research-runtime`: 定义 vectorbt 唯一账本、共享单场景运行、Strategy Module 接缝、停止状态、确定性和非退化性能要求。
- `local-research-result-package`: 定义后端中立的标准事实表、策略扩展、清单、摘要、原子固化和统一分析读取要求。
- `local-research-archive-promotion`: 定义完成运行向策略目录不可变自包含档案的显式晋升、校验、幂等和冲突处理要求。

### Modified Capabilities

无。聚宽正式回测和模拟交易归档继续遵守既有 `joinquant-archive-sync` 规格，本地研究档案不得进入其正式运行目录或冒充云端结果。

## Impact

- 共享能力：`.agents/skills/run-local-quant-research/`、`scripts/research/local_quant_research/`、`scripts/research/analysis_data/`。
- 策略能力：`joinquant/strategies/strategy-003/research/` 下的配置、代码身份和 `turtle_etf` 实现。
- 契约与数据：项目运行配置、本地结果清单、标准事实表扩展、策略档案清单和性能基线。
- 测试与验证：现有本地研究测试、第二个最小策略 Adapter、共享 CLI 到结果包再到档案晋升的完整端到端回归。
- 依赖保持现有 vectorbt 1.1.0、Numba、NumPy、Pandas、PyArrow 和 DuckDB，不新增回测框架、DSL 或 Rust 执行内核。
