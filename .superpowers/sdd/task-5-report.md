# Task 5 实施报告

STATUS: DONE_WITH_CONCERNS

## 实施结果

- 配置固定为 v2，拒绝 `command/project_entry/code_identity/required_outputs/output_root/stop_states`，错误码为 `legacy_run_field`。
- 公开 CLI 仅保留 `run --config` 与 `promote`；私有 `_execute` 仅由父 runner 生成，公开帮助不显示。
- 新增共享 `vectorbt_runtime.py`，Task 5 最小边界只支持 1 列、2 日、无订单，并实际调用 `Portfolio.from_order_func()`；原始 Portfolio 不公开，账本数组只读。
- 新增 `performance.py` 与 `scenario.py`：每个日常 run 恰好一次 cold 和一次 warm，比较完整 execution digest，各自严格小于 180 秒；结果包只物化一次。
- 父 runner 保留输入冻结、摘要复核、清洁环境、外写检测、失败 attempt、完成复用、冲突失败与原子发布；最终目录直接是 archive-ready package。
- Skill 保持单次调用、单场景、返回调用者边界，并公开 run/promote 两条固定命令。

## RED 证据

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_runner.py tests\local_quant_research\test_evidence.py tests\local_quant_research\test_skill_contract.py tests\local_quant_research\test_generic_e2e.py tests\local_quant_research\test_vectorbt_runtime.py -q
```

结果：`17 failed, 37 passed in 8.76s`。

预期失败原因：旧 runner 仍要求 v1 字段；`legacy_run_field`、固定 `_execute`、performance/vectorbt runtime、Skill promote/archive-ready 与 v2 E2E 尚未实现。

补充 RED：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\local_quant_research\test_runner.py::test_same_size_input_digest_change_is_rejected_after_shared_execution -q
```

结果：`1 failed in 8.69s`；同长度并恢复 mtime 的输入篡改曾被仅大小/mtime 检查漏过。

## GREEN 证据

指定 Task 5 集合：`56 passed in 55.83s`。

摘要复核补强：`2 passed in 8.19s`，同时覆盖永久摘要变化失败与临时变化后恢复成功。

## 验证

- 相关回归：`test_strategy_contract.py test_result_package.py test_archive_promotion.py test_analysis_data_views.py` 共 `193 passed`。
- Ruff：允许的 Python 文件 `All checks passed!`。
- `git diff --check`：通过。
- 共享 `local_quant_research` 中仅 `vectorbt_runtime.py` 导入 vectorbt。
- E2E 明确断言 cold/warm 摘要相同、各小于 180 秒、固定十阶段、四表、自包含代码/配置/证据/报告、复用、摘要冲突与无 DuckDB 残留。
- 未触碰、未暂存、未提交 `joinquant/strategies/` 并行修改或未跟踪归档文件；未触碰协调文件。

## 改动文件

- `.agents/skills/run-local-quant-research/SKILL.md`
- `.agents/skills/run-local-quant-research/agents/openai.yaml`
- `scripts/research/local_quant_research/contracts.py`
- `scripts/research/local_quant_research/vectorbt_runtime.py`
- `scripts/research/local_quant_research/scenario.py`
- `scripts/research/local_quant_research/performance.py`
- `scripts/research/local_quant_research/runner.py`
- `scripts/research/local_quant_research/evidence.py`
- `scripts/research/local_quant_research/cli.py`
- `tests/local_quant_research/test_runner.py`
- `tests/local_quant_research/test_skill_contract.py`
- `tests/local_quant_research/test_generic_e2e.py`
- `tests/local_quant_research/test_vectorbt_runtime.py`

## 风险与疑问

- 已知旧 `tests/local_quant_research/test_turtle_e2e.py` 仍提交 v1 `command/project_entry/...` 配置，单独运行时按新合同得到 `evidence_insufficient`。协调者已确认这是 Task 7/8 的预期迁移项；本任务受边界限制未修改该测试、海龟策略或生产配置。
- Task 5 的 vectorbt runtime 仅实现明确验收的最小无订单 seam；稳定优先级、订单转换、成交回调、惰性缓存与延迟程序留给 Task 6。

