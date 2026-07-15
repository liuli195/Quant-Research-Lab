## 1. 保留仍有效的通用基础

- [x] 1.1 已建立真实 `strategy-003` 身份、仓库级 `run-local-quant-research` Skill（技能）、三态运行证据和不可变 `run_id`
- [x] 1.2 已把共享日线行情中心迁移为 `.local/market-data/` 下的不可变 Parquet（列式文件）批次与快照，DuckDB（嵌入式分析数据库）只使用内存查询，聚宽 CSV（逗号分隔文件）仅作传输暂存并在验证后删除
- [x] 1.3 已固化海龟 ETF（交易型开放式指数基金）的未复权口径、次日开盘、退出/强制减仓/买入顺序、A1（同日共享预算分配）和风险规则夹具；这些规则只作为 vectorbt（向量化回测框架）新实现的验收事实，不保留旧逐日执行路径

## 2. 建立 vectorbt 唯一执行内核

- [x] 2.1 先添加失败的依赖身份和兼容性测试，再在项目 `.venv`（虚拟环境）中固定经实测可用的 vectorbt、Numba（即时编译）、NumPy（数组计算）和 Pandas（数据处理）版本；记录许可边界、依赖摘要和 Build and Verify（构建与验证）影响映射，非海龟项目不得因此依赖 vectorbt
- [x] 2.2 先添加失败的数组契约和前视偏差测试，再实现日期/证券对齐、T 日信号到 T+1 开盘的显式错位、只读指标/协方差/市场状态数组和稳定数值类型，并证明 Numba 回调以 `nopython`（无 Python 模式）编译
- [x] 2.3 先添加失败的官方回调生命周期测试，再使用 `Portfolio.from_order_func()`、单一共享现金组、`pre_sim_func_nb`、`pre_segment_func_nb`、`order_func_nb` 和 `post_order_func_nb` 实现卖出优先、卖出实际成交后一次性 A1、风险门槛、每只 ETF 每日最多一单和成交后状态更新；不得启用 `flexible=True`（灵活多订单）或调用 vectorbt 私有模拟函数
- [x] 2.4 用固定小型合成夹具验证逐日订单、成交、现金、持仓、批次、共同止损、风险原因、A1、跳空、停牌、涨跌停、拒单和数据故障；预期值直接来自已确认规则，不执行旧 `process_day` 路径，不安排新旧双跑

## 3. 以聚宽现有结果建立统一读取契约

- [x] 3.1 新增 `local-backtest-manifest.schema.json`，固定 `schema_version=local-backtest/1`、`object.kind=local_backtest`、`source.kind=local_vectorbt`、`authority=local_research`、代码与参数摘要、六类数据集条目、`performance.json` 摘要、门禁和空表表达；四类共同执行事实必须 `complete`，`risk` 与 `period_risks` 必须为 `required=false`、`status=missing_at_source`、`reason=computed_by_strategy_analysis`，不得出现聚宽远端证据字段
- [x] 3.2 先添加失败的双 Schema（结构约束）选择和降级攻击测试，再实现 `scripts/research/analysis_data/`：聚宽 `schema_version=1` 只按现有 `manifest.schema.json` 零改动验证，本地 `schema_version=local-backtest/1` 只按本地 Schema 验证；未知版本、混合字段或校验失败不得回退另一契约
- [x] 3.3 以仓库现有聚宽回测目录验证六类物理数据集、合法空表、列顺序和已观察兼容类型；`gate.status=pass` 时只放行仓库既有的 `attribution_log:missing_at_source` 合法例外，其他未知例外仍拒绝。固定 `results.returns` 与 `results.benchmark_returns` 为累计收益。以本地夹具验证四类物理事实加两类缺失参考，并断言 `results` 物理包含 `time:string`、累计 `returns:double`、全空但仍为 `double` 的 `benchmark_returns`，清单将其标为 `missing_at_source/independent_benchmark_set`，不得填零或冒充双基准。读取器在查询期按累计净值比派生单日收益并规范化 Asia/Shanghai 交易日，再与双基准单日收益对齐；不得直接比较累计与单日序列，也不修改或复制来源目录
- [x] 3.4 新增共享分析基准集契约：`.local/market-data/benchmark-sets/<benchmark_set_id>/manifest.json` 与 `benchmark-returns.parquet` 必须只包含沪深300人民币总回报和纳斯达克100人民币总回报，记录 `benchmark_id`、币种、总回报口径、汇率处理、日期范围、来源、底层快照、生成版本和 SHA256（文件摘要）；实际数据源必须先做真实可行性验证，禁止 ETF 代理、零收益补齐或未声明降级

