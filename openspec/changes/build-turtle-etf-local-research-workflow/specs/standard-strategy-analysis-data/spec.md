## ADDED Requirements

### Requirement: 以聚宽现有回测归档作为标准物理基准
系统 SHALL（必须）直接接受 `joinquant/strategies/<strategy_id>/backtests/<backtest_id>/` 现有目录、原 `manifest.json`、`params.json`、`code.py` 和 `data/` 文件。聚宽回测流程、归档流程和既有回测目录 SHALL NOT（不得）新增、改名、复制、重写或转换任何文件。

#### Scenario: 聚宽归档零改动直读
- **WHEN** 现有聚宽回测 `manifest.json` 的 `schema_version=1`、`object.kind=backtest`、`object.status=done` 且 `gate.status=pass`
- **THEN** 读取器只按现有聚宽 `manifest.schema.json` 验证并直接建立分析视图，不生成 `analysis-data-manifest.json`、适配目录、八表副本或回写字段

#### Scenario: 聚宽清单仍是唯一权威入口
- **WHEN** 读取器处理聚宽回测结果
- **THEN** 它只按原清单定位文件、摘要、行数、合法空表、数据集状态和来源身份，不扫描“看起来最新”的文件；读取前后聚宽目录摘要必须一致

### Requirement: 本地回测使用独立且可执行的清单 Schema
系统 SHALL（必须）提供 `local-backtest-manifest.schema.json`。本地结果从 `.local/quant-research/<strategy_id>/<run_id>/backtests/<local_backtest_id>/` 向内尽量镜像聚宽现有回测目录，但 SHALL（必须）使用 `schema_version=local-backtest/1`、`object.kind=local_backtest`、`source.kind=local_vectorbt` 和 `authority=local_research` 明确来源，不能声称符合聚宽远端归档 Schema。

#### Scenario: 本地清单必需证据
- **WHEN** 本地 vectorbt（向量化回测框架）完成一个场景
- **THEN** 本地 Schema 顶层 `additionalProperties=false`，必需字段为 `schema_version`、`object`、`source`、`authority`、`run`、`code`、`params`、`datasets`、`performance`、`gate`；其中必须记录 `local_id`、`status`、`run_id`、`scenario_id`、`snapshot_id`、引擎/适配器版本、公司行动核算模式及精度边界、代码路径/字节数/SHA256、当前参数路径/版本路径/字节数/SHA256、数据集状态/文件摘要/行数/时间范围/空表、性能证据路径/字节数/SHA256，以及 `gate.status=pass|fail` 与 `exceptions`；`code.py`、`params.json`、`params_versions/<sha256>.json`、`performance.json` 与 `data/` 的位置沿用同一回测根目录

#### Scenario: 本地公司行动近似口径显式可见
- **WHEN** 本地 vectorbt 不能原生同步拆分后的真实份额和派息日现金，而改用连续总回报经济价格
- **THEN** `source.accounting` 必须记录 `corporate_action_mode=point_in_time_total_return_approximation`、`continuity_factor_basis=raw_previous_close_over_current_pre_close`、`corporate_action_metadata_timing=audit_only_may_be_retrospective`、`price_basis=continuous_economic_price`、`quantity_basis=economic_units`、`cash_dividend_mode=implicit_reinvestment_on_ex_date`、`pay_date_cash_supported=false`、`exact_joinquant_reconciliation=false`、公司行动数据摘要和口径版本；逐事件归因还必须记录 `evidence_timing=point_in_time|retrospective_reconciliation`。读取器必须原样暴露这些限制，未知模式或缺少必需声明时拒绝来源

#### Scenario: 性能证据可独立复核
- **WHEN** 本地结果准备通过 `gate.status=pass`
- **THEN** `performance.json` 必须记录环境与依赖摘要、准备后输入摘要、代码/参数/场景摘要、`cold_seconds`、`warm_seconds`、两次规范化结果摘要、摘要一致性结论、性能上限和暂存清理结果；计时统一从准备后输入进入项目执行后端开始，到项目声明的执行事实和必需扩展完成结构/摘要/勾稽校验时停止。停止计时后先删除预热副本和可丢弃暂存并确认清理，再把清理结果写入 `performance.json`、生成最终清单并校验全部摘要；二者属于完成门禁但不计入冷/热耗时。最后只原子发布已整理的权威结果目录，发布后不得依赖写入或清理；任一门禁失败不得发布完成目录

