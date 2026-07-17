## MODIFIED Requirements

### Requirement: Skill 结构和通用性验证
实现 SHALL（必须）使用 `init_skill.py` 初始化 `run-local-quant-research`，通过 `quick_validate.py`、仓库布局测试、确定性脚本测试、用户入口 E2E（端到端）回归和非海龟前向验证。仓库 `verify --full`（全量验证）MUST 保留全部测试，只在本机进程及其子进程内执行，不得联网调用外部系统，并 MUST 在 30 秒内完成。

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

#### Scenario: 全量验证性能与执行边界
- **WHEN** 在本机从仓库入口连续执行非缓存 `verify --full`
- **THEN** 每次运行都执行全部单元测试和 E2E 回归，不联网调用外部系统，并在 30 秒内成功结束
