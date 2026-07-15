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
- **THEN** 项目只引用共享 `snapshot_id` 及摘要，不在策略目录复制权威 Parquet（列式行情）、快照批次或持久 DuckDB（嵌入式分析数据库）

### Requirement: 策略自有机器可读分析计划
海龟项目 SHALL（必须）在 `joinquant/strategies/strategy-003/research/analysis-plan.json` 保存版本化、机器可读的研究场景定义。通用 `quant_analysis`（量化分析） SHALL（必须）只按 `scripts/research/quant_analysis/schemas/analysis-plan.schema.json` 校验并展开通用场景，不得解析 Markdown（文档标记语言）或硬编码海龟资产、参数、分组、数量和门槛。本地研究 Skill SHALL NOT（不得）读取该分析计划。

#### Scenario: 分析计划完整描述海龟研究范围
- **WHEN** 主 agent（代理）准备执行基础研究或完整稳健性分析
- **THEN** `analysis-plan.json` 以 `schema_version=strategy-analysis-plan/1` 明确给出一个冻结基线、六个单项挑战、三个固定时期、三年季度滚动规则、11只 ETF 删除集合、6个资产组删除集合、3个成本与延迟执行场景，以及区块抽样、历史压力、持仓冲击、CVaR（条件风险价值）的定义、期望数量、计算方法、固定随机种子和门槛

#### Scenario: 通用分析计划字段可直接执行
- **WHEN** 通用 Schema 校验 `analysis-plan.json`
- **THEN** 顶层至少包含 `schema_version`、`strategy_id`、`baseline_config`、`scenarios`、`universe`、`analyses`、`expected` 和 `thresholds`；七个基础场景各自包含唯一 `scenario_id`、`dimension` 和结构化 `overrides`，其余分析包含日期、资产、成本、抽样、冲击、门槛或固定种子等机器可读配置，不允许用自由文本代替可执行字段

#### Scenario: 通用分析层只校验与展开
- **WHEN** `quant_analysis` 接收任一符合通用 Schema（结构约束）的策略分析计划
- **THEN** 它输出版本化 `analysis-scenarios.json` 和七份基础场景配置，不导入 `strategy-003` 或海龟模块；主 agent 每次只把其中一份场景配置和运行时 `snapshot_id` 交给本地研究 Skill，其余分析保留为确定性分析配置

#### Scenario: 分析计划不向 Skill 注入复数编排
- **WHEN** 冻结基线和六个挑战需要执行
- **THEN** 主 agent 从分析计划读取七个独立场景并分别调用 Skill 七次；Skill 的输入仍只有一个场景，不接收分析计划、候选数组、数量、顺序或循环指令

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
共享聚宽日线导出器 SHALL（必须）在聚宽研究内核直接调用注入的 `get_price`、`write_file` 和 `read_file` 接口，使用 `fq=None` 与 `skip_paused=False`，按固定字段和排序生成传输用 `market-data.csv`；实现 SHALL（必须）兼容 Pandas（数据处理库）0.23.4 的 `line_terminator`。本地导入器 SHALL（必须）校验传输字节后转换为权威 `market-data.parquet`，并在查询层把 `paused` 规范化为布尔值。

#### Scenario: 原始行情与研究核算价格分离
- **WHEN** 导出海龟项目日线行情
- **THEN** 传输 CSV 与权威 Parquet 保存实际未复权 OHLCV（开高低收量）、`pre_close`（前收盘参考价）及 `factor`（复权因子）作为审计事实；本地 vectorbt（向量化回测框架）只在内存中按应用日可见的原始行情事实派生连续总回报 OHLC，统一用于信号、N 值、协方差、风险、成交和估值。结果中的价格与数量分别表示连续经济价格和经济单位，不宣称是聚宽原始撮合价或真实 ETF 份额

#### Scenario: 公司行动证据完整
- **WHEN** 相邻原始收盘价与下一交易日 `pre_close` 显示除权、份额拆分或现金分配
- **THEN** 快照必须同时提供可校验的公司行动事件，至少包含证券、类型、来源事件标识、公告日、权益登记日、除权或生效日、到账日、拆分比例或每份现金金额、事件状态、知识截止日、来源身份和摘要。连续因子只由实际应用日可见的 `上一交易日原始 close / 当日原始 pre_close` 决定；公告日在生效日之前或当日的元数据标记为 `point_in_time`，晚于生效日的元数据只能用于 `retrospective_reconciliation`（事后核对），不得改变因子或交易。官方拆分比例或每份现金仅作审计；取消事件不得授权变化，类型或状态不明、知识截止日无效、事件缺失或价格基准变化没有对应有效事件时以 `evidence_insufficient` 停止，不得按价格变化幅度猜测