## Fix Round 1

### 关闭情况与 RED/GREEN

1. 执行隔离与路径绑定
   - RED：`test_fixed_output_root_cannot_escape_repository_through_directory_link` 与 `test_private_execute_rejects_staging_not_bound_to_frozen_request` 为 `2 failed`；旧实现缺少固定输出根链接拒绝，并把任意 `--staging` 只归为普通 frozen request 错误。
   - GREEN：同组 `2 passed`。live `cli.py` 只做 stdlib（标准库）校验/引导；request 精确绑定 live repository、frozen repository、market data、runtime cache 与 staging。真实 `_execute` 从冻结 `adapter_guard.py` 安装 audit guard（审计守卫），只允许本次 staging 和精确 runtime cache 写入，禁止 live repo 读取和子进程。
   - 补充 GREEN：adapter guard 外写/live-read/process 回归 `1 passed`；真实公开 CLI E2E `1 passed`。
2. 首次身份冻结与冻结 runtime 执行
   - RED：runtime 未复制、scenario 在身份捕获后变化仍会进入子进程，针对性 `2 failed`；冻结 bootstrap 仍加载 live runner，另有 `1 failed`；守卫未在真实 bootstrap 安装，另有 `1 failed`。
   - GREEN：runtime/source 首次捕获摘要驱动复制 `2 passed`；冻结 runner/bootstrap/guard `1 passed`。冻结集合包含 `scripts/__init__.py`、`scripts/research/__init__.py`、全部共享 local runtime、market data runtime 与策略 source files。
3. 完成包强身份复用
   - RED：`test_completed_package_reuse_binds_all_frozen_identity_documents` 为 `1 failed`，旧 `_package_identity()` 不接收完整 expected identity。
   - GREEN：包内 `project-run/scenario/code-identity/market-snapshot/runtime-lock` 与本次冻结身份逐项相等，并从包内证据机械重算 config/code/snapshot digest 与 run_id；针对性 `3 passed`。
4. 完整性能门禁与 writer 四阶段证据
   - RED：writer 没有阶段返回，且共享 writer 耗时无法加入 cold/warm 门禁，针对性 `2 failed`。
   - GREEN：writer 持久化 `core_facts/parquet_materialize/readback_validate/report_and_manifest` 四个真实阶段，单次 writer wall time（墙钟时间）同时加入 cold/warm 的 180 秒门禁，针对性 `2 passed`。
   - 单次物化设计：四张核心表和扩展只写一轮 Parquet、只回读一轮；首轮 report/manifest 完成后，仅轻量重写 `performance.json`、两份 report 和 manifest 以解决因果计时，不重算 core facts、不重写或重读 Parquet。记录写入测试仍严格为 5 次 Parquet 写入。
5. cold/warm 生命周期与流式摘要
   - RED：cold outcome 在 warm 开始时仍存活、NumPy 摘要整块复制、extension 调用 `to_pylist()`，针对性 `3 failed`。
   - GREEN：cold 摘要后立即释放；即时 `final is primary` 用稳定引用且只扫描一次；相同 ledger array 用引用表示；NumPy 使用 buffer/分块摘要，Arrow extension 使用 schema + record batch buffers 流式摘要，针对性 `3 passed`。
6. `scenario_id` 前置
   - RED：缺失与空值均未在配置加载阶段拒绝，`2 failed`。
   - GREEN：`load_run_config()` 在任何策略 prepare/vectorbt 前抛稳定 `ConfigurationError("missing_scenario_id")`，`2 passed`。
7. Skill 输出合同
   - RED：Skill 契约缺少“固定唯一 archive-ready package、不由配置声明”语义，`1 failed`。
   - GREEN：Skill 改为正向固定输出合同，移除“唯一必需输出/必需输出声明”，`1 passed`。
