# Brainstorm Summary

- Change: `build-turtle-etf-local-research-workflow`
- Date: `2026-07-15`
- Status: 已按确认方案完成实施与全量验证，等待独立实现审查和人工确认研究结论

## 当前有效架构

- 整体架构固定为三个独立 Skill（技能）：本地研究流程、JoinQuant（聚宽）回测流程、策略分析。
- 本变更主要实现本地研究流程、`strategy-003` vectorbt（向量化回测框架）执行路径和统一分析读取能力。
- 聚宽回测 Skill 与策略分析 Skill 另立变更；聚宽正式复核、模拟交易、规则冻结和实盘不在本变更范围。
- 本变更在本地研究 Skill 外用通用确定性分析完成一次独立验收，但不创建或修改策略分析 Skill。Vibe-Trading（氛围量化）群体分析存在已知缺陷，禁止作为证据或结论；当前没有安全单体分析入口时只记录证据不足。

## 本地研究 Skill 边界

- `run-local-quant-research` 每次只接受一个策略项目、一个 `snapshot_id`（快照标识）和一个场景。
- 每次调用只产出一份本地兼容结果，并以 `next_action=return_to_caller` 停止。
- Skill 不读取 `analysis-plan.json`，不接收候选数组，不知道七个基础场景，不内部循环，不生成聚合清单，也不执行绩效、归因、稳健性、报告或推荐。
- 冻结基线和六个挑战由主 agent（代理）从策略自有分析计划读取后分别调用 Skill，合计恰好七次；其他稳健性分析只从基线已有事实确定性计算，不再调用 Skill。

## 机器可读分析计划

- `joinquant/strategies/strategy-003/research/analysis-plan.json` 是海龟策略自有、版本化的机器可读场景定义。
- 该计划固定一个冻结基线、六个单项挑战、三个固定时期、三年季度滚动规则、11只 ETF（交易型开放式指数基金）删除集合、6个资产组删除集合、3个成本与延迟场景，以及区块抽样、历史压力、持仓冲击和 CVaR（条件风险价值）的定义、期望数量、固定随机种子和门槛。
- 通用 `quant_analysis`（量化分析）只按 `analysis-plan.schema.json` 校验和展开通用场景；不得解析 Markdown（文档标记语言）、导入海龟模块或硬编码海龟资产、参数、分组、数量和门槛。
- 主 agent 每次只把一个基础场景交给本地研究 Skill；计划摘要、七份来源摘要和其他确定性稳健性场景只在独立 `analysis_id` 下聚合。

## 行情与基准数据

- 共享行情中心使用 `.local/market-data/` 下不可变 Parquet（列式文件）批次和快照引用；DuckDB（嵌入式分析数据库）只使用内存查询。
- 聚宽 CSV（逗号分隔文件）只作传输暂存，验证转换后删除；不保存持久 DuckDB 副本。
- 海龟行情固定使用 `fq=None` 的未复权价格并保留 `factor`（复权因子），策略信号不生成或使用复权价。
- 跨来源比较只使用独立基准集中的沪深300人民币总回报和纳斯达克100人民币总回报。
- 聚宽 `results.benchmark_returns` 只作平台单基准累计收益参考；本地同名字段保留为全空 `double` 并明确声明 `missing_at_source/independent_benchmark_set`，禁止填零或冒充双基准。

## 标准分析数据包

- 聚宽现有 `manifest.json` 和六类核心 Parquet 是标准物理基准，现有回测目录、归档流程和文件保持 0 改动。
- 本地结果使用独立 `schema_version=local-backtest/1`、`object.kind=local_backtest`、`source.kind=local_vectorbt` 和 `authority=local_research`。
- 每个本地结果位于 `.local/quant-research/<strategy_id>/<run_id>/backtests/<local_backtest_id>/`，包含 `manifest.json`、`code.py`、`params.json`、`params_versions/`、`performance.json` 和 `data/`。
- 本地物理事实只有 `results`、`balances`、`positions`、`orders`；`risk` 与 `period_risks` 明确声明来源未提供。统一读取器为两种来源建立六类逻辑视图。
- 通用契约不要求所有策略共享归因字段；`strategy-003` 项目契约强制生成 `attribution_log-<sha256>.parquet`，固定字段、确定性 `event_id` 唯一主键和 `turtle-etf-attribution/1` 原因码。