## 4. 输出对齐聚宽目录的本地回测结果并删除旧实现

- [x] 4.1 先添加失败的目录、字段、摘要和本地 Schema 测试，再在 `.local/quant-research/strategy-003/<run_id>/backtests/<local_backtest_id>/` 下生成 `manifest.json`、`code.py`、`params.json`、`params_versions/<sha256>.json`、`performance.json` 和 `data/`；物理落盘含 `results`、`balances`、`positions`、`orders` 四类共同执行事实，并按海龟项目契约强制生成 `attribution_log-<sha256>.parquet`
- [x] 4.2 固定四类共同事实的字段名称、时间含义、方向、数量、价格、费用、现金、持仓、主键和跨表勾稽；固定海龟归因日志的字段、确定性 `event_id` 唯一主键、`turtle-etf-attribution/2` 原因码版本、最小原因码集合、公司行动应用记录及清单条目，缺失或无法覆盖订单、风险状态变化或公司行动应用时阻止完成。完整往返交易、权益、双基准和可比较风险指标只作为统一读取器或独立分析的查询期视图，不生成八表副本
- [x] 4.3 把本地研究 Skill 固定为“一个场景输入、一份本地回测结果、`next_action=return_to_caller`”；拒绝 `analysis-plan.json`、候选数组、内部循环、候选/聚合清单和分析调用。由主 agent（代理）从策略自有分析计划读取冻结基线和六个挑战并分别调用七次，七个独立结果只在后续 `analysis_id` 下通过 `source-results.json` 聚合
- [x] 4.4 在新路径通过后删除旧 `execution.py`、`state.py`、`signals.py`、`risk.py`、`allocation.py`、项目 `reporting.py`、旧专用测试和公开导出；不保留兼容模块、别名、特性开关、回退、旧八表契约或流程内分析入口

## 5. 建立逐场景 180 秒性能门禁

- [x] 5.1 为冻结基线和六个预设挑战提供同一基准命令：fresh process（全新进程）执行一次冷启动，同一已编译进程执行至少一次预热；每次计时从已准备输入进入 vectorbt 开始，到交易执行、四类共同执行事实、海龟必需归因日志及其结构/摘要/跨表校验完成时停止。`performance.json` 与最终本地清单属于原子完成门禁，但明确不计入 `cold_seconds`、`warm_seconds`
- [x] 5.2 每次单场景 Skill 调用在同一暂存区保存环境摘要、输入摘要、场景摘要、代码/参数摘要、冷/热规范化结果摘要、`cold_seconds`、`warm_seconds` 和临时产物清理证据；两次结果摘要不一致、冷启动或预热超过 180 秒即失败。比较通过后先删除预热副本和可丢弃暂存并确认清理，再把清理结果写入 `performance.json`、生成最终清单并校验全部摘要，最后只把已整理的唯一权威结果目录原子发布；发布后不得再依赖写入或清理动作，失败只保留 attempt（尝试）证据
- [x] 5.3 运行公开 Skill 用户入口 E2E（端到端），验证一次调用只产生一份结果、逐场景性能证据、目录对齐、三态、原子固化、临时文件清理和 `next_action=return_to_caller`；另由主 agent 连续调用七次验证复数编排，并对现有聚宽回测目录执行零改动只读 E2E

## 6. 在本地 Skill 外完成完整策略分析验收

