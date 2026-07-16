## 1. 冻结现有行为与性能证据

- [ ] 1.1 为即时、17 ETF 扩展和 `additional_delay_days=1` 延迟场景补齐成交、费用、现金、持仓、净值、策略状态和逻辑摘要特征测试
- [ ] 1.2 在固定 `.venv` 环境采集三个冷启动新进程与五次预热的主场景基线，并记录分阶段时间、峰值内存和结果包体积
- [ ] 1.3 固化旧配置、停止状态、清单、代码身份和分析视图契约，确保后续破坏性迁移只能通过新 Interface 完成

## 2. 建立共享 contracts 与真实 Strategy Module 接缝

- [ ] 2.1 先编写失败测试，定义 Strategy Module、LedgerInput、OrderProgram、ExecutionLedger、ExecutionRun 和 ResultExtension 的最小只读 Interface
- [ ] 2.2 实现仓库内 Strategy Module 安全加载与配置校验，拒绝仓库外路径、未知 symbol、旧任意 command 和策略专属 project entry
- [ ] 2.3 增加第二个最小测试策略 Adapter，并证明共享入口无需修改即可加载两个策略

## 3. 抽取后端中立标准结果包

- [ ] 3.1 先编写失败测试，把四张核心表 Schema、公共跨表校验、逻辑摘要和清单契约从海龟适配器迁入共享结果 Module
- [ ] 3.2 实现版本化 ResultExtension 注入，让海龟归因保持策略私有而共享 writer 不导入海龟动作码
- [ ] 3.3 实现单次 Parquet 固化、回读验证、失败清理和原子发布，并证明账本视图只惰性生成和缓存一次
- [ ] 3.4 扩展 analysis_data，使新本地结果、策略扩展和既有聚宽归档通过统一视图查询且保留 backend 与公式版本

## 4. 实现策略目录自包含档案

- [ ] 4.1 先编写失败测试，定义档案清单、目录布局、必需代码/配置/数据/证据/报告和 `analysis_id` 校验
- [ ] 4.2 实现独立 `promote` 动作，只复制已完成结果字节和策略源码，不加载策略、vectorbt 或 writer
- [ ] 4.3 实现逐文件 SHA256 复核、同内容幂等复用、异内容冲突拒绝、同级暂存和原子发布
- [ ] 4.4 验证删除 `.local` 源运行后档案仍可查询和生成报告，同时确认共享行情文件没有被复制

## 5. 统一共享单场景、性能与 CLI

- [ ] 5.1 先编写失败测试，把项目 CLI、单场景编排、冷热确定性、阶段计时和停止状态迁入共享 Module
- [ ] 5.2 将 runner 固定为项目 `.venv` 加共享 CLI，配置改为声明 `strategy_root`、`strategy_module` 和 `strategy_symbol`
- [ ] 5.3 保持输入冻结、清理环境、运行复用、失败证据和 `complete/evidence_insufficient/failed` 语义不变
- [ ] 5.4 更新仓库 Skill，使 Codex、其他 Agent 和人工命令只调用共享 `run` 与 `promote` 入口

## 6. 建立 vectorbt 唯一账本并迁移即时路径

- [ ] 6.1 先编写失败测试，实现隐藏原始 Portfolio 的共享 vectorbt Adapter 和惰性只读 ExecutionLedger
- [ ] 6.2 将即时 `from_order_func()` 接线迁入共享底层，让海龟 Strategy Module 只提供项目自有 OrderProgram、状态和轨迹
- [ ] 6.3 删除成交、费用、持仓和净值镜像，验证 orders/assets/cash/value 只生成一次且结果摘要零差异
- [ ] 6.4 在等价性测试通过后评估并验证 `max_logs=0`、未使用持仓周期跟踪关闭和回调缓冲预分配

## 7. 将延迟执行迁入 vectorbt 账本

- [ ] 7.1 先用现有延迟特征测试约束冻结计划、执行日复核、优先级、最低佣金、现金/持仓截断、到期和原因码
- [ ] 7.2 实现延迟 OrderProgram，通过第二个 `from_order_func()` 处理实际订单和账户变化
- [ ] 7.3 逐笔对比新旧计划、成交、费用、现金、单位、共同止损和归因，并删除 Python 手工账本及 `from_orders()` 重放路径

## 8. 收敛海龟公开 Strategy Module

- [ ] 8.1 创建 `turtle_etf.strategy:MODULE` 唯一公开入口，把指标、参数、输入准备、订单程序和归因组合在其 Interface 后
- [ ] 8.2 将 Numba 内核、海龟归因和暂不通用的延迟语义收敛到私有实现文件，并移除测试对旧公开内部文件的依赖
- [ ] 8.3 更新 project-run、baseline、代码身份和回调摘要到新 Strategy Module 与共享运行时版本
- [ ] 8.4 删除旧策略 CLI、single_scenario、benchmark、vectorbt_engine、result_adapter 和其他已被新 Module 取代的生产入口，不保留兼容分支

## 9. 文档同步与完整验证

- [ ] 9.1 更新 Skill、研究说明、旧 OpenSpec 约束和示例命令，明确本地研究档案与聚宽 `backtests/`、`simulations/` 的语义隔离
- [ ] 9.2 从共享 CLI 执行最小策略和海龟策略的完整 run → 结果包 → promote 端到端回归，覆盖复用、证据不足、失败、冲突和中途清理
- [ ] 9.3 执行 3,432 日 × 11 ETF、3,432 日 × 17 ETF 和延迟场景的零差异及 5% 性能/内存/体积门禁
- [ ] 9.4 运行仓库完整 Build and Verify，逐项核对三份 capability 规格并确认工作区没有临时产物或双生产路径
