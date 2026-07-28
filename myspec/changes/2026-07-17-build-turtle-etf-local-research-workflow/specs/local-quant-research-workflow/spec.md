## ADDED Requirements

### Requirement: Skill 编排与共用脚本分层
系统 SHALL（必须）由 `run-local-quant-research` Skill（技能）只负责用户意图、流程顺序、输入输出、停止状态和安全边界；配置契约、运行身份、共享行情中心、项目安全调用和证据收口 SHALL（必须）由 `scripts/research/` 下与具体策略解耦的共用脚本实现。

#### Scenario: 有效配置调用策略模块
- **WHEN** 调用者提供通过契约校验的 `snapshot_id`（快照标识）、单场景配置、仓库内 `strategy.root/module/symbol` 和声明输入
- **THEN** Skill 调用共用运行器，运行器按“快照校验、配置校验、固定子进程执行、结果包校验、证据收口”的固定顺序执行并记录每一步状态

#### Scenario: 拒绝不安全策略入口
- **WHEN** 配置缺少必需字段、包含旧 `command/project_entry` 字段、引用仓库外策略或未声明输入
- **THEN** 系统停止在策略代码执行前，只允许固定 `_execute` 子进程命令，不拼接配置命令或扩大路径范围

#### Scenario: 通用层不解释策略语义
- **WHEN** 使用非海龟策略模块运行同一流程
- **THEN** Skill、共用脚本和共享行情中心无需海龟资产、参数、信号、风险或报告规则即可完成运行

### Requirement: 共享日线行情中心
系统 SHALL（必须）在仓库已忽略的 `.local/market-data/` 提供与任何策略解耦的共享行情中心；完整行情值不得写入公开仓库。首版只实现日线行情，但 SHALL（必须）通过来源、标的类型、频率和显式字段能力允许以后追加其他标的。

#### Scenario: 导入不可变行情批次
- **WHEN** 导入一个通过字段与来源校验的日线行情批次及其公司行动事件
- **THEN** 系统在 `.local/market-data/batches/<batch_id>/` 固化 `manifest.json`、权威 `market-data.parquet`（列式行情）、版本化 `corporate-actions.parquet`（公司行动事件）和 `validation.json`，并记录来源、标的类型、频率、字段、价格口径、每只证券实际起止日、公司行动知识截止日、两类行数、导出代码摘要、传输文件字节 SHA256（文件摘要）、规范化内容摘要和 Parquet 文件摘要

#### Scenario: 创建不可变快照引用
- **WHEN** 调用者从一个或多个已验证批次选择明确证券、日期、字段、来源和价格口径
- **THEN** 系统在 `.local/market-data/snapshots/<snapshot_id>.json` 创建只引用批次、不复制行情的不可变快照，策略运行只保存 `snapshot_id` 及其摘要

#### Scenario: 相同内容去重
- **WHEN** 新导入内容与既有批次的来源身份、结构版本和规范化逻辑内容摘要完全一致
- **THEN** 系统复用既有批次，不创建第二份权威行情

#### Scenario: 冲突重叠拒绝
- **WHEN** 新批次与既有批次在相同来源、频率、证券和日期上重叠，但字段值或价格口径不同
- **THEN** 系统拒绝合并并输出 `failed`，不得覆盖旧批次或静默选择其中一份

#### Scenario: 追加新标的不改变旧快照
- **WHEN** 行情中心追加新的证券或批次
- **THEN** 既有批次和既有 `snapshot_id` 的内容、查询结果与摘要保持不变

#### Scenario: 首版范围外的数据类型
- **WHEN** 调用者要求分钟线、基本面、财务或因子数据
- **THEN** 系统明确报告首版不支持，不把未实现的数据类型伪装成日线行情

### Requirement: 快照身份与完整性门禁
系统 SHALL（必须）验证快照引用的来源、标的类型、频率、数据截止日、价格口径、字段、证券清单、批次、导出代码摘要和文件 SHA256（文件摘要），不得用隐式默认值补齐缺失身份。

#### Scenario: 快照清单完整且文件匹配
- **WHEN** 快照清单包含全部身份字段、引用的批次均存在且重新计算的文件摘要与清单一致
- **THEN** 系统接受该快照并把身份和摘要写入本次运行清单

#### Scenario: 快照不可追溯或已被修改
- **WHEN** 任一身份字段缺失或任一文件摘要不匹配
- **THEN** 系统拒绝执行研究入口；身份或来源本来就缺失时输出 `evidence_insufficient`，既有文件被篡改或内容不一致时输出 `failed`