#### Scenario: 本地不伪造聚宽专属证据
- **WHEN** 本地来源没有聚宽详情页、远端原始响应、围栏、官方摘要或官方风险结果
- **THEN** 本地清单和目录不得包含伪造的聚宽 URL、`research_response`、`research_lineage`、`collection_fence`、`official_summary`、`raw/`、`risk.parquet` 或 `period_risks.parquet`

#### Scenario: 读取器严格选择 Schema
- **WHEN** 读取器打开来源清单
- **THEN** 整数 `schema_version=1` 只使用聚宽 Schema，字符串 `schema_version=local-backtest/1` 只使用本地 Schema；未知版本、混合身份、越权字段或校验失败直接拒绝，不得尝试回退另一契约

### Requirement: 六类逻辑模型与两种物理形态明确分离
统一分析模型 SHALL（必须）沿用聚宽现有 `results`、`balances`、`positions`、`orders`、`risk`、`period_risks` 六类名称。聚宽来源物理提供六类数据；本地来源只物理提供四类共同执行事实，并在清单中声明两类官方风险参考缺失。读取器 SHALL（必须）为两种来源建立相同的六类逻辑视图。

#### Scenario: 本地只落四类执行事实
- **WHEN** 本地结果通过门禁
- **THEN** `results`、`balances`、`positions`、`orders` 必须为 `required=true`、`status=complete`；`risk` 与 `period_risks` 必须为 `required=false`、`status=missing_at_source`、`reason=computed_by_strategy_analysis`，本地研究不得计算 Alpha/Beta（超额收益/市场暴露）、Sharpe（夏普比率）、回撤或分期风险来填满六类物理文件

#### Scenario: 收益与权益沿用聚宽字段
- **WHEN** 分析策略收益、权益、现金和仓位占用
- **THEN** 使用 `results.time`、`results.returns`、`balances.time`、`total_value`、`net_value`、`cash` 和 `aval_cash`，不得固化第二份 `returns.parquet` 或 `equity.parquet`；本地来源存在研究级公司行动近似时，分析必须把收益和权益解释为连续经济口径，把现金、仓位和订单解释为近似路径，不能与聚宽逐日账户做精确差异归因

#### Scenario: 本地 results 保留聚宽单基准字段但不伪造值
- **WHEN** 本地适配器生成 `data/results.parquet`
- **THEN** 文件固定包含 `time:string`、`returns:double`、`benchmark_returns:double nullable`；`returns` 表示从初始资金起算的累计净收益，`benchmark_returns` 全列为空值且物理类型仍为 `double`，本地清单记录 `source_benchmark_returns.status=missing_at_source`、`reason=independent_benchmark_set` 和空值行数；不得填零或任选一个独立基准冒充聚宽单基准

#### Scenario: 累计收益只在查询期转换为单日收益
- **WHEN** 统一分析需要把来源策略收益与双基准的单日收益对齐
- **THEN** 读取器先把 `results.time` 规范化为 Asia/Shanghai（亚洲/上海）交易日，再按 `(1 + cumulative_return_t) / (1 + cumulative_return_t-1) - 1` 派生 `daily_returns`；首个样本只在累计收益为零时取单日收益零，否则标记缺少前值并从比较样本排除。分析不得把累计 `returns` 直接与基准单日 `returns` 比较

#### Scenario: 交易与持仓沿用聚宽字段
- **WHEN** 分析持仓、成交、费用、滑点或完整往返交易
- **THEN** 使用 `positions` 与 `orders` 的现有字段和时间语义；完整往返交易只作为查询期派生视图，不要求来源新增 `trades.parquet`

#### Scenario: 官方风险只作来源参考
- **WHEN** 分析聚宽回测风险结果
- **THEN** 读取器暴露原 `risk` 与 `period_risks` 作为 `source_risk` 官方参考；策略分析仍从共同执行事实复算跨来源可比较指标，不覆盖或改写聚宽官方结果

