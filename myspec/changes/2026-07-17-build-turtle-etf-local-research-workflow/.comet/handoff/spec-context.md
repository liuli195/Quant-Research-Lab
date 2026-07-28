# Comet Spec Context

- Change: build-turtle-etf-local-research-workflow
- Phase: design
- Mode: beta
- Context hash: dc23e3d037dbdd7bda188f0a24bdb5d9ef6639ef9ec33a1ce0a5a9fda3f39284

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This beta context pack verbatim-projects spec files and references supporting artifacts by hash, not an agent-authored summary.

## Source References

- Source: openspec/changes/build-turtle-etf-local-research-workflow/proposal.md
- SHA256: 91408dcb677019054822e9efec483b54ed59b5a0cb2e5fd4fdff36f6be8b7423
- Source: openspec/changes/build-turtle-etf-local-research-workflow/design.md
- SHA256: 2dc2cfc8ae02fd5861d2bcad459a3d51d289d1c245614ea040cdbfcb523f084d
- Source: openspec/changes/build-turtle-etf-local-research-workflow/tasks.md
- SHA256: 08045cfc3dbf20e113f1b24b0ff4357652d85757e28606319bd2d8e1532c8fcc
- Source: openspec/changes/build-turtle-etf-local-research-workflow/specs/local-quant-research-workflow/spec.md
- SHA256: 95f938a53189728ff9de011d50ef7eac48f849b4661e5c18150570b3ae144342
- Source: openspec/changes/build-turtle-etf-local-research-workflow/specs/turtle-etf-local-research/spec.md
- SHA256: 41bacd2659343a861d0c25f8cd77bcfd021892d3aa2e61e635a56410fcf53780

## Acceptance Projection

## openspec/changes/build-turtle-etf-local-research-workflow/specs/local-quant-research-workflow/spec.md

- Source: openspec/changes/build-turtle-etf-local-research-workflow/specs/local-quant-research-workflow/spec.md
- Lines: 1-153
- SHA256: 95f938a53189728ff9de011d50ef7eac48f849b4661e5c18150570b3ae144342

```md
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
- **THEN** 系统在 `.local/market-data/batches/<batch_id>/` 固化 `manifest.json`、精确原始 `market-data.csv` 和 `validation.json`，并记录来源、标的类型、频率、字段、价格口径、每只证券实际起止日、行数、导出代码摘要和 CSV 字节 SHA256（文件摘要）

#### Scenario: 创建不可变快照引用
- **WHEN** 调用者从一个或多个已验证批次选择明确证券、日期、字段、来源和价格口径
- **THEN** 系统在 `.local/market-data/snapshots/<snapshot_id>.json` 创建只引用批次、不复制行情的不可变快照，策略运行只保存 `snapshot_id` 及其摘要

#### Scenario: 相同内容去重
- **WHEN** 新导入内容与既有批次的来源身份和 CSV 字节摘要完全一致
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

### Requirement: 权威 CSV 与可重建 DuckDB 视图
系统 SHALL（必须）把每个批次的精确原始 `market-data.csv` 作为唯一行情事实源；DuckDB（嵌入式分析数据库）只从权威 CSV 建立可重建的内存查询视图，不得保存持久数据库副本或第二份权威行情。

#### Scenario: CSV 与内存视图一致
- **WHEN** 已验证快照从权威 CSV 批次建立 DuckDB 内存视图
- **THEN** 系统规范化字段顺序、类型、空值、排序和 `paused` 布尔类型后，CSV 与查询结果的行数和规范化内容摘要一致

#### Scenario: 派生视图发生漂移
- **WHEN** DuckDB 查询结果与权威 CSV 的行集合、字段值、类型或规范化内容摘要不一致
- **THEN** 系统输出 `failed` 并停止项目研究，不把 DuckDB 结果视为替代事实源

#### Scenario: 未复权价格口径
- **WHEN** 批次通过 `fq=None` 或来源声明的等价方式导入
- **THEN** CSV 精确保留实际未复权价格和 `factor`（复权因子），本流程不生成或使用复权价格序列

#### Scenario: 禁止持久 DuckDB 副本
- **WHEN** 查询或研究运行结束
- **THEN** `.local/market-data/` 中不存在作为长期事实源的 `.duckdb` 文件，后续查询可仅凭快照清单和权威 CSV 重建

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
- **THEN** 测试覆盖不可变批次导入、相同内容去重、追加新标的、旧快照复算不变、冲突重叠拒绝、字段能力、快照摘要及 CSV 到内存 DuckDB 一致性，并确认未生成持久 DuckDB 文件

#### Scenario: 非海龟完整 E2E
- **WHEN** 从 Skill 用户入口使用非海龟最小项目适配器和固定日线夹具运行
- **THEN** 流程完整经过 Skill、共用运行器、共享行情中心、项目入口、声明输出和不可变证据收口

#### Scenario: 用户入口完整回归
- **WHEN** 从 Skill 文档公开的用户入口启动离线研究夹具
- **THEN** 流程实际贯通快照引用、CSV 校验、内存 DuckDB 查询、项目进程、输出验证和三态收口，而不是以若干孤立单元测试代替

#### Scenario: 公开仓库安全扫描
- **WHEN** 运行仓库安全检查
- **THEN** Git（版本管理）跟踪文件中不存在完整行情值、账号、Cookie（浏览器凭证）或 Token（访问令牌）

```