### Requirement: 权威 Parquet 与可重建 DuckDB 视图
系统 SHALL（必须）把每个批次的 `market-data.parquet` 作为本地唯一行情事实源；聚宽导出的 CSV（逗号分隔文件）只允许存在于传输和导入暂存阶段。DuckDB（嵌入式分析数据库）只从权威 Parquet 建立可重建的内存查询视图，不得保存持久数据库副本或第二份权威行情。`batch_id` SHALL（必须）绑定规范化逻辑内容与来源契约，Parquet 字节摘要 SHALL（必须）单独用于文件完整性验证，避免编码器版本变化静默改变逻辑身份。

#### Scenario: Parquet 与内存视图一致
- **WHEN** 已验证快照从权威 Parquet 批次建立 DuckDB 内存视图
- **THEN** 系统规范化字段顺序、类型、空值、排序和 `paused` 布尔类型后，清单中的规范化内容摘要与查询结果的行数和规范化内容摘要一致

#### Scenario: 派生视图发生漂移
- **WHEN** DuckDB 查询结果与权威 Parquet 的行集合、字段值、类型或规范化内容摘要不一致
- **THEN** 系统输出 `failed` 并停止项目研究，不把 DuckDB 结果视为替代事实源

#### Scenario: 原始价格与公司行动口径
- **WHEN** 批次通过 `fq=None` 或来源声明的等价方式导入
- **THEN** `market-data.parquet` 按固定结构保存实际未复权价格、`pre_close`（前收盘参考价）和 `factor`（复权因子），同一批次的 `corporate-actions.parquet` 保存版本化公司行动事件；两者及其摘要共同进入批次与快照身份。连续总回报价格只按生效日可见的原始 `close/pre_close` 行情事实在内存派生；公司行动元数据仅用于核对，不回写原始行情，也不固化为第二行情事实

#### Scenario: 公司行动只提供可审计事实
- **WHEN** 项目从共享快照请求拆分或现金分红信息
- **THEN** 共用行情层只提供证券、事件类型、来源事件标识、公告日、登记日、除权或生效日、支付日、拆分比例、每份现金、状态、知识截止日和来源摘要；它不得导入 vectorbt、生成订单、修改项目持仓或决定现金分红如何进入账户

#### Scenario: 公司行动缺失时关闭运行
- **WHEN** 项目请求区间内存在无法由公司行动事件解释的价格基准变化，或公司行动文件、来源摘要、事件字段和行情勾稽不完整
- **THEN** 快照校验或项目运行输出 `evidence_insufficient`，不得忽略事件、猜测类型、使用经验阈值或继续生成可被分析的本地结果

#### Scenario: 研究级近似不冒充精确账户
- **WHEN** 项目执行后端不能原生处理派息日现金或拆分后的真实份额状态
- **THEN** 共用流程允许项目声明版本化的研究级近似口径并继续运行，但本地清单必须明确记录价格基准、数量基准、现金分红处理、不能精确复核的账户字段和公司行动来源摘要；未声明精度边界的结果不得通过输出门禁

#### Scenario: 传输文件完成使命后清理
- **WHEN** 聚宽 CSV 已完成字节摘要核对、结构校验、Parquet 转换和逻辑内容复核
- **THEN** 系统删除本地暂存 CSV 和聚宽远端临时文件，仅在批次清单保留传输摘要；任一清理步骤无法确认时本次导入输出 `failed`

#### Scenario: 禁止持久 DuckDB 副本
- **WHEN** 查询或研究运行结束
- **THEN** `.local/market-data/` 中不存在作为长期事实源的 `.duckdb` 文件，后续查询可仅凭快照清单和权威 Parquet 重建

### Requirement: 唯一三态收口
每次运行 SHALL（必须）且只能以 `complete`、`evidence_insufficient` 或 `failed` 之一收口；流程状态不得与项目研究建议混为一谈。

#### Scenario: 证据不足
- **WHEN** 真实项目身份、快照、清单、必需字段、日期范围、来源证明或项目声明输入在执行前不完整
- **THEN** 系统输出 `evidence_insufficient`，不进入项目研究计算

#### Scenario: 执行或一致性失败
- **WHEN** 已存在的证据发生摘要不一致、结构或类型违规、重复键、冲突重叠、项目进程异常、硬约束突破、同输入结果不一致或远端临时文件无法确认清理
- **THEN** 系统输出 `failed`，保留失败证据且不得把部分结果标记为完成

#### Scenario: 完整成功
- **WHEN** 输入门禁、项目流程、声明输出、摘要校验和证据固化全部通过
- **THEN** 系统输出 `complete`

### Requirement: 不可变且原子固化的研究证据
系统 SHALL（必须）以快照摘要、生产配置摘要、规范化单场景配置摘要、自动发现的策略/共享运行时代码摘要和执行后端身份生成 `run_id`，先在暂存位置生成产物，全部校验通过后一次性固化包含输入、状态、结果包路径和输出摘要的不可变证据；不同场景配置不得复用同一 `run_id`。