#### Scenario: 取消状态按快照截止日重建
- **WHEN** 聚宽当前记录显示事件已取消
- **THEN** 导出器必须比较取消日期与 `snapshot_end_date`：截止日后的取消在该快照中仍记录为 `active`，截止日当日或之前才记录为 `cancelled`；当前显示已取消但缺少取消日期时必须停止导出，不能用当前 `process_id` 推断历史状态

#### Scenario: 行情时点可知连续总回报价格
- **WHEN** 有效公司行动导致当日 `pre_close` 与上一交易日原始 `close` 的价格基准发生变化
- **THEN** 输入构造器以 `上一交易日原始 close / 当日 pre_close` 更新从实际应用日开始生效的累计连续因子，并用同一因子换算当日及以后原始 OHLC 和 `pre_close`；生效日停牌时延后到首个复牌行情日，应用日前的历史价格不得因未来事件被改写。有效事件没有价格基准变化时只记录审计且因子保持不变；官方比例不作为因子输入或数值勾稽条件

#### Scenario: 拆分使用经济单位近似
- **WHEN** 持仓 ETF 发生经验证的份额拆分或合并
- **THEN** vectorbt 中的经济单位保持稳定，连续经济价格消除机械价格跳变，突破、N 值、收益、协方差、风险和组合权益不得出现公司行动造成的虚假变化；归因日志记录事件、累计连续因子和官方拆分比例。结果不得声称经济单位等于拆分后的真实份额、真实成本或碎股现金

#### Scenario: 现金分红按隐含再投资近似
- **WHEN** 持仓 ETF 发生经验证的现金分红
- **THEN** 除权日连续经济价格吸收现金权益并保持总回报连续，等价于分红隐含再投资；vectorbt 共享现金不得在支付日另行增加，避免重复计入。清单和报告必须声明无法精确还原支付日可用现金、税费、真实再投资份额以及这些差异可能造成的后续订单路径偏差

#### Scenario: 近似核算身份可被分析层识别
- **WHEN** 本地结果准备通过完成门禁
- **THEN** 代码身份和本地清单必须记录 `corporate_action_mode=point_in_time_total_return_approximation`、`price_basis=continuous_economic_price`、`quantity_basis=economic_units`、`cash_dividend_mode=implicit_reinvestment_on_ex_date`、`pay_date_cash_supported=false` 和 `exact_joinquant_reconciliation=false`；缺少任一声明时不得发布完成结果

#### Scenario: 聚宽策略价格模式
- **WHEN** 生成供后续聚宽功能校验使用的海龟策略代码
- **THEN** 信号行情显式使用 `fq=None` 且策略设置 `use_real_price=False`，并把聚宽在该模式下使用固定基准日前复权撮合价记录为平台限制，不宣称撮合使用未复权实际价

#### Scenario: 导出传输与清理
- **WHEN** 聚宽端临时文件已传输到本地
- **THEN** 系统先验证本地字节 SHA256 与远端回读一致，再删除远端临时文件；无法确认删除时本次运行输出 `failed`

### Requirement: 海龟规则的确定性验证
海龟项目 MUST（必须）以项目代码和固定夹具验证已确认基线：此前 55 日最高价收盘突破入场、20 日 N 值、0.5% 初始风险单位、固定 0.5N 加仓档位、只上移的 2N 共同止损、此前 20 日最低价收盘跌破退出，以及资金、单 ETF、资产组、组合风险和目标波动率约束。ETF 池在进入策略前已经完成流动性初筛；海龟项目不得读取成交额来决定标的资格、订单数量或是否接受入场、加仓、退出和止损。

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
- **WHEN** 候选订单超过任一项目风险、资金或目标波动率上限，或裁剪后不足一个交易整手
- **THEN** 项目按确认规则缩小或跳过订单，不使用融资、透支或跨 ETF 锁定利润补足预算

