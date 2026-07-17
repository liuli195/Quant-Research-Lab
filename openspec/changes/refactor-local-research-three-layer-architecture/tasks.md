## 1. 冻结现有行为与性能证据

- [x] 1.1 为即时、17 ETF 扩展和 `additional_delay_days=1` 延迟场景补齐成交、费用、现金、持仓、净值、策略状态和逻辑摘要特征测试
- [x] 1.2 在固定 `.venv` 环境采集三个冷启动新进程与五次预热的主场景基线，并记录分阶段时间、峰值内存和历史整包体积观测；旧整包字段不直接用于 v2 比例门禁
- [x] 1.3 固化旧配置、停止状态、清单、代码身份和分析视图契约，确保后续破坏性迁移只能通过新 Interface 完成

## 2. 建立共享 contracts 与真实 Strategy Module 接缝

- [x] 2.1 先编写失败测试，定义 Strategy Module、LedgerInput、OrderProgram、ExecutionLedger、ExecutionRun 和 ResultExtension 的最小只读 Interface
- [x] 2.2 实现仓库内 Strategy Module 安全加载与配置校验，拒绝仓库外路径、未知 symbol、旧任意 command 和策略专属 project entry
- [x] 2.3 增加第二个最小测试策略 Adapter，并证明共享入口无需修改即可加载两个策略

## 3. 抽取后端中立标准结果包

- [x] 3.1 先编写失败测试，把四张核心表 Schema、公共跨表校验、逻辑摘要和清单契约从海龟适配器迁入共享结果 Module
- [x] 3.2 实现版本化 ResultExtension 注入，让海龟归因保持策略私有而共享 writer 不导入海龟动作码
- [x] 3.3 实现单次 Parquet 固化、回读验证、失败清理和原子发布，并证明账本视图只惰性生成和缓存一次
- [x] 3.4 扩展 analysis_data，使新本地结果、策略扩展和既有聚宽归档通过统一视图查询且保留 backend 与公式版本

## 4. 实现策略目录自包含档案

- [x] 4.1 先编写失败测试，定义档案清单、目录布局、必需代码/配置/数据/证据/报告和 `analysis_id` 校验
- [x] 4.2 实现独立 `promote` 动作，只复制已完成结果字节和策略源码，不加载策略、vectorbt 或 writer
- [x] 4.3 实现逐文件 SHA256 复核、同内容幂等复用、异内容冲突拒绝、同级暂存和原子发布
- [x] 4.4 验证删除 `.local` 源运行后档案仍可查询和生成报告，同时确认共享行情文件没有被复制

## 5. 统一共享单场景、性能与 CLI

- [ ] 5.1 先编写失败测试，把项目 CLI、单场景编排、冷热确定性、阶段计时和停止状态迁入共享 Module
- [ ] 5.2 将 runner 固定为项目 `.venv` 加共享 CLI；外部配置只声明嵌套 `strategy.root/module/symbol`，内部 `RunConfig` 再映射为 `strategy_root/strategy_module/strategy_symbol`
- [ ] 5.3 保持输入冻结、清理环境、运行复用、失败证据和 `complete/evidence_insufficient/failed` 语义不变
- [ ] 5.4 更新仓库 Skill，使 Codex、其他 Agent 和人工命令只调用共享 `run` 与 `promote` 入口

## 6. 收窄扩展表并收敛共享 writer

- [ ] 6.1 先编写失败测试，限定 ResultExtension 只接受扁平 `string/bool/int64/float64`、用 Arrow null 表示缺失并在冷/热比较前拒绝 NaN 与其他类型
- [ ] 6.2 使用 PyArrow `Table.validate`、精确 Schema 和 `Table.equals` 比较扩展，核心事实继续使用 NumPy 摘要，删除递归 Arrow 类型解码和任意类型逻辑哈希
- [ ] 6.3 将内部 writer 收敛为一次物化、一次回读事实链，公开 validator 保持纯磁盘读取，删除 `preloaded_*` 参数和 provisional/final 双包路径
- [ ] 6.4 运行结果包、runner、双策略和公开 CLI 回归，确认越界扩展固定返回 `failed/result_contract_failed`

## 7. 统一策略源码身份并简化加载

