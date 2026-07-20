## MODIFIED Requirements

### Requirement: Skill 结构和通用性验证
实现 SHALL（必须）以 `.agents/skills/run-local-quant-research/` 中的元数据、`.claude/skills/` 兼容链接、仓库布局测试、契约测试、确定性回归、共享行情链路回归和用户入口 E2E（端到端）回归验证 `run-local-quant-research` Skill（技能）。组合式 E2E（端到端）夹具 SHALL（必须）先通过共享行情能力完成 CSV（逗号分隔文件）暂存导入、Parquet（列式文件）固化和不可变快照创建，再通过 Skill 文档公开的 `run` 入口执行本地研究。仓库 `verify --full`（全量验证）MUST 保留全部测试，只在本机进程及其子进程内执行，不得联网调用外部系统；其性能预算为 60 秒，超预算 MUST（必须）记录性能报告和告警，但不得覆盖各检查决定的功能通过状态。

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
- **WHEN** 组合式 E2E 夹具通过共享行情能力准备固定日线 CSV、不可变 Parquet 批次和快照后，从 Skill 文档公开的用户入口使用非海龟最小策略模块运行
- **THEN** 流程完整经过共享行情中心、固定子进程、共享 vectorbt runtime、标准结果包和不可变证据收口；`run` 入口只消费已验证快照，不负责导入或创建快照

#### Scenario: 用户入口完整回归
- **WHEN** 组合式 E2E 夹具从固定 CSV 开始，经共享行情能力完成暂存导入、Parquet 固化和快照引用后，再调用 Skill 文档公开的 `run` 入口
- **THEN** 测试实际验证 CSV 到快照的数据链路，以及从快照到内存 DuckDB 查询、项目进程、输出验证和三态收口的研究链路，而不是以若干孤立单元测试替代

#### Scenario: 公开仓库安全扫描
- **WHEN** 运行仓库安全检查
- **THEN** Git（版本管理）跟踪文件中不存在完整行情值、账号、Cookie（浏览器凭证）或 Token（访问令牌）

#### Scenario: 全量验证性能与执行边界
- **WHEN** 在本机从仓库入口连续执行非缓存 `verify --full`
- **THEN** 每次运行都执行全部单元测试和 E2E（端到端）回归，不联网调用外部系统；系统以 60 秒作为全量验证性能预算，并在超过预算时生成包含 `overBudget`（超预算）状态的性能报告和告警，功能通过状态由全部检查结果决定