#### Scenario: 策略层不存在流动性规则
- **WHEN** ETF 已由上游候选池纳入海龟项目且产生入场、加仓、退出或止损订单
- **THEN** 项目不得设置最低成交额、成交额占比、订单参与率或其他流动性门槛，不得因成交额缺失或下降缩小、拒绝或延迟订单；流动性是否适合入池不属于海龟策略执行职责

#### Scenario: 被动超限不冻结其他证券
- **WHEN** 已有持仓因价格上涨被动超过单 ETF 或资产组市值上限
- **THEN** 项目禁止继续扩大同一超限证券或超限资产组，但允许满足资金、风险和波动率约束的其他证券入场或加仓；不得仅因既有超限冻结整个组合，也不得为恢复市值上限新增非海龟退出规则

#### Scenario: 持仓风险输入缺失
- **WHEN** 任一持仓 ETF 缺少可用价格或无法形成可靠协方差输入
- **THEN** 项目停止所有新增风险，不以零值、陈旧协方差或虚构成交继续；其他可交易 ETF 仍允许退出和强制减仓

### Requirement: vectorbt 官方回调式本地执行内核
海龟项目 SHALL（必须）使用 vectorbt（向量化回测框架）官方 `Portfolio.from_order_func()`（自定义订单函数）作为唯一生产本地模拟入口，以一个共享现金组运行全部 ETF，并通过 Numba（即时编译）回调实现项目专属的海龟状态、退出、强制减仓、A1（同日共享预算分配）、风险门槛、市场可交易性和审计原因。项目不得另建独立 Numba 回测引擎，也不得让旧 Python（编程语言）逐日循环与 vectorbt 同时成为生产裁决源。

#### Scenario: 使用官方组合与回调生命周期
- **WHEN** 海龟项目启动一个基线或挑战场景
- **THEN** 项目通过 `Portfolio.from_order_func()`、`cash_sharing=True`（共享现金）、单一 ETF 组合组以及 `pre_sim_func_nb`、`pre_segment_func_nb`、`order_func_nb`、`post_order_func_nb` 官方回调运行，不直接依赖 vectorbt 私有接口

#### Scenario: 只使用当时可获得的信息
- **WHEN** T 日收盘形成突破、退出、N 值、协方差或风险输入
- **THEN** 项目把这些只读数组显式错位到 T+1 日执行行，回调不得读取 T+1 日收盘或任何尚未完成样本；订单价格使用 T+1 日开盘及当日已经公开的停牌、涨跌停和成本输入

#### Scenario: 卖出成交后再分配买入预算
- **WHEN** 同一交易日同时存在退出、强制风险减仓和多个买入候选
- **THEN** `pre_segment_func_nb` 先分类候选并设置“退出 → 风险减仓 → 买入”的调用顺序；卖单处理完成后，`order_func_nb` 基于实际卖出结果更新后的现金和持仓只计算一次 A1，再为每只 ETF 返回最多一个最终订单

#### Scenario: 成交结果驱动海龟状态
- **WHEN** vectorbt 处理一个买入、卖出、拒绝或无订单结果
- **THEN** `post_order_func_nb` 只根据实际成交数量和价格更新批次、理论档位、共同止损、现金关联审计和持仓状态；停牌、涨跌停、拒单或未成交不得推进海龟状态

#### Scenario: 普通订单函数足以表达现行规则
- **WHEN** 现行规则保证同一 ETF 同一交易日的退出会取消其买入，且最终最多产生一笔订单
- **THEN** 项目使用普通 `from_order_func()`，不启用 `flexible=True`（灵活多订单）；若未来需要同一 ETF 同一日多笔订单，必须先修改规格

#### Scenario: 按已确认规则验证执行语义
- **WHEN** 使用依据本规格编写、覆盖入场、加仓、退出、强制减仓、A1、跳空、停牌、涨跌停、拒单和数据故障的固定小型合成夹具运行 vectorbt
- **THEN** vectorbt 路径的逐日订单、实际成交、现金、持仓、批次、共同止损、风险原因和最终摘要满足夹具明确断言；验收不执行旧 `process_day` 完整路径或新旧双跑

