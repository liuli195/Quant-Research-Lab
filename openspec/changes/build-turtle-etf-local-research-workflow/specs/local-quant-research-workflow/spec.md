## ADDED Requirements

### Requirement: Skill 编排与共用脚本分层
系统 SHALL（必须）由 `run-local-quant-research` Skill（技能）只负责用户意图、流程顺序、输入输出、停止状态和安全边界；配置契约、运行身份、共享行情中心、项目安全调用和证据收口 SHALL（必须）由 `scripts/research/` 下与具体策略解耦的共用脚本实现。

#### Scenario: 有效配置调用项目适配器
- **WHEN** 调用者提供通过契约校验的 `snapshot_id`（快照标识）、项目配置、仓库内项目入口参数数组和必需输出
- **THEN** Skill 调用共用运行器，运行器按“快照校验、配置校验、项目入口、输出校验、证据收口”的固定顺序执行并记录每一步状态

#### Scenario: 拒绝不安全项目入口
- **WHEN** 配置缺少必需字段、使用 Shell（命令解释器）字符串、引用仓库外入口或未声明必需输出
- **THEN** 系统停止在项目代码执行前，不拼接命令、不扩大文件访问范围，并输出 `evidence_insufficient`

#### Scenario: 通用层不解释策略语义
- **WHEN** 使用非海龟项目适配器运行同一流程
- **THEN** Skill、共用脚本和共享行情中心无需海龟资产、参数、信号、风险或报告规则即可完成运行

### Requirement: 共享日线行情中心
系统 SHALL（必须）在仓库已忽略的 `.local/market-data/` 提供与任何策略解耦的共享行情中心；完整行情值不得写入公开仓库。首版只实现日线行情，但 SHALL（必须）通过来源、标的类型、频率和显式字段能力允许以后追加其他标的。

#### Scenario: 导入不可变行情批次
- **WHEN** 导入一个通过字段与来源校验的日线行情批次
- **THEN** 系统在 `.local/market-data/batches/<batch_id>/` 固化 `manifest.json`、权威 `market-data.parquet`（列式行情）和 `validation.json`，并记录来源、标的类型、频率、字段、价格口径、每只证券实际起止日、行数、导出代码摘要、传输文件字节 SHA256（文件摘要）、规范化内容摘要和 Parquet 文件摘要

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

#### Scenario: 未复权价格口径
- **WHEN** 批次通过 `fq=None` 或来源声明的等价方式导入
- **THEN** Parquet 按固定结构保存实际未复权价格和 `factor`（复权因子），本流程不生成或使用复权价格序列

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

#### Scenario: 完成不等于策略通过
- **WHEN** 流程完整执行但项目建议为 `revise_and_reassess`（修订后再评估）
- **THEN** 运行状态仍可为 `complete`，且不得把该状态解释为正式回测通过或进入实盘

### Requirement: 不可变且原子固化的研究证据
系统 SHALL（必须）以快照摘要、项目配置摘要和代码摘要生成 `run_id`，先在暂存位置生成产物，全部校验通过后一次性固化包含输入、命令、状态、输出路径和输出摘要的不可变证据索引。

#### Scenario: 首次成功运行
- **WHEN** 一个新 `run_id` 的全部输入、项目流程和输出校验通过
- **THEN** 系统原子固化运行证据，不留下可被误认成完成的中间目录

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
- **WHEN** 从 Skill 用户入口使用非海龟最小项目适配器和固定日线夹具运行
- **THEN** 流程完整经过 Skill、共用运行器、共享行情中心、项目入口、声明输出和不可变证据收口

#### Scenario: 用户入口完整回归
- **WHEN** 从 Skill 文档公开的用户入口启动离线研究夹具
- **THEN** 流程实际贯通 CSV 暂存导入、Parquet 固化、快照引用、内存 DuckDB 查询、项目进程、输出验证和三态收口，而不是以若干孤立单元测试代替

#### Scenario: 公开仓库安全扫描
- **WHEN** 运行仓库安全检查
- **THEN** Git（版本管理）跟踪文件中不存在完整行情值、账号、Cookie（浏览器凭证）或 Token（访问令牌）