#### Scenario: 合法空持仓和空订单
- **WHEN** 清单把 `positions` 或 `orders` 标记为 `status=complete`、`rows=0`、`verified_empty=true` 且没有对应 Parquet（列式文件）
- **THEN** 读取器按对应来源 Schema 建立固定字段空内存视图，不补写空文件

#### Scenario: 聚宽合法缺失归因例外保持兼容
- **WHEN** 既有聚宽回测 `gate.status=pass` 且唯一例外为 `attribution_log:missing_at_source`
- **THEN** 读取器必须把该例外视为合法的可选归因缺失并零改动打开来源；其他未知例外仍须拒绝，不能把任意门禁例外降级放行

#### Scenario: 兼容已观察物理类型差异
- **WHEN** 既有聚宽归档只存在列顺序、`cancel_time` 空类型/字符串或风险数值整数/浮点差异
- **THEN** 读取器只在 DuckDB（嵌入式分析数据库）内存视图按字段名和兼容类型规范化，不重写源 Parquet 或摘要

### Requirement: 双基准使用独立且不可变的分析输入契约
系统 SHALL（必须）在 `.local/market-data/benchmark-sets/<benchmark_set_id>/` 保存 `manifest.json` 与 `benchmark-returns.parquet`，且只包含 `CSI300_CNY_TOTAL_RETURN`（沪深300人民币总回报）和 `NASDAQ100_CNY_TOTAL_RETURN`（纳斯达克100人民币总回报）两个基准。基准集不属于来源回测目录，不要求聚宽归档改动。

#### Scenario: 基准身份与口径完整
- **WHEN** 创建分析基准集
- **THEN** 清单逐项记录 `benchmark_id`、人民币币种、总回报定义、美元兑人民币处理公式、实际日期范围、来源标识、底层快照、生成版本、行数和文件 SHA256（文件摘要）；Parquet 固定包含 `time`、`benchmark_id`、`returns`，`time` 是 Asia/Shanghai（亚洲/上海）交易日，`returns` 是小数形式的单日人民币总回报，唯一键为 `(time, benchmark_id)`；纳斯达克100人民币总回报按 `(1 + USD指数总回报) × (1 + USD/CNY变动) - 1` 计算

#### Scenario: 基准来源必须真实验证
- **WHEN** 实施者准备导入或派生两个基准
- **THEN** 必须先对配置来源做真实最小可行性验证并保存证据；来源身份、总回报、汇率或日期覆盖不能证明时以 `evidence_insufficient` 停止，不得使用 ETF 代理、零收益补齐、前向/后向填充或未声明降级；策略和两个基准只在共同有效交易日计算可比较指标，并在报告披露被排除日期与样本数

#### Scenario: 聚宽单基准只作官方参考
- **WHEN** 聚宽 `results` 包含单列 `benchmark_returns`
- **THEN** 读取器把其累计收益序列暴露为 `source_benchmark_returns`；本地全空同名列则暴露为带 `missing_at_source` 状态的同一参考视图。除非来源清单能证明身份与口径完全一致，否则任何来源的该列都不能代替两条跨来源分析基准

### Requirement: 归档证据与分析输入分层
现有聚宽 `official_summary`、`records`、`attribution_log`、日志、性能剖析和 `raw/` 文件 SHALL（必须）保持原归档职责，不得成为所有策略的强制核心分析表。

#### Scenario: 官方摘要不成为第二分析事实源
- **WHEN** 聚宽归档包含 `data/official-summary.csv`
- **THEN** 它只作为页面口径交叉校验证据；确定性分析使用核心 Parquet 与独立基准集，读取器不复制或用该 CSV 恢复高精度数据

#### Scenario: 通用契约不强制统一归因扩展
- **WHEN** 回测包含 `attribution_log-<sha256>.parquet`
- **THEN** 策略分析可以读取它作为扩展证据，统一契约不得要求所有策略拥有相同归因字段；具体策略可以在自己的版本化项目契约中把该扩展声明为完成分析所必需