## vectorbt 唯一执行路径

- `strategy-003` 使用 vectorbt 1.1.0 官方 `Portfolio.from_order_func()`，11只 ETF 使用一个 `cash_sharing=True`（共享现金）组合组。
- 海龟状态、退出、强制减仓、A1（同日共享预算分配）、组合风险、停牌和涨跌停判断由项目专属 Numba（即时编译）回调实现。
- 官方回调使用 `pre_sim_func_nb`、`pre_segment_func_nb`、`order_func_nb`、`post_order_func_nb`；T日信息显式错位到T+1执行，卖出实际成交后才计算一次 A1，状态只按实际成交更新。
- 不启用 `flexible=True`（灵活多订单），不调用 vectorbt 私有模拟函数，不另建独立 Numba 回测引擎。
- 新路径通过后删除旧 `execution.py`、`state.py`、`signals.py`、`risk.py`、`allocation.py`、`reporting.py`、旧专用测试和公开导出；不保留兼容层、双引擎、回退或旧完整对照。

## 性能与原子完成门禁

- 每个单场景调用在同一暂存区执行冷启动和预热回测；两者均不得超过180秒，规范化结果摘要必须一致。
- 计时从已准备输入进入 vectorbt 开始，到交易执行、四类共同事实、海龟必需归因日志及其结构/摘要/勾稽校验完成时停止；停止计时后才写入并校验 `performance.json` 与最终清单，二者属于完成门禁但不计入冷/热耗时。
- 两次执行、摘要一致性和性能门槛通过后，先删除预热副本与可丢弃暂存并确认清理，再把清理结果写入 `performance.json`、生成最终清单并校验全部摘要，最后只原子发布已整理的一份权威结果；发布后不再依赖写入或清理。
- 任一门禁失败只保留 attempt（尝试）证据，不得先发布 `complete` 目录；七次调用总耗时和 Vibe 分析耗时不能替代单场景门槛。

## 独立分析验收

- 分析准备先在 `.local/strategy-analysis-preparations/<preparation_id>/` 固化计划、基准、运行模板和七份单场景配置；七次调用完成后，主 agent 显式登记七组 `scenario_id -> run_id`，禁止扫描历史目录猜测来源。
- 分析入口必须验证七个 `run_id` 唯一且共享快照、代码身份和执行后端，再由准备身份及全部来源摘要派生不可变 `analysis_id`；同计划下的新一批结果不得覆盖旧分析证据。
- 主 agent 在 `.local/strategy-analysis/<analysis_id>/` 保存准备证据、含七个明确来源引用的一份 `source-results.json`、`analysis-scenarios.json`、确定性分析、Parquet（列式文件）证据矩阵、完整报告、Vibe 边界证据和推荐。
- 七个基础场景逐次调用单场景 Skill；固定时期、滚动窗口、资产删除、成本/延迟、区块抽样、历史压力、持仓冲击和 CVaR 从基线来源事实确定性计算，不额外重跑策略。
- 确定性分析覆盖收益、回撤、仓位与风险、Alpha/Beta（超额收益/市场暴露）、多维归因、完整稳健性、挑战比较、反对证据和不确定性。
- Vibe 的研究目标与方法文档只作审计记录，不算实际分析。当前误调用的 `run_swarm`（运行群体分析）已标记无效并从全部结论排除；报告和推荐完全来自确定性分析。
- 最终输出 `next_action=human_confirmation_required`，人工确认前不得启动聚宽正式回测、替换基线、修改参数、冻结策略或启动模拟交易。

## 验证要求

- 全部 Python（编程语言）命令使用项目 `.venv`（虚拟环境）。
- 使用 TDD（测试驱动开发）覆盖 Schema 选择、聚宽零改动读取、本地结果字段、分析计划展开、海龟归因、vectorbt 官方回调、前视偏差、性能原子门禁和临时产物清理。
- 从 Skill 用户入口只跑通一次单场景完整 E2E（端到端）；主 agent 复数调用和独立分析使用单独集成 E2E，不能把七个场景耦合进 Skill。
- 不执行旧完整方案性能对照；验收直接使用已确认规则和固定小型合成夹具。