#### Scenario: 完成后彻底删除旧执行方案
- **WHEN** vectorbt 规则夹具、本地四类物理事实与六类逻辑视图适配和项目公开入口全部通过
- **THEN** 项目删除旧 `execution.py`、`state.py`、`signals.py`、`risk.py`、`allocation.py` 及只服务这些模块的测试和公开导出；海龟状态、信号、A1 与风险规则只存在于新的输入和 Numba 回调实现中，不得提供兼容模块、导入别名、双引擎开关、旧入口或运行时回退

#### Scenario: 清理后不存在隐藏旧引用
- **WHEN** 对生产代码、测试、项目配置、构建映射和公开入口执行迁移验收
- **THEN** 全仓扫描不再发现 `process_day`、旧 `_simulate` 或上述已删除模块的导入和调用，完整海龟 E2E（端到端）仍从唯一 vectorbt 入口通过；历史性能诊断文档可以保留为迁移原因，但不得形成可执行旧方案

#### Scenario: vectorbt 记录适配现有分析数据包
- **WHEN** vectorbt 完成一个候选或稳健性路径场景
- **THEN** 项目在 `<run_id>/backtests/<local_backtest_id>/` 下按聚宽现有内部目录生成 `manifest.json`、`code.py`、`params.json`、`params_versions/`、`performance.json`、`data/`，并把官方订单、交易、持仓、现金和权益记录转换为同名同义的 `results`、`balances`、`positions`、`orders` 四类共同执行事实；本地清单把来源未提供的 `risk`、`period_risks` 标记为由独立策略分析计算，并把海龟专属 `attribution_log-<sha256>.parquet` 声明为必需扩展；通用分析层不得直接消费 vectorbt 对象

#### Scenario: 性能验收区分编译与执行
- **WHEN** 对同一代表性行情和配置运行性能复核
- **THEN** 项目在同一暂存区分别记录包含首次 JIT 编译的冷启动单次回测耗时和预热后的单次回测耗时；每次从准备后输入进入 vectorbt 开始，到交易执行、四类共同事实、海龟必需归因日志及其结构/摘要/勾稽校验完成时停止。两者均不得超过 180 秒且规范化结果摘要必须一致；停止计时后先删除预热副本和可丢弃暂存并确认清理，再把清理结果写入 `performance.json`、生成最终清单并校验全部摘要。最后只原子发布已整理的权威结果目录，发布后不得依赖写入或清理；冻结基线、每个挑战和后续每个显式场景都独立应用该门槛，不运行旧完整流程作为比较基准，策略分析耗时另行记录且不得计入回测耗时

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

### Requirement: 海龟归因日志是完整分析的必需扩展
海龟项目 SHALL（必须）为每个单场景结果生成 `data/attribution_log-<sha256>.parquet`，并在本地清单以 `required=true`、`status=complete`、路径、行数、时间范围和 SHA256（文件摘要）登记。该要求只属于 `strategy-003` 项目契约，不提升为所有策略的统一核心表。

#### Scenario: 归因日志字段和主键固定
- **WHEN** 项目记录信号、风险门禁、订单或状态变化
- **THEN** 日志至少包含 `time:string`、`event_id:string`、`scope:string`、`security:string nullable`、`event_type:string`、`reason_code:string`、`requested_amount:double nullable`、`executed_amount:double nullable`、`reference_price:double nullable`、`risk_before:double nullable`、`risk_after:double nullable`、`details_json:string`；`event_id` 是确定性唯一主键，`details_json` 必须是可解析 JSON（结构化数据）

#### Scenario: 原因码版本和覆盖范围固定
- **WHEN** 写入归因日志
- **THEN** 清单记录 `reason_code_version=turtle-etf-attribution/2`，原因码至少覆盖 `signal_entry`、`signal_add`、`signal_exit`、`protective_stop`、`forced_risk_reduction`、`risk_gate_block`、`untradeable`、`order_rejected`、`state_update` 和 `corporate_action_applied`；过滤掉的候选、风险暂停、不可交易、拒单、成交、共同止损变化及公司行动应用均须留下对应事件

#### Scenario: 缺失归因日志阻止海龟结果完成
- **WHEN** 归因日志缺失、主键重复、原因码未知、摘要不匹配或无法覆盖实际订单与风险状态变化
- **THEN** 该海龟单场景不得通过 `gate.status=pass`，也不得进入完整策略分析