8. ledger/trace/Portfolio 覆盖
   - 审查已确认生产实现静态合规，本项缺陷是测试覆盖不足，无法诚实制造生产 RED。新增测试遍历 `orders/assets/cash/value/trades/positions/returns`，验证只读、缓存身份、trace 只读缓存及无公开 Portfolio；直接 `1 passed`。

### 验证

- Task 5 指定集合加新增 result package 回归：`137 passed in 62.76s`。
- Task 3/4 结果包、晋升、查询相关回归：`193 passed in 15.21s`。
- 真实公开 CLI E2E：`1 passed in 38.75s`。
- Ruff：`All checks passed!`。
- `git diff --check`：通过。
- 旧 `test_turtle_e2e.py` 的 v1 配置失败仍是 Task 7/8 已知项，本轮未扩围。

### Fix Round 1 变更文件

- `.agents/skills/run-local-quant-research/SKILL.md`
- `scripts/research/local_quant_research/adapter_guard.py`
- `scripts/research/local_quant_research/cli.py`
- `scripts/research/local_quant_research/evidence.py`
- `scripts/research/local_quant_research/performance.py`
- `scripts/research/local_quant_research/result_package.py`
- `scripts/research/local_quant_research/runner.py`
- `scripts/research/local_quant_research/scenario.py`
- `tests/local_quant_research/test_evidence.py`
- `tests/local_quant_research/test_result_package.py`
- `tests/local_quant_research/test_runner.py`
- `tests/local_quant_research/test_skill_contract.py`
- `tests/local_quant_research/test_vectorbt_runtime.py`

### 遗留顾虑

- `report_and_manifest` 的最终真实时长存在因果自引用：最终值写入文件本身会继续消耗时间。本实现只做一次轻量 finalize（收尾重写）；完整 wall time 仍参与 180 秒失败门禁，而持久化阶段值记录 finalize 前的真实 report/manifest pass。该 finalize 不重写 Parquet、不重复 core facts 或全表扫描。
- Task 5 runtime 仍只覆盖既定最小无订单 seam；Task 6 的完整订单生命周期不在本轮范围。

## Fix Round 2

### 关闭情况与 RED/GREEN

1. execution digest（执行摘要）计时
   - RED：慢摘要用例 `1 failed`，摘要发生在 `_sample()` 计时结束之后。
   - GREEN：摘要完成后才停止计时；慢摘要计入 cold/warm（冷/热）及 180 秒门禁，`1 passed`。
2. writer（写入器）单次全表工作
   - RED：事实访问器与完整 validator（校验器）在 finalize（收尾）再次运行，相关用例 `1 failed`。
   - GREEN：报告事实、表条目与已验证结果在首个完整 pass（轮次）缓存，收尾只重写固定小型证据、报告和清单；完整 validator 只调用一次，`1 passed`。
3. 性能证据测量范围
   - RED：持久化证据不能区分首个完整报告/清单耗时、持久化门禁测量值和 writer 返回前实际门禁，`1 failed`。
   - GREEN：新增 `writer.first_full_pass_seconds`、`writer.gate_measured_seconds` 与 `measurement_scope`；实际 complete/failed 仍以 writer 返回后的完整墙钟时间判断，`1 passed`。
4. frozen bootstrap（冻结引导）路径封闭
   - RED：repository/market-data/runtime-cache 的 symlink/junction（符号链接/目录联接）矩阵 `6 failed`；request 的 project/run/attempt/output 绑定 `1 failed`。
   - GREEN：live CLI 逐组件 `lstat` 校验普通目录与固定命名/父子关系，guard 文件也必须是普通文件；同组 `7 passed`。
5. 输出根、run 与 attempt 安全
   - RED：已链接 `.attempts` 仍可能写入，`1 failed`；run-dir（运行目录）缺少安全解析入口，`1 failed`。
   - GREEN：project/run/.attempts/staging 都复用普通输出目录解析；不安全时保持稳定状态、`attempt_id=None` 且不写盘，`2 passed`。
