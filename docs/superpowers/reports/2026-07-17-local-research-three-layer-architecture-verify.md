# 本地研究三层架构验证报告

## 结论

状态：**通过**。

三层架构、结果包自包含、档案晋升、唯一 vectorbt（向量化回测）账本和公开 CLI（命令行入口）均通过完整验证。性能差异已如实列出，用户于 2026-07-18 明确确认接受；不建设自动相对门禁或专用发布性能命令。

## 能力规格覆盖

### local-quant-research-runtime（本地量化研究运行时）

- 配置 v2、共享 CLI、`turtle_etf.strategy:MODULE` 和即时/延迟统一 vectorbt 账本通过单元与 E2E（端到端）验证。
- 三个冻结场景的成交、费用、现金、持仓、净值、状态和逻辑摘要零差异。
- 日常 cold/warm（冷/热）确定性检查和各 180 秒超时保留。
- 公开 CLI 只包含 `run` 与 `promote`，没有发布性能工作流。

### local-research-result-package（本地研究结果包）

- 核心表、策略扩展、清单、逐文件 SHA256、原子发布和发布后校验通过。
- 共享 writer（写入器）直接使用 PyArrow（列式计算库）提供的 Zstd（列式压缩），没有新增依赖或封装。
- 三个场景的 Parquet（列式文件）数据载荷相对历史观测分别为 0.9910、0.9919、0.9982。

### local-research-archive-promotion（本地研究档案晋升）

- `run → package → promote`、同内容幂等、异内容冲突、中断清理、删除 `.local` 源后查询和共享行情不复制均通过。
- 本次变更没有修改 JoinQuant（聚宽）模拟交易同步、定时归档或运行目录隔离。

## 性能人工确认

下表格式为“当前观测 / 历史观测比值”；它用于说明现状，不作为自动判退条件。

| 场景 | 完整 CLI 时间 | 引擎 cold | 引擎 warm | 峰值内存 | Parquet 载荷 |
|---|---:|---:|---:|---:|---:|
| immediate-11-etf | 79.988 秒 / 1.1727 | 20.960 秒 / 0.7664 | 3.153 秒 / 0.6760 | 682,061,824 / 0.8760 | 1,269,788 / 0.9910 |
| immediate-17-etf | 74.767 秒 / 0.9925 | 36.314 秒 / 1.0933 | 4.899 秒 / 0.6669 | 718,110,720 / 0.8356 | 2,161,984 / 0.9919 |
| delayed-11-etf-1d | 78.296 秒 / 1.1326 | 29.027 秒 / 1.0457 | 3.435 秒 / 0.7298 | 728,416,256 / 0.9298 | 1,250,489 / 0.9982 |

观测结果不呈现统一退化：17 ETF 完整 CLI 优于历史观测；两个 11 ETF 完整 CLI 分别增加 17.3% 和 13.3%，但引擎、内存和数据体积多数改善。所有采集样本均小于 180 秒，最大完整 CLI 样本为 102.656 秒。用户已确认接受当前结果。

## 验证结果

TDD（测试驱动开发）防回归：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_runner.py::test_public_cli_omits_release_performance_workflow -q
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_contract_fixtures.py::test_local_research_performance_baseline_freezes_observations -q
```

两项测试均先按旧行为失败，删除自动门禁与 fixture 阈值后通过。

相关回归：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_contract_fixtures.py tests\local_quant_research\test_runner.py tests\local_quant_research\test_result_package.py tests\test_skill_layout.py -q
```

结果：126 项通过。

完整 Build and Verify（构建与验证）：

```powershell
.\.venv\Scripts\python.exe .build-and-verify\runtime\build_and_verify.py verify --project . --full
```

结果：通过，耗时 197.1 秒，11/11 检查项完成；本地研究单元测试 404 项通过，4 条来自 vectorbt 依赖的 Pandas 弃用警告；真实 E2E 8 项通过；OpenSpec（开放规格）严格校验 6 项通过。

## 残留扫描

- 生产代码中 vectorbt import（导入）的唯一位置为 `scripts/research/local_quant_research/vectorbt_runtime.py`；测试 fixture 中的独立导入不属于生产路径。
- 自动发布性能命令、采样器、策略性能 profile、专用发布时间字段和临时目录均不存在。
- 旧入口字符串只存在于冻结历史观测、删除断言或测试名称，不存在生产导入。
- 策略根 `code-identity.json` 不存在；测试档案、PID 文件和性能工作目录无残留。
- `.attempts` 是失败运行证据，不按临时产物删除。