- [ ] 7.1 先编写失败测试，证明当前 module 顶层包内静态发现并排序的普通 `.py` 文件集合同时驱动运行身份和档案 `code/`，相邻目录与 `research/archives/` 不进入，descriptor 不含第二份 `source_files`
- [ ] 7.2 在每次全新单策略 `_execute` 子进程中使用标准 `importlib.import_module()`，删除 UUID 命名空间、全局导入锁和手工 `sys.modules` 生命周期
- [ ] 7.3 把第二个最小策略 fixture 缩到验证公开 Module、相对导入和无扩展结果所需的最小代码，并运行 loader、contract 和 E2E 回归

## 8. 简化可信工作区内的档案晋升

- [ ] 8.1 保留现有行为与内部测试，先新增禁止描述符/inode 状态机的 AST 边界失败测试，并补齐链接/非普通文件拒绝、硬链接拒绝、复制中断清理、同内容复用、异内容冲突和逐字节一致性行为测试
- [ ] 8.2 在本机同一用户可信工作区边界内，使用预扫描、`shutil.copy2`、复制后长度/SHA256 复核和 `os.replace` 原子发布
- [ ] 8.3 删除文件描述符、inode 与敌对并发换树状态机及其专属测试，运行完整 archive 与 analysis_data 回归

## 9. 完善通用 vectorbt 唯一账本

- [ ] 9.1 先编写通用 primary/follow-up、稳定优先级、真实成交回调和惰性只读访问器失败测试，实现隐藏原始 Portfolio 的共享 vectorbt Adapter
- [ ] 9.2 让两个最小 fixture 都通过同一 `run_vectorbt()` 完成 primary 与可选 follow-up，runtime 不解释海龟冻结计划、执行日规则或原因码
- [ ] 9.3 删除成交、费用、持仓和净值镜像，直接复用并缓存 vectorbt trades/positions/returns 等访问器；默认保留所需记录
- [ ] 9.4 等价性通过后仅评估有测量证据的 `max_logs=0` 和缓冲预分配，不通过关闭记录再自行重建统计来优化

## 10. 收敛海龟公开 Module 并迁移即时与延迟执行

- [ ] 10.1 创建 `turtle_etf.strategy:MODULE` 唯一公开入口，把配置校验、输入准备、即时/后续 OrderProgram 和 ResultExtension 组合在公开 Interface 后
- [ ] 10.2 将 Numba 内核、海龟归因和延迟冻结计划收敛到私有文件；即时和延迟实际账户变化都交给共享 `from_order_func()`
- [ ] 10.3 用现有特征逐笔验证计划、成交、费用、现金、持仓、共同止损和原因码，删除 Python 手工账本与 `from_orders()` 重放路径
- [ ] 10.4 在旧生产文件删除前重跑三个场景的 3 冷/5 热采样，把历史整包体积拆为可比 `parquet_payload_bytes` 与单独固定开销，再更新测试只依赖公开 MODULE 并运行零差异回归

## 11. 单次切换生产配置与入口

- [ ] 11.1 把生产 project-run 配置切换为 v2，并删除策略根下手工输入型 `code-identity.json`
- [ ] 11.2 在同一提交物理删除旧策略 CLI、single_scenario、benchmark、vectorbt_engine、result_adapter 及其他已被新 Module 取代的生产入口，不保留兼容分支
- [ ] 11.3 从共享 CLI 完成海龟 `run → package → promote` E2E，覆盖复用、证据不足、失败、冲突、中途清理和删除 `.local` 后查询
- [ ] 11.4 同步 Skill、研究说明、旧 OpenSpec 约束和 Build and Verify 配置，保留生成包内 `config/code-identity.json`

## 12. 发布性能门禁与完整验证

- [ ] 12.1 执行 3,432 日 × 11 ETF、3,432 日 × 17 ETF 和延迟场景的零差异及 3 冷/5 热采样
- [ ] 12.2 比较时间、峰值内存和同逻辑核心/扩展 Parquet 数据载荷体积的 5% 门禁，固定自包含开销单独报告，并继续执行 180 秒绝对门禁
- [ ] 12.3 运行仓库完整 Build and Verify，逐项核对三份 capability 规格并确认没有旧生产路径、第二套账本或临时产物
- [ ] 12.4 保存验证报告，并只在对应证据真实存在后逐项勾选本文件任务
