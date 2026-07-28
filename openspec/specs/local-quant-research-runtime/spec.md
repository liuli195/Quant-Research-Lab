# local-quant-research-runtime Specification

## Purpose
TBD - created by archiving change refactor-local-research-three-layer-architecture. Update Purpose after archive.
## Requirements
### Requirement: 本地研究必须使用三层单向架构
系统 MUST 将本地研究组织为 Strategy Module、Skill 通用能力层和 vectorbt 执行底层，并且依赖只能从 Strategy Module 经共享 contracts 进入 Skill 能力和执行底层。具体策略不得导入 vectorbt 上下文、订单枚举、记录结构或原始 Portfolio；共享能力不得导入具体策略的私有实现。

#### Scenario: 加载海龟策略
- **WHEN** 共享入口加载海龟研究项目
- **THEN** 它只通过 `turtle_etf.strategy:MODULE` 使用策略，并通过共享执行 Interface 调用 vectorbt，不直接导入海龟私有文件

#### Scenario: 第二个策略复用共享能力
- **WHEN** 仓库提供一个实现同一 Strategy Module Interface 的最小测试策略
- **THEN** 共享 CLI、日常超时检查、结果包和停止状态无需修改即可运行该策略

### Requirement: vectorbt 必须成为唯一账户账本
系统 MUST 使用 vectorbt `Portfolio.from_order_func()` 处理即时和延迟场景的订单接受、拒绝、部分成交、费用、共享现金、持仓和组合估值。策略只保存决策所需的单位、止损、冻结计划、原因和归因轨迹，不得维护第二套成交、现金、持仓、费用或净值账本。

#### Scenario: 即时执行
- **WHEN** 策略在同一交易日生成并执行目标订单
- **THEN** 实际成交、费用、现金、持仓和净值全部来自单一 vectorbt Portfolio

#### Scenario: 延迟执行
- **WHEN** 场景声明正数 `additional_delay_days`
- **THEN** 策略冻结原计划并在执行日复核可交易性和机械约束，但实际账户变化仍由第二个 `from_order_func()` 程序完成

#### Scenario: vectorbt 拒绝订单
- **WHEN** vectorbt 因现金、持仓或订单约束拒绝一笔请求
- **THEN** 系统把拒绝状态和策略原因保存为执行事实，不把正常拒单误报为框架失败

#### Scenario: 复用 vectorbt 持仓与交易访问器
- **WHEN** 共享账本需要 trades、positions 或 returns 视图
- **THEN** 它直接惰性缓存 vectorbt 的现成访问器结果，并默认保留所需记录；没有等价性和性能证据时不得关闭记录后自行重建

### Requirement: 每个策略必须只有一个公开 Strategy Module
每个本地研究策略 MUST 暴露一个版本化公开 Strategy Module，负责配置校验、输入准备、订单程序和策略结果扩展。策略 MAY 使用私有实现文件，但外部调用和测试不得把这些私有文件当作稳定 Interface。

#### Scenario: 策略内部重组
- **WHEN** 策略维护者在不改变公开 Module 和行为的前提下拆分或合并私有 Numba 内核文件
- **THEN** 共享层、其他策略和 Interface 级测试无需修改

#### Scenario: 禁止动态回调
- **WHEN** Strategy Module 根据场景参数构造订单程序
- **THEN** 它使用模块级固定 Numba 函数和稳定数组类型，不为每个配置动态创建闭包或 lambda

#### Scenario: 源码身份只有一个来源
- **WHEN** 父进程冻结并加载一个仓库内策略
- **THEN** 它从声明 module 定位当前顶层包目录并静态发现、排序其中全部普通 `.py` 文件，同一集合同时驱动运行身份和档案 `code/`；它不扫描 `research/archives/` 或相邻目录，descriptor 不再声明第二份源码清单

#### Scenario: 子进程加载策略
- **WHEN** 全新 `_execute` 子进程加载唯一 Strategy Module
- **THEN** 它把冻结策略根放到 `sys.path` 首位并使用标准 `importlib.import_module()`，不建立 UUID 私有命名空间、全局导入锁或手工模块缓存生命周期

### Requirement: 仓库内策略加载不得模拟敌对插件沙箱
Strategy Module MUST 是仓库内受版本管理和代码审查的可信代码。共享运行 MUST 使用受限源码路径、冻结输入、清理环境、全新子进程和超时限制执行策略；它 MUST NOT 安装 Python audit hook（审计钩子）或自行模拟操作系统沙箱。本 capability（能力）不得声称支持第三方不可信策略。

#### Scenario: 执行仓库内策略
- **WHEN** 固定 `_execute` 子进程加载已通过源码边界校验的 Strategy Module
- **THEN** 它直接通过标准导入执行，不复制或注入 `adapter_guard`，并继续受输入冻结、环境清理和超时限制

### Requirement: 所有项目必须通过固定共享入口运行
系统 MUST 从项目 `.venv` 调用共享本地研究 CLI。项目配置 MUST 声明仓库内 strategy root、module 和 symbol，不得声明策略专属 project entry、任意命令、系统 Python 或隐式依赖安装。

#### Scenario: 运行合法项目配置
- **WHEN** 配置声明有效的仓库内 Strategy Module、单一行情快照和一个场景
- **THEN** runner 在清理后的子进程中调用固定共享 CLI 并冻结全部声明输入

#### Scenario: 配置尝试执行任意命令
- **WHEN** 配置包含旧 `command`、策略专属 `project_entry`、仓库外模块路径或安装命令
- **THEN** 系统在启动项目进程前拒绝配置并返回 `evidence_insufficient`

### Requirement: 单场景停止状态必须保持固定
共享运行 MUST 每次只接受一个场景，并且只返回 `complete`、`evidence_insufficient` 或 `failed`。完整运行 MUST 返回 `next_action=return_to_caller`；Skill 不得循环多个场景、解释策略字段或自动给出研究推荐。

#### Scenario: 输入证据缺失
- **WHEN** 策略身份、配置、行情快照、范围或必需字段缺失
- **THEN** 系统返回 `evidence_insufficient`，不猜测替代输入且不执行策略

#### Scenario: 执行或证据失败
- **WHEN** vectorbt 执行异常、输出缺失、摘要冲突、临时清理失败或性能超限
- **THEN** 系统返回 `failed` 并保留紧凑失败证据，不发布完整运行

### Requirement: 重构必须保持结果一致并提供性能观测
系统 MUST 在真实规模、扩展资产和延迟场景校验重构前后结果。Schema、行数、成交、费用、现金、持仓、净值和逻辑摘要 MUST 完全一致；时间、内存和同逻辑核心/扩展 Parquet 数据载荷体积 MUST 在验证报告中列出，由用户人工确认，不设置自动相对门禁。日常 cold/warm 执行 MUST 继续满足各 180 秒超时。

正确性基线 MUST 直接从公开 `ExecutionLedger` 和 `ResultExtension` 生成，并用历史 v1 fixture 的 SHA256 记录迁移来源；系统不得为继续执行旧基线测试而保留第二套核心结果适配器。

#### Scenario: 真实规模性能复核
- **WHEN** 在相同机器和输入上运行 3,432 日 × 11 ETF 主场景
- **THEN** 结果证据零差异，验证报告列出与历史观测的时间、内存和 Parquet 数据载荷体积差异，并记录用户确认

#### Scenario: 扩展和延迟场景
- **WHEN** 运行 3,432 日 × 17 ETF 场景以及 `additional_delay_days=1` 场景
- **THEN** 两者分别通过相同的正确性与确定性校验，性能差异一并交由用户确认