6. Arrow extension digest（Arrow 扩展摘要）
   - RED：slice offset（切片偏移）、dictionary values（字典值）与 nested/chunk（嵌套/分块）布局用例 `3 failed`。
   - GREEN：固定 65,536 行窗口内 `combine_chunks()`，再用 Arrow IPC（进程间格式）规范流摘要；不调用 `to_pylist()`，不整表合并；原有 null/NaN、字段顺序与全值覆盖继续通过，相关文件 `10 passed`。
7. `scenario_id` 完成包交叉绑定
   - RED：内部重签且自洽、但 manifest object 与冻结 scenario 不同的包，通用 validator 和 runner reuse 均接受，`2 failed`。
   - GREEN：两层都机械交叉比较 `scenario_id`，`2 passed`。
8. 父进程零策略执行
   - RED：父进程身份捕获会执行策略顶层，`1 failed`；静态源发现缺失与链接源封闭补充用例 `2 failed`。
   - GREEN：父进程只用 `PathFinder`（路径查找器）静态解析并冻结策略根全部普通 Python 源；真正 `load_strategy()` 仅在 guard 安装后的冻结子进程执行，同组 `3 passed`。真实子进程外写/启进程验证 `2 passed`，均固定返回 `access_guard_violation` 且无外部副作用。
9. bootstrap/import（引导/导入）稳定错误
   - RED：guard 导入错误输出 traceback（堆栈），链接 guard 可触发顶层，`2 failed`。
   - GREEN：引导错误固定为 `frozen_bootstrap_failed`，不输出异常细节；链接 guard 在执行前拒绝，同组 `2 passed`。同时将 TEMP/TMP、Numba/Matplotlib 缓存固定到本次 runtime-cache，避免标准库临时目录探测吞掉底层权限错误。
10. `scenario_id` 前置完整性
   - RED：纯空白值未被拒绝，`1 failed`；writer 对纯空白值仍可生成包，`1 failed`。
   - GREEN：缺失、空字符串、纯空白和非字符串都在策略执行前返回 `missing_scenario_id -> evidence_insufficient`，writer/validator 同样拒绝空白身份，`2 passed`。

### 验证

- Task 5 全集及 Task 2/3/4/5 相关回归：`289 passed in 82.84s`，`0 failed`。
- 真实冻结子进程安全定向回归：`2 passed in 10.55s`。
- Ruff（代码规范检查）：允许的 11 个 Python 文件 `All checks passed!`。
- `git diff --check`：通过。
- 允许文件清单：7 个共享运行模块、4 个对应测试和本报告；未触碰、未暂存、未提交 `joinquant/strategies/` 与协调文件的并行修改。

### Fix Round 2 变更文件

- `scripts/research/local_quant_research/cli.py`
- `scripts/research/local_quant_research/evidence.py`
- `scripts/research/local_quant_research/performance.py`
- `scripts/research/local_quant_research/result_package.py`
- `scripts/research/local_quant_research/runner.py`
- `scripts/research/local_quant_research/scenario.py`
- `scripts/research/local_quant_research/strategy_loader.py`
- `tests/local_quant_research/test_evidence.py`
- `tests/local_quant_research/test_result_package.py`
- `tests/local_quant_research/test_runner.py`
- `tests/local_quant_research/test_strategy_contract.py`
- `.superpowers/sdd/task-5-report.md`

### 遗留顾虑

- 旧 `test_turtle_e2e.py` 的 v1 配置仍属于 Task 7/8 迁移项，本轮没有扩围。
- 最终性能数字的小型元数据写回仍是明确记录的 observer overhead（观测开销）；摘要、事实转换、Parquet、回读、报告与完整校验均已计入门禁，writer 返回前的实际墙钟时间仍决定最终状态。