- [x] 6.1 新增通用 `scripts/research/quant_analysis/schemas/analysis-plan.schema.json` 和策略自有 `joinquant/strategies/strategy-003/research/analysis-plan.json`。后者以 `strategy-analysis-plan/1` 机器可读定义冻结基线、六个挑战、固定时期、季度滚动、资产/分组删除、成本/延迟、区块抽样、历史压力、持仓冲击、CVaR、期望数量、固定种子和门槛；`quant_analysis` 只校验和展开为确定性的 `analysis-scenarios.json`，不得解析 Markdown、导入海龟模块或硬编码海龟常量，本地研究 Skill 不读取该计划
- [x] 6.2 对冻结基线和六个挑战生成七份独立场景配置，由主 agent 每次调用一次本地研究 Skill，在 `.local/quant-research/strategy-003/<scenario_run_id>/backtests/<local_backtest_id>/` 生成兼容结果；独立分析只记录引用和摘要，不写回任何来源 `run_id`，七个场景都满足第5节性能门禁。其他稳健性场景只从基线事实确定性计算，不额外调用 Skill
- [x] 6.3 使用统一读取器、双基准集和现有 `quant_analysis` 算法完成收益、回撤、仓位、风险、Alpha/Beta（超额收益/市场暴露）、多维归因、参数/时期/资产/成本稳健性、区块抽样、历史压力、假设冲击、CVaR、挑战比较、反对证据和最终门槛矩阵；确定性数值不得交给 Vibe-Trading（氛围量化）自行推算
- [x] 6.4 在独立 `.local/strategy-analysis/<analysis_id>/` 固化来源基础运行、全部场景运行、基准集、分析配置、证据矩阵和摘要后，只允许调用 Vibe-Trading 无已知缺陷的单体公开分析能力；`research-goal`（研究目标）和证据登记只作审计编排，加载方法文档不算实际分析，禁止 `run_swarm`（运行群体分析）、Vibe 回测和有前视偏差风险的组合优化器。没有安全单体入口时记录 `evidence_insufficient`；误调用群体分析必须标记无效并排除出全部结论
- [x] 6.5 由确定性分析输出完整 `local-strategy-analysis-report.md`、`vibe-evidence.json`、`recommendation.json` 和 `next_action=human_confirmation_required`；报告必须给出收益、回撤、Alpha/Beta、归因、仓位与风险控制、全部稳健性及挑战结果、推荐、反对证据和不确定性，并声明 Vibe 群体分析未用于结论、不是聚宽正式回测或最终验收结论

## 7. 完成全量验证

- [x] 7.1 运行全仓扫描，确认不存在旧执行符号、旧模块导入、旧八表契约、流程内分析耦合、双引擎、兼容层、死测试或转换副本；工作区中同时存在由外部同步任务生成且不属于本变更的 `strategy-001/002` 模拟归档改动，本变更未触碰这些文件，聚宽回测 113 只读 E2E 前后 26 个文件摘要完全一致
- [x] 7.2 使用项目 `.venv` 运行覆盖主流程的全量测试、OpenSpec（开放规格）严格校验、Build and Verify 完整门禁、敏感数据扫描和独立前向验证；Skill E2E 必须只贯通一次单场景回测、兼容结果和返回调用者状态，主 agent 集成 E2E 另行连续调用七次，独立分析验收再贯通场景复数调用、双基准、完整稳健性、Vibe 安全边界审计、确定性报告和人工确认前停止状态
- [x] 7.3 逐项记录已验证与无法验证部分；只有本地 Skill、统一读取、逐场景性能、独立完整分析和临时产物清理全部通过，变更才可进入完成审查

## 8. 修复真实基线的上游数据与策略边界

- [x] 8.1 先以真实最小样例验证权威公司行动来源，以及 vectorbt（向量化回测框架）官方公开路径处理动态持仓拆分与现金分配的可行性；无法闭环时停止为 `evidence_insufficient`，不得使用私有引擎、自动再投资或静态收益修补
  - 执行结论：聚宽 `finance.FUND_DIVIDEND` 权威来源已通过真实查询验证；vectorbt 1.1.0 公开接口无法把动态拆分与现金分配同步到原生持仓、现金、权益和收益序列。该结论只否定原生精确账户核算；用户随后确认采用 `point_in_time_total_return_approximation`（时点可知总回报近似），因此继续实施 8.2、8.3、8.6、8.7，不使用私有内核、伪造订单或支付日现金补丁。
- [x] 8.2 按 TDD（测试驱动开发）完成共享公司行动双事实：RED（失败）先覆盖事件字段/主键/状态/时点校验、`market-data.parquet` 与 `corporate-actions.parquet` 双摘要进入 `batch_id` 和 `snapshot_id`、DuckDB（嵌入式分析数据库）内存回读、事件篡改与无法解释除权关闭运行；GREEN（通过）再实现版本化公司行动契约、双 Parquet 原子批次、快照双证据和 `SnapshotView` 同源只读返回，并删除导入暂存文件
  - 实证：真实聚宽导出 23,938 行未复权行情与 37 行公司行动，双摘要构成批次 `1923c902f5692d35bd84e2745620a06cb6c18666c4a4add724ce80d261d5f4e1` 和快照 `e88238cca420a8ae66b90adb6cda4dd6c38a07390a13b8ac2f471e534742e33e`；远端与本地传输临时文件均已删除。