## openspec/changes/build-turtle-etf-local-research-workflow/specs/turtle-etf-local-research/spec.md

- Source: openspec/changes/build-turtle-etf-local-research-workflow/specs/turtle-etf-local-research/spec.md
- Lines: 1-127
- SHA256: 41bacd2659343a861d0c25f8cd77bcfd021892d3aa2e61e635a56410fcf53780

```md
## ADDED Requirements

### Requirement: 海龟项目内容与通用 Skill 隔离
系统 SHALL（必须）先创建真实 JoinQuant（聚宽）策略空壳并同步为 `strategy-003`，仅建立身份且不启动正式回测；海龟 ETF（交易型开放式指数基金）的资产池、参数、交易规则、策略代码、研究配置和证据保存在 `joinquant/strategies/strategy-003/research/` 及对应本地运行证据中，通过通用配置契约接入流程。`strategy-001` 与 `strategy-002` 不得修改。

#### Scenario: 真实身份建立后创建项目目录
- **WHEN** 聚宽策略空壳、聚宽详情页身份和本地索引已验证为唯一对应的 `strategy-003`
- **THEN** 系统建立 `strategy-003` 研究项目但不启动正式回测；身份缺失、冲突或占用时停止

#### Scenario: 通用 Skill 调用海龟项目
- **WHEN** 海龟项目配置传入策略入口、共享 `snapshot_id`（快照标识）和输出路径
- **THEN** 通用 Skill 只执行契约校验和编排，海龟项目模块独立计算交易规则并生成项目产物

#### Scenario: 检查反向依赖
- **WHEN** 检查 Skill 源码、参考和夹具
- **THEN** 其中不得包含 11 只 ETF、55 日入场、N 值、加仓、止损或海龟验收门槛的硬编码

#### Scenario: 策略不得拥有行情副本
- **WHEN** 检查 `strategy-003` 项目与运行证据
- **THEN** 项目只引用共享 `snapshot_id` 及摘要，不在策略目录复制权威 CSV、快照批次或持久 DuckDB（嵌入式分析数据库）

### Requirement: 聚宽权威行情快照
海龟项目 SHALL（必须）通过共享行情中心引用由 JoinQuant（聚宽）研究环境取得的方案指定 11 只 ETF 日线行情。每只 ETF 从自身首个可用完整交易日导出到运行时显式指定的 `snapshot_end_date`（快照截止日）；2015-01-01 之前数据只作指标预热。字段固定为 `date`、`security`、`open`、`high`、`low`、`close`、`pre_close`、`volume`、`money`、`factor`、`paused`、`high_limit`、`low_limit`，静态证券信息保存在批次清单。

#### Scenario: 接受完整权威快照
- **WHEN** 共享快照覆盖项目清单中的 11 只 ETF、固定 13 个字段、各自实际起止日、未复权口径和完整身份摘要
- **THEN** 项目接受该 `snapshot_id` 作为本次本地研究的唯一行情引用，不复制行情

#### Scenario: 快照缺失或来源不明确
- **WHEN** 任一证券或必需字段缺失、价格口径未记录、数据截止日不明确或来源无法追溯
- **THEN** 项目输出 `evidence_insufficient`，不得用代理数据、旧快照或默认口径补齐

#### Scenario: 新 ETF 冷启动
- **WHEN** 某只 ETF 尚未具有 60 个完整、有效且日期对齐的收益样本
- **THEN** 项目保留其行情和审计，但禁止该 ETF 新建仓或加仓，不把全局运行标记为失败

### Requirement: 聚宽导出与价格口径
共享聚宽日线导出器 SHALL（必须）在聚宽研究内核直接调用注入的 `get_price`、`write_file` 和 `read_file` 接口，使用 `fq=None` 与 `skip_paused=False`，按固定字段和排序生成精确 `market-data.csv`；实现 SHALL（必须）兼容 Pandas（数据处理库）0.23.4 的 `line_terminator`，并在查询层把 `paused` 规范化为布尔值。

#### Scenario: 未复权信号数据
- **WHEN** 导出海龟项目日线行情
- **THEN** CSV 保存实际未复权 OHLCV（开高低收量）及 `factor`（复权因子），本地研究不生成或使用复权价格序列

#### Scenario: 聚宽策略价格模式
- **WHEN** 生成供后续聚宽功能校验使用的海龟策略代码
- **THEN** 信号行情显式使用 `fq=None` 且策略设置 `use_real_price=False`，并把聚宽在该模式下使用固定基准日前复权撮合价记录为平台限制，不宣称撮合使用未复权实际价

#### Scenario: 导出传输与清理
- **WHEN** 聚宽端临时文件已传输到本地
- **THEN** 系统先验证本地字节 SHA256 与远端回读一致，再删除远端临时文件；无法确认删除时本次运行输出 `failed`

### Requirement: 海龟规则的确定性验证
海龟项目 MUST（必须）以项目代码和固定夹具验证已确认基线：此前 55 日最高价收盘突破入场、20 日 N 值、0.5% 初始风险单位、固定 0.5N 加仓档位、只上移的 2N 共同止损、此前 20 日最低价收盘跌破退出，以及资金、流动性、单 ETF、资产组、组合风险和目标波动率约束。

#### Scenario: 入场与 N 值计算
- **WHEN** 固定行情在收盘价突破不含当日的此前 55 日盘中最高价，且具有足够有效 TR（真实波幅）与协方差样本
- **THEN** 项目按确认公式计算信号日 N、理论单位和次日候选订单；盘中突破但收盘未突破不得产生订单

#### Scenario: 加仓与共同止损
- **WHEN** 已成交仓位在后续收盘跨过一个或多个固定 0.5N 档位
- **THEN** 项目每天至多申请一次受全部预算约束的加仓，只有实际成交才能推动共同止损，且共同止损不得下移

#### Scenario: 退出与故障安全
- **WHEN** 收盘价跌破不含当日的此前 20 日盘中最低价、触发保护性止损，或数据缺失导致协方差无法可靠更新
- **THEN** 前两种条件生成全部批次退出；数据故障暂停新增风险但保留能够成交的退出和风险减仓

#### Scenario: 风险或整手预算不足
- **WHEN** 候选订单超过任一项目风险、资金、流动性或目标波动率上限，或裁剪后不足一个交易整手
- **THEN** 项目按确认规则缩小或跳过订单，不使用融资、透支或跨 ETF 锁定利润补足预算

#### Scenario: 持仓风险输入缺失
- **WHEN** 任一持仓 ETF 缺少可用价格或无法形成可靠协方差输入
- **THEN** 项目停止所有新增风险，不以零值、陈旧协方差或虚构成交继续；其他可交易 ETF 仍允许退出和强制减仓

### Requirement: 完整本地交易主流程
海龟项目 SHALL（必须）用固定行情和成交夹具完整执行“收盘信号、次日订单、成交回填、持仓与风险状态、审计输出”，并把同日订单优先级和预算分配作为显式项目规则验证。

#### Scenario: 入场主流程贯通
- **WHEN** 固定行情在交易日收盘产生有效入场信号且次日具备可成交开盘价
- **THEN** 项目在下一交易日生成订单、回填实际成交、建立批次与共同止损，并在审计记录中关联信号、订单、成交和风险状态

#### Scenario: 多类订单同日出现
- **WHEN** 同一开盘同时存在退出、风险减仓、建仓或加仓候选
- **THEN** 项目依次处理全仓退出、强制风险减仓、同级的新建仓与加仓；同一 ETF 当日出现退出时取消其全部买入候选

#### Scenario: A1 共享预算分配
- **WHEN** 多个有效新建仓或加仓候选争用有限资金与风险预算
- **THEN** 每个候选先生成最多一个 U0（标准单位）的请求量，系统使用同一完成比例缩减全部可行请求，直到资金、单 ETF、资产组、组合计划风险和目标波动率约束全部满足；被自身或所属组上限卡住的未用预算可流向其他仍可增仓候选

#### Scenario: A1 整手余额分配
- **WHEN** 等比缩放后的请求量不是完整交易手数且仍有剩余预算
- **THEN** 系统先向下取整，再按小数余额从大到小逐手补分；每补一手重新检查全部硬门槛，完全同分时按 ETF 代码升序确定，候选输入顺序不得改变结果

#### Scenario: 退出主流程贯通
- **WHEN** 既有持仓产生有效趋势退出或保护性止损条件并在次日成交
- **THEN** 项目关闭该 ETF 全部批次、更新现金与组合风险状态，并输出可追溯审计记录

### Requirement: 探索性粗筛及结果边界
海龟项目 SHALL（必须）允许 Vibe-Trading（AI 研究助理）做方向性粗筛、归因和报告，并由确定性项目计算复核事件与风险；所有产物必须关联快照、代码和配置摘要并明确标为非正式结果。

#### Scenario: 生成可复算研究报告
- **WHEN** 数据桥、策略规则和完整本地主流程均通过验证
- **THEN** 项目生成 `research-report.md`，包含方法、输入身份、事件与交易结果、仓位分布、现金占比、留现原因、资产组与组合风险使用率、限制和产物摘要，且可按证据索引复算

#### Scenario: 生成研究建议
- **WHEN** 完整本地研究运行结束
- **THEN** 项目生成 `conclusion.json`，研究建议且只能为 `proceed_to_joinquant`（进入聚宽回测）、`revise_and_reassess`（修订后再评估）或 `stop_evidence_insufficient`（证据不足而停止）之一，并列出确定性理由、阻断项及“不是正式回测或最终验收结论”的声明

#### Scenario: 生成固定候选策略包
- **WHEN** 本地研究报告和研究建议均通过输出校验
- **THEN** 项目生成 `candidate-strategies.json`，恰好包含一项冻结基线和 40/60 日入场、1.5N/2.5N 止损、120 日/30 日半衰期 EWMA（指数加权移动平均）协方差六项预设单项挑战配置；七项共用同一代码摘要和 `snapshot_id`

#### Scenario: 禁止本地结果删选候选
- **WHEN** 本地粗筛显示某候选收益更高或更低
- **THEN** 项目不得按本地收益排名删除候选、替换冻结基线或产生未预设参数，只记录方向性证据与风险提示

#### Scenario: 已知组合优化器缺陷仍存在
- **WHEN** 当前 Vibe-Trading 版本尚未包含已知前视偏差修复
- **THEN** 项目跳过受影响的组合优化器并记录原因，不用其结果筛选参数，也不阻塞其他方向性研究

#### Scenario: 防止本地结果越界
- **WHEN** 本地粗筛或确定性回归产生收益、交易或风险统计
- **THEN** 报告必须声明其不是聚宽正式回测、模拟交易或验收结论，并不得据此修改已确认基线参数

#### Scenario: 三类输出完成门禁
- **WHEN** `research-report.md`、`conclusion.json` 或 `candidate-strategies.json` 任一缺失、结构无效或摘要不匹配
- **THEN** 本次运行不得输出 `complete`

```

Full source files remain canonical. If a required heading or scenario is missing here, regenerate the handoff or read the source spec directly. Supporting files (proposal, design, tasks) are referenced by hash only.