#### Scenario: 首次成功运行
- **WHEN** 一个新 `run_id` 的全部输入、项目流程和输出校验通过
- **THEN** 系统原子固化运行证据，不留下可被误认成完成的中间目录

#### Scenario: 性能证据属于原子完成门禁
- **WHEN** 项目对同一单场景执行冷启动和预热性能复核
- **THEN** 两次执行、结果摘要一致性比较和各自不超过 180 秒上限的判断均在同一暂存区完成；比较通过后写入 `evidence/performance.json`、机械报告和最终清单，校验全部摘要后只原子发布一份权威结果包。任一失败不发布完成目录，只保留紧凑失败尝试证据

#### Scenario: 相同身份重复运行
- **WHEN** 已存在同一 `run_id` 的 `complete` 运行且全部产物重新校验通过
- **THEN** 系统复用既有完整产物，不重写文件或创建第二份权威证据

#### Scenario: 输入身份变化
- **WHEN** 快照、配置或代码摘要任一变化
- **THEN** 系统生成新的 `run_id`，不得更新或覆盖旧运行

#### Scenario: 同输入产生不同结果
- **WHEN** 相同快照、配置和代码产生与既有证据不同的输出摘要
- **THEN** 系统输出 `failed` 并记录确定性冲突，不覆盖既有 `complete` 运行

#### Scenario: 失败后重试
- **WHEN** 调用者重试一个 `failed` 或 `evidence_insufficient` 运行
- **THEN** 系统保留原尝试证据并创建新的尝试记录；只有新尝试全部通过才可固化为 `complete`

### Requirement: 共享 vectorbt 后端与策略模块分层
通用本地研究流程 SHALL（必须）把 vectorbt（向量化回测框架）`Portfolio.from_order_func()` 作为即时和后续执行的唯一账户账本。共享 runtime 负责成交、费用、现金、持仓和净值；策略模块只提供准备、订单程序、后续计划和版本化结果扩展，不维护第二套账户事实。

#### Scenario: 项目声明 vectorbt 执行后端
- **WHEN** `strategy-003` 使用 vectorbt 官方 `Portfolio.from_order_func()`（自定义订单函数）运行本地交易路径
- **THEN** 自动生成的代码身份与运行时锁记录 vectorbt、Numba（即时编译）、NumPy（数组计算）、Pandas（数据处理）和 PyArrow（列式计算）版本及摘要；外部配置不提供手工代码身份文件

#### Scenario: 非海龟项目复用同一共享后端
- **WHEN** 第二个策略模块提供不同策略逻辑且不提供海龟归因
- **THEN** Skill、共享行情中心和通用运行器不修改即可通过同一 vectorbt runtime 完成执行和核心结果包，策略扩展可以为空

#### Scenario: 执行后端依赖缺失
- **WHEN** 项目声明的 vectorbt、Numba 或兼容依赖在项目 `.venv`（虚拟环境）中缺失或版本不匹配
- **THEN** 通用运行器在项目执行前输出具体缺项并停止，不静默安装、升级或回退到另一执行后端

#### Scenario: 更换后端不改变通用分析契约
- **WHEN** 项目从旧逐日实现迁移到 vectorbt 后端
- **THEN** 共享 writer 输出与聚宽现有归档同名同义的 `results`、`balances`、`positions`、`orders` 四类共同执行事实和独立 `local-research-package/2` 清单；既有聚宽归档无需改动，分析层无需解释 vectorbt 对象

### Requirement: 每次 Skill 调用只交付一个场景结果
本地研究流程 SHALL（必须）每次只接受一个策略项目、一个快照和一个场景配置，只编排一次项目执行、单份兼容结果校验、运行身份和证据收口，不接收候选数组、不循环多个场景，也不调用或包含策略分析 Skill（技能）。`scripts/research/local_quant_research/` 和策略项目不得导入绩效、归因、稳健性、压力、证据矩阵、报告或推荐算法。

#### Scenario: 成功交付单场景结果
- **WHEN** 调用者提交一个完整场景配置且项目执行与统一契约校验通过
- **THEN** 本地研究流程在 `.local/quant-research/<strategy_id>/<run_id>/` 直接固化一份 `local-research-package/2` 自包含结果包，记录场景身份、完整策略源码、配置、行情/环境/代码身份、四张核心表、策略扩展、性能证据和机械执行报告，并以 `next_action=return_to_caller` 停止

#### Scenario: 本地结果晋升且不混入聚宽正式归档
- **WHEN** 调用者把完整运行晋升到 `joinquant/strategies/<strategy_id>/research/archives/<analysis_id>/`
- **THEN** 晋升只逐字节复制、摘要复核和原子发布，不重算结果；该目录明确属于本地探索性研究，不写入或改变聚宽正式 `backtests/`、`simulations/` 及其同步流程