### Requirement: 向独立策略分析能力提供标准事实
海龟项目 SHALL（必须）只负责基于实际交易路径生成聚宽口径兼容的本地 `manifest.json` 与 `results`、`balances`、`positions`、`orders` 四类共同执行事实，并分别执行冻结基线和六个挑战；本地清单 SHALL（必须）显式声明 `risk` 与 `period_risks` 由来源未提供。完整往返交易、权益、基准和事件分析视图由独立读取层按共同事实查询派生；绩效、Alpha（超额收益）、Beta（市场暴露）、归因、稳健性、压力、证据矩阵、报告和推荐 SHALL（必须）由独立 `strategy-analysis` 能力计算；海龟项目不得直接导入或复制这些算法。

#### Scenario: 交付基线或挑战事实
- **WHEN** 一个海龟基线或挑战场景执行完成
- **THEN** 项目只输出通过标准契约校验的数据包、执行身份、场景身份和摘要，独立策略分析能力可以在不导入海龟代码和 vectorbt 的情况下消费这些事实

#### Scenario: 项目不执行派生稳健性分析
- **WHEN** 独立策略分析需要时期、资产删除、成本/延迟、抽样、压力、冲击或 CVaR 结果
- **THEN** 主 agent 不再调用本地研究 Skill；这些结果由通用分析从七个来源数据包派生，Skill 和项目不计算场景评分、证据矩阵或报告结论

#### Scenario: 禁止项目内分析耦合
- **WHEN** 扫描 `strategy-003/research/turtle_etf/` 的生产导入和输出职责
- **THEN** 项目不导入通用绩效、基准、归因、稳健性、压力、证据矩阵或报告实现，也不生成只能由这些算法解释的最终分析结果

### Requirement: 独立分析覆盖完整稳健性矩阵
本变更 SHALL（必须）在本地研究 Skill 之外，以策略自有 `analysis-plan.json` 为唯一场景定义输入，经通用 Schema 校验后生成版本化 `analysis-scenarios.json`，完整覆盖参数、时期、滚动窗口、资产删除、成本与延迟执行、区块抽样、历史压力、持仓冲击和 CVaR（条件风险价值）。七个基础场景 SHALL（必须）引用各自独立结果，其余场景 SHALL（必须）从基线事实确定性计算并记录方法限制。

#### Scenario: 参数邻域引用基础调用
- **WHEN** 主 agent 已分别完成冻结基线和六个预设挑战
- **THEN** 参数邻域直接引用对应的七个独立来源结果，不重复塞入一次 Skill 调用，也不按结果删选挑战

#### Scenario: 七个基础场景独立重跑
- **WHEN** 分析冻结基线和六个预设挑战
- **THEN** 主 agent 为七个场景各生成一份明确配置并分别调用单场景 Skill 一次；Skill 不知道七个场景的组合，也不继续执行其他稳健性场景

#### Scenario: 其余稳健性从基线事实计算
- **WHEN** 分析固定时期、三年季度滚动窗口、逐只删除 ETF、逐组删除资产组或成本与延迟执行敏感性
- **THEN** 独立分析只读取基线收益、持仓、订单和事件事实，不额外调用本地研究 Skill；时期结果明确为既有路径切片，资产删除明确不重新分配资金，成本与延迟明确为一阶订单级敏感性，不得表述为独立交易路径回测

#### Scenario: 非路径场景从事实计算
- **WHEN** 分析5/20/60日区块抽样各10,000条路径、五个历史压力窗口、四个持仓冲击和95%/99%及5日 CVaR
- **THEN** 独立分析只使用基线收益、持仓和事件视图按固定公式、随机种子和门槛计算，不调用 Vibe 或本地研究 Skill 伪造新交易路径

#### Scenario: 场景矩阵完整性门禁
- **WHEN** 独立分析准备生成报告
- **THEN** `analysis-plan.json`、`analysis-scenarios.json`、`source-results.json` 与证据矩阵必须逐项给出计划摘要、期望数量、实际数量、配置/来源摘要、计算方法和门槛状态；七个来源缺失、重复、摘要不符或性能证据不完整时以 `evidence_insufficient` 停止