#### Scenario: 海龟单位证据为可选扩展
- **WHEN** 海龟本地结果的归因扩展包含单位数、冻结 N、候选基础数量、实际成交价、共同止损、资产组比例、组合比例、现金比例和全量仓位再分配标记
- **THEN** 通用策略分析可以派生最高计划损失比例、最高有效 N 风险单位、组合单位预算最高利用率和全量仓位再分配次数，但不得改变 `results`、`balances`、`positions`、`orders` 四类共同事实
- **AND** 现有聚宽结果缺少该扩展时，上述海龟专用指标返回缺失或零，其他收益、风险、仓位和基准分析继续运行

### Requirement: 本地与聚宽共用同一分析读取入口
系统 SHALL（必须）在 `scripts/research/analysis_data/` 提供清单选择、Schema 校验、六类逻辑视图、内存规范化和派生查询能力。策略分析只使用该入口，不直接消费 vectorbt 对象，也不为聚宽建立转换副本。

#### Scenario: 同一算法消费两种来源
- **WHEN** 分析接收聚宽目录或本地单场景目录
- **THEN** 它通过同一读取入口计算指标；来源差异只在 Schema 校验、核算精度元数据与缺失参考视图中处理，分析算法不读取 vectorbt 对象或按执行引擎复制算法。报告必须展示来源核算精度，研究级近似不得被描述为聚宽精确账户复核

#### Scenario: 主代理聚合多个单场景结果
- **WHEN** 主 agent（代理）需要比较冻结基线、挑战或稳健性场景
- **THEN** 它按已校验计划逐次调用单场景本地研究 Skill；调用前以 `preparation_id` 绑定分析计划、基准与每场景配置，调用后以完整来源登记显式绑定所有 `scenario_id -> run_id`，逐场景校验配置、证券集合、精确快照、代码和执行后端，再由准备身份与全部来源摘要派生不可变 `analysis_id`；不得扫描历史目录猜测来源或覆盖旧分析。本地 Skill 不读取分析计划、不接收候选数组、不循环方案、不生成聚合清单

#### Scenario: 独立资产扩展使用真实单场景矩阵
- **WHEN** 资产扩展计划包含原 11 只基线、完整扩展、逐只删除、逐扩展切片删除和五个成本执行压力场景
- **THEN** 每个计划项必须登记一个独立标准结果包，全部六只候选通过时来源总数为 16
- **AND** 删除和成本执行结果必须来自真实本地回测，不得用贡献扣除或订单损益一阶调整代替

#### Scenario: 不同资产池使用可比较的精确快照
- **WHEN** 来源登记包含不同证券集合的场景
- **THEN** 分析读取每个场景自己的配置和完全匹配的快照，拒绝缺失、额外或未知证券
- **AND** 所有场景共享相同不可变批次、截止日、字段、价格口径、代码和非资产池参数；重叠证券行情及公司行动摘要必须完全一致

#### Scenario: Vibe-Trading 分析能力安全降级
- **WHEN** 主 agent 已收集所需单场景结果、双基准集和确定性分析证据
- **THEN** 系统在本地 Skill 之外由确定性算法生成绑定来源摘要的完整报告与推荐；Vibe-Trading（氛围量化）只允许调用无已知缺陷的单体公开能力，禁止群体分析。没有安全单体入口时记录 `evidence_insufficient`，不得阻塞或替代确定性报告

#### Scenario: 群体分析结果无效
- **WHEN** 误调用 Vibe `run_swarm`（运行群体分析）或收到任何群体分析结果
- **THEN** `vibe-evidence.json` 必须把该调用标记为边界违规、`valid_evidence=false` 和 `excluded_from_conclusions=true`，报告、推荐、门槛和证据矩阵不得引用其内容

#### Scenario: Vibe 传输数据不形成第二事实源
- **WHEN** 可用且安全的 Vibe 单体分析入口需要读取统一视图
- **THEN** 独立分析按明确查询、字段和日期范围物化临时 CSV（逗号分隔文件），记录查询版本和摘要，确认读取后删除；分析身份始终引用原清单、Parquet 与基准集摘要