#### Scenario: 拒绝批量候选输入
- **WHEN** 调用者把冻结基线、挑战数组、参数网格或稳健性场景列表作为一次 Skill 输入
- **THEN** 本地研究流程拒绝批量请求并要求调用者拆成单场景调用；Skill 不生成 `candidate-strategies.json`、`local-research-manifest.json`、排名或聚合结果

#### Scenario: 不启动策略分析
- **WHEN** 单场景兼容结果已经完整
- **THEN** 本地研究流程不计算 Alpha/Beta（超额收益/市场暴露）、稳健性、压力或推荐，不生成完整策略分析报告，也不调用后续策略分析 Skill；包内机械执行报告只复述核心表、配置、性能门禁和来源身份

#### Scenario: 依赖方向检查
- **WHEN** 扫描本地研究运行器和策略项目的生产导入
- **THEN** 两者只依赖以聚宽现有结果数据为基准的读取契约，不导入 `quant_analysis` 的指标、归因、稳健性、压力、证据矩阵或报告实现

#### Scenario: 独立资产扩展使用真实单场景矩阵
- **WHEN** 主 agent 已冻结候选前置筛选结果并生成原 11 只基线、完整扩展、逐只删除、逐扩展切片删除和五个成本执行压力场景
- **THEN** 每个场景分别调用本地研究 Skill 一次并生成一个标准结果包，全部六只候选通过时总数为 16
- **AND** Skill 的输入输出和停止状态仍只表达一个场景，不包含本次候选、场景数量或分析逻辑

#### Scenario: 不同资产池使用可比较的精确快照
- **WHEN** 两个调用场景的证券集合不同
- **THEN** 每个调用只接收与自身证券集合完全一致的不可变快照
- **AND** 快照共享相同不可变批次、截止日、字段和价格口径，重叠证券行情及公司行动摘要必须完全一致

### Requirement: 仓库运行与能力复用边界
系统 MUST（必须）使用项目 `.venv`（虚拟环境）运行本地 Python（编程语言）入口，并复用既有聚宽认证和归档能力，不得保存或打印账号、密码、Token（访问令牌）或 Cookie（浏览器凭证）。

#### Scenario: 需要既有聚宽对象能力
- **WHEN** 流程需要认证或归档既有聚宽远端对象
- **THEN** Skill 调用 `joinquant-archive-sync`（聚宽归档同步）的公开入口，而不是复制其实现

#### Scenario: 运行环境或依赖缺失
- **WHEN** 项目 `.venv` 不存在或明确必需依赖不可用
- **THEN** 系统报告具体缺项并停止，不回退到系统 Python 或静默安装依赖

### Requirement: Skill 结构和通用性验证
实现 SHALL（必须）使用 `init_skill.py` 初始化 `run-local-quant-research`，通过 `quick_validate.py`、仓库布局测试、确定性脚本测试、用户入口 E2E（端到端）回归和非海龟前向验证。

#### Scenario: Skill 布局有效
- **WHEN** 运行结构与布局验证
- **THEN** `.agents/skills/run-local-quant-research/` 的元数据和必要资源有效，且 `.claude/skills/` 兼容链接解析到同一 Skill

#### Scenario: 通用能力与海龟项目解耦
- **WHEN** 在不提供海龟目录、参数、资产和代码的环境中运行通用单元测试及非海龟最小任务
- **THEN** Skill 仍能完成流程并生成可验证证据，且其目录不包含海龟专属常量或项目产物

#### Scenario: 共享行情中心回归
- **WHEN** 运行行情中心自动测试
- **THEN** 测试覆盖 CSV 暂存导入、Parquet 不可变批次、逻辑内容去重、追加新标的、旧快照复算不变、冲突重叠拒绝、字段能力、快照摘要、Parquet 到内存 DuckDB 一致性及暂存清理，并确认未生成持久 DuckDB 文件

#### Scenario: 非海龟完整 E2E
- **WHEN** 从 Skill 用户入口使用非海龟最小策略模块和固定日线夹具运行
- **THEN** 流程完整经过 Skill、共用运行器、共享行情中心、固定子进程、共享 vectorbt runtime、标准结果包和不可变证据收口

#### Scenario: 用户入口完整回归
- **WHEN** 从 Skill 文档公开的用户入口启动离线研究夹具
- **THEN** 流程实际贯通 CSV 暂存导入、Parquet 固化、快照引用、内存 DuckDB 查询、项目进程、输出验证和三态收口，而不是以若干孤立单元测试代替

#### Scenario: 公开仓库安全扫描
- **WHEN** 运行仓库安全检查
- **THEN** Git（版本管理）跟踪文件中不存在完整行情值、账号、Cookie（浏览器凭证）或 Token（访问令牌）
