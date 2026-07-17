## ADDED Requirements

### Requirement: 共享结果包必须定义后端中立的核心事实
系统 MUST 用版本化 Schema 定义结果、资金、持仓和订单四张核心事实表，并允许策略通过版本化扩展增加归因等证据。核心 writer 不得导入具体策略动作码，策略扩展不得改变核心表的字段含义。

#### Scenario: 海龟策略生成结果包
- **WHEN** 海龟执行完成并提供策略轨迹
- **THEN** 共享 writer 从 ExecutionLedger 生成四张核心表，并把海龟归因作为独立扩展写入清单

#### Scenario: 最小策略不提供归因
- **WHEN** 第二个测试策略只提供核心执行账本而没有策略扩展
- **THEN** 共享 writer 仍生成合法完整结果包，并明确声明没有对应扩展

### Requirement: 策略扩展必须使用有边界的 Arrow 契约
每个 `ResultExtension.table` MUST 只包含扁平 `string`、`bool`、`int64` 或 `float64` 列。浮点缺失值 MUST 使用 Arrow null，不得使用 NaN。dictionary、list、struct、map、union、run-end encoded 及其他类型 MUST 在冷/热比较前以 `result_contract_failed` 拒绝；共享层不得实现递归 Arrow 类型解释器或任意类型逻辑哈希。

#### Scenario: 比较合法扩展
- **WHEN** 冷启动和预热产生相同的扁平扩展表
- **THEN** 共享层先调用 `Table.validate(full=True)`、比较精确 Schema，再用 `Table.equals(check_metadata=True)` 判断相等

#### Scenario: 拒绝越界扩展
- **WHEN** 扩展包含 NaN、嵌套、字典编码或其他未允许类型
- **THEN** 系统在冷/热确定性比较和 Parquet 写入前返回 `failed` 与 `result_contract_failed`

### Requirement: 账本视图和事实数据只能物化一次
ExecutionLedger MUST 对 orders、assets、cash 和 value 使用只读惰性缓存；共享 writer MUST 直接消费这些视图并只执行一次公共事实转换、一次 Parquet 固化和一次回读。内部 writer MUST 复用这次回读事实完成 Schema、唯一键、跨表勾稽、报告和最终清单；公开 validator MUST 只从磁盘读取并供复用、晋升和外部查询。运行路径不得通过 JSON、字典或 Arrow 往返重新构造账本，不得在多层重复复制完整矩阵，也不得通过 `preloaded_*` 参数或 provisional/final 两套完整包维护第二条校验路径。

#### Scenario: 多个消费者读取净值
- **WHEN** 性能摘要、结果 writer 和策略归因都需要组合净值
- **THEN** 它们复用同一个缓存视图，不分别调用 vectorbt 生成或复制完整净值数组

#### Scenario: 策略不需要某类轨迹
- **WHEN** Strategy Module 未声明某个策略审计字段
- **THEN** 执行底层不为该字段分配行数乘证券数的稠密矩阵

### Requirement: 结果包必须可验证并原子发布
系统 MUST 为每个数据集记录路径、状态、行数、时间范围、Schema 版本和 SHA256，并在暂存目录完成写入、回读、跨表校验和核心事实摘要后原子发布。核心事实确定性使用 NumPy 数组摘要；扩展确定性使用精确 Schema 与 `Table.equals(check_metadata=True)`；文件完整性使用最终 Parquet 字节的 SHA256。任何校验或发布失败 MUST 清理暂存目录且不得留下可被识别为完整的结果。

#### Scenario: 成功固化结果包
- **WHEN** 四张核心表和全部声明扩展通过 Schema、摘要和跨表勾稽
- **THEN** 系统原子发布数据文件和清单，并把运行标记为 `complete`

#### Scenario: 回读事实不一致
- **WHEN** Parquet 回读后的核心事实摘要、扩展表或文件摘要与内存事实和最终清单不同
- **THEN** 系统返回 `failed`，删除暂存结果且不覆盖既有完整运行

### Requirement: 共享分析必须读取本地和聚宽结果
analysis_data MUST 通过后端中立清单读取新的本地结果包和既有聚宽归档，并为相同概念提供一致查询视图。任何来源差异、缺失数据集和公式版本 MUST 保留显式证据，不得伪造成相同来源或静默补全。

#### Scenario: 比较本地研究和聚宽回测
- **WHEN** 分析流程同时打开一个本地 vectorbt 结果和一个聚宽正式回测
- **THEN** 它通过统一视图查询共同事实，同时保留 backend、来源身份和数据集状态

#### Scenario: 使用 vectorbt 统计交叉校验
- **WHEN** 分析流程调用 vectorbt returns 或 stats 复核收益指标
- **THEN** 结果被标记为交叉校验，不静默替换现有 Alpha、Information Ratio、CVaR 或其他公式版本

### Requirement: 性能证据必须用明确边界覆盖完整固化路径
系统 MUST 使用两个不自指的观测边界覆盖完整固化路径。结果包内 `performance.json` MUST 诚实记录从 writer 启动到最终 evidence/report/manifest 写入前的 `prefinalization_seconds` 及各阶段耗时，不得宣称包含尚未发生的自身写入或父进程发布；日常 cold/warm 门禁 MUST 使用 writer 返回时的完整耗时，覆盖策略执行、核心事实、策略扩展、Parquet 写入、回读校验、摘要和最终元数据写入。发布验证 MUST 把引擎 3 冷/5 热采样与三个独立冷进程的完整 CLI 发布采样分开；完整 CLI 样本 MUST 禁止结果复用，并由父进程测量进程启动、子进程执行、父进程原子发布和发布后校验的总耗时。5% 门禁 MUST 比较重构前后相同协议和起止点的完整 CLI 总耗时；`finalize_publish` 只作诊断。外部报告 MUST 用协议版本、场景、样本类型和序号、PID、run_id、package_sha256、非复用标记、发布后校验状态、baseline 摘要和环境摘要绑定实际包，并证明报告生成未修改结果包。不得为了把最终总耗时写回同一个不可重写包而恢复 provisional/final 双包、第二次元数据写入或旁路清单。相对体积门禁 MUST 只比较同逻辑核心/扩展 Parquet 数据载荷；代码、配置、证据和报告等固定自包含开销 MUST 单独报告。重构不得通过在相同观测边界之间移动工作来满足门禁。

#### Scenario: 抽取共享 writer
- **WHEN** 原策略 writer 迁移到共享结果包
- **THEN** 包内预最终化、日常 writer 返回门禁和发布级父进程总计时分别使用固定边界，重构前后基准计入相同固化步骤，并能单独比较执行、事实转换、扩展、文件和最终发布阶段