- [x] 8.3 按 TDD 完成 `point_in_time_total_return_approximation`：RED 先覆盖 512480 1:2 拆分、现金分红、晚公布元数据保留并标记为事后核对、取消事件不能授权变化、取消日期晚于快照截止日时仍按当时有效/当前已取消但缺少取消日期时关闭导出、未知状态/知识截止日无效/没有有效事件解释的价格基准变化关闭运行、生效日停牌延后到首个复牌行情日、有效事件无价格基准变化时只审计、官方比例不参与因子计算、累计连续因子只从应用日生效、连续经济 OHLC/经济单位、`continuous_close / continuous_pre_close - 1` 协方差收益、突破/N 值/权益连续和归因记录；GREEN 再让 `prepare_simulation_inputs` 消费快照公司行动，只以应用日可见的 `上一交易日原始 close / 当日原始 pre_close` 累乘连续因子，公司行动元数据仅用于授权和审计并记录 `evidence_timing`，统一用连续经济价格执行、估值和风控，现金分红按除权日隐含再投资且不在支付日增加现金；本地清单写入完整精度和元数据时点边界，统一读取器原样暴露并拒绝未知或缺失口径
  - TDD 证据：公司行动/行情/输入/结果/读取器定向回归 89 项全部通过；真实记录中晚公布元数据保留并标记 `retrospective_reconciliation`，连续因子始终只取当日原始行情事实。
- [x] 8.4 从海龟配置、`SimulationInputs`（模拟输入）、回调、原因码和测试中彻底删除最低成交额、订单参与率、单笔成交额占比及全部流动性规则；证明成交额缺失、极低或极高不会改变任何海龟订单
  - TDD 证据：修复前极低成交额为零成交、极高成交额成交 1600 份；修复后成交额缺失、极低、极高的动作、成交数量和订单数完全一致，海龟实现、配置和测试中的旧流动性字段扫描为零。
- [x] 8.5 先添加失败测试，再把单 ETF 与资产组资金上限改为“不得恶化”：被动超限不冻结其他证券、不新增强制退出，只禁止同一证券或同一超限组继续增加；退出、止损和风险降低始终允许
  - TDD 与审查证据：价值上限只检查实际增加的证券和资产组；全面审查发现并复现同类证券/资产组风险掩码会连带冻结无关候选，修复后局部风险上限只阻止对应候选，组合总风险与目标波动率仍保持全局约束。复审结果 Critical、Important、Minor 均为 0。
- [x] 8.6 在 8.2、8.3 通过后删除本变更内受旧公司行动口径污染的本地快照、七份单场景结果和派生分析，按新双事实生成不可变快照；主 agent 从 `analysis-plan.json` 生成七份配置并复数调用单场景 Skill 七次，每次冷启动/预热均不超过 180 秒且摘要一致，再显式登记七组 `scenario_id -> run_id`，重建完整确定性收益、回撤、Alpha/Beta（超额收益/市场暴露）、归因、仓位与风险、挑战、全部稳健性、压力、CVaR（条件风险价值）、报告和推荐；不得保留兼容副本或运行新旧方案对照
  - 实证：七组场景均以显式 `scenario_id -> run_id` 登记，冷启动为 29.92–31.21 秒、预热为 5.03–5.70 秒且规范化摘要一致；确定性分析 `b76821272f792bafe2557b72988d505d3c5d0e166ddf5337fd70c23ffcd06942` 用时 8.32 秒，完整报告、推荐和 Vibe 单体审计证据已生成。基线累计收益 117.44%、年化收益 5.87%、最大回撤 -12.07%、Calmar 0.486，90 项证据为 52 通过、38 失败、0 证据不足，停止状态为 `human_confirmation_required`。
- [x] 8.7 从公开 `run-local-quant-research` Skill 用户入口完成公司行动单场景 E2E，再完成主 agent 七次复数调用与独立分析 E2E；运行公司行动/输入/引擎/清单定向测试、全量测试、OpenSpec（开放规格）严格校验、Build and Verify（构建与验证）完整门禁、全仓旧公司行动精确记账与流动性规则扫描，并确认 `.local` 暂存 CSV、预热副本和测试临时产物已清理，阻断项为零后进入完成审查
  - 实证：项目 `.venv` 全仓 `445 passed`；Build and Verify 完整门禁 11 组全部通过，`full-not-run=false`，其中本地研究单元 `166 passed`、公开入口 E2E `2 passed`；OpenSpec 当前变更及全仓严格校验通过。旧引擎/流动性生产代码扫描为零，`.local` 只保留七份新结果和一份新分析，CSV、持久 DuckDB、`.attempts`、冷/热副本及传输暂存均为零；独立规格审查与实现复审均为 `No findings`。