### Requirement: 主代理复数调用与独立确定性分析验证
海龟项目 SHALL（必须）一次只执行一个明确场景。本地研究 Skill SHALL NOT（不得）知道冻结基线加六个挑战这一组合，也不得在内部循环。主 agent（代理） SHALL（必须）从策略自有 `analysis-plan.json` 读取冻结基线和六个预设挑战，分别调用本地研究 Skill 七次，再在 Skill 外建立独立分析身份并由通用确定性分析完成本地结果分析。Vibe-Trading（AI 研究助理）群体分析 SHALL NOT（不得）使用。

#### Scenario: 单次调用只生成一个结果
- **WHEN** 主 agent 调用本地研究 Skill 并传入一个基线或挑战场景
- **THEN** Skill 只生成一份本地回测目录及单场景运行证据，以 `next_action=return_to_caller` 停止，不读取其他候选、不生成聚合清单、不启动下一场景

#### Scenario: 主代理收集七个基础结果
- **WHEN** 需要执行冻结基线和六个预设挑战
- **THEN** 主 agent 先在独立 `preparation_id` 下按已校验 `analysis-plan.json` 生成七份配置并按顺序调用 Skill 七次，再显式登记七组 `scenario_id -> run_id`；分析入口拒绝目录扫描和来源猜测，验证七个独立 `run_id`、同一代码摘要、同一 `snapshot_id`、同一执行后端、各自场景摘要、结果摘要和性能证据，由准备身份与全部来源摘要派生不可变 `analysis_id`，在其下生成 `analysis-scenarios.json`、`preparation.json` 与 `source-results.json`；该聚合不写回任何单场景运行，也不得覆盖同计划下的旧分析

#### Scenario: Skill 保持策略数量无关
- **WHEN** 增加、删除或重排待研究场景
- **THEN** 本地研究 Skill 的输入、执行步骤、输出和停止状态保持不变；只有主 agent 的调用次数和独立分析来源集合变化

#### Scenario: 安全使用 Vibe-Trading 能力
- **WHEN** 主 agent 已收集基础结果、完整稳健性场景结果、双基准集和确定性分析证据
- **THEN** 本变更创建单独 `analysis_id`，由确定性分析输出 `local-strategy-analysis-report.md`、`vibe-evidence.json` 和 `recommendation.json`；Vibe 研究目标和证据登记只作审计编排，只允许额外调用无已知缺陷的单体公开分析入口，并记录目标标识、工具证据、输入摘要和 `analysis_seconds`

#### Scenario: Vibe 能力不改变研究事实
- **WHEN** Vibe-Trading 单体能力可用、不可用或证据不足
- **THEN** 它不得修改任何来源结果、候选配置、本地运行状态、海龟参数、数值事实或最终门槛；报告必须声明 Vibe 是否实际产生单体分析，并声明不是聚宽正式回测、模拟交易或最终验收结论

#### Scenario: 已知缺陷能力不得降级使用
- **WHEN** 当前 Vibe-Trading 版本仍存在群体分析缺陷或组合优化器前视偏差
- **THEN** 独立分析跳过受影响能力并记录原因，不得通过换预设、重试群体分析或加载方法文档冒充结果，也不阻塞确定性报告生成

#### Scenario: 误调用群体分析必须隔离
- **WHEN** 运行记录中存在 `run_swarm`（运行群体分析）调用
- **THEN** `vibe-evidence.json` 将其标记为边界违规、无效证据并排除出结论；报告与推荐只使用确定性分析，不等待、不引用也不转述该群体运行

#### Scenario: 人工确认独立留痕
- **WHEN** 独立确定性分析报告和 `recommendation.json` 已固化
- **THEN** 人工确认写入分析目录之外的追加式 `human-decision.json`，引用 `analysis_id`、`source-results.json`、`deterministic-analysis.json`、报告和推荐摘要；确认前不得启动聚宽正式回测、替换基线、修改参数、冻结策略或启动模拟交易

#### Scenario: 单场景与变更完成门禁分离
- **WHEN** 判断单次 Skill 调用是否完成或判断本变更是否可以交付
- **THEN** 单次调用只以一份兼容结果及运行证据为门禁；本变更完成还必须由主 agent 收集全部要求场景，并提供确定性分析、报告、推荐和 Vibe 安全边界证据，二者状态不得混写
