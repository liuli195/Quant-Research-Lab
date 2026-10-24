# 14 项配置精确清单：已批准4×2，待官方覆盖与复验

完整命令及每项 paths/inputs 见 [config.candidate.json](config.candidate.json)。候选 SHA256：

`6ba9c42fd344cb2883577432d5780fec255e7211336e7b837ff0dadecde2c395`

历史：ca082经官方init更新为80bebb，旧4×4实现d683正式14全通过47.07秒。用户 Sentinel_90c7ae7dfa508191a155b0b9d242a4d0 现明确批准永久采用4×2。当前正式目标仍为旧80bebb，本候选仅 verify.worktree-setup.pytestXdistWorkers 从4改2；maxParallel仍4，14项命令/paths/inputs/角色/顺序/超时/60hard完全不变。对应布局契约改为2，不能拿试验中13pass/1旧契约fail冒称新整体验收。旧2d99不可用。本机offline期间不手写正式目标，恢复后官方init及一次无cache固定基线复验。

最小历史验收及资源试验证据见 [verification-evidence.md](verification-evidence.md)。

## 已批准范围与本次配置变化

用户 `Sentinel_5eba966575b48191aa0b9a7d916fb320` 已批准按领域/场景保留一条最核心真实链，并明确接受所披露的物理异常矩阵及三条私有全历史复算删除。不是把旧 253 个真实函数塞进几个入口。当前静态源映射312个函数：300普通、12真实；原4×4实现已全验收，新4×2含契约改动仍待正式复验。

- 16 项改为 14 项：删除 `verify.skill-layout-real` 和 `verify.quant-equivalence`。普通 7 项，真实 7 项承接下列 12 链；每领域仍有独立选择和缓存。
- 第一批顺序为 quant-real、analysis-real、quant-calculation、sync-real，其余保留原相对顺序。基于已有耗时让长组早启动；旧4×4固定实现47.07秒通过，不承诺不同机器或后续改动始终相同。
- 命令改为实际保留模块列表，全部普通组使用精确 `-m "not real"`，全部真实组使用 `-m real`。删除已退休测试和大历史归档输入，不隐藏真实依赖。
- 量化/行情/分析共 7 项的 paths 与 inputs 补齐 `scripts/__init__.py`、`scripts/research/__init__.py`。计算项另精确包含两个 minimal_strategy 目录和 performance-baseline.json；分析普通项包含现有静态策略输入和共享测试数据接缝，真实 SQL 数据在所属小型夹具中生成。

## 运行与权限边界

- 版本 2；build.checks 保持空，无假构建。外层 maxParallel=4；只给既有工作树检查配置 pytestXdistWorkers=2 并显式加载 xdist。浏览器、工作树 checkParallel=false，按工具既有串行阶段执行；不新增调度器。线程/业务子进程不冒称包含在简单的4×2乘积内。
- 本机快/全验证 fullBudgetSeconds=60、enforceLocalBudget=true，到期终止本次进程树、停止剩余检查并失败。默认单检查 180 秒，既有量化项 300 秒；不加单测 3 秒。云端/PR/CI 沿工具原策略。
- PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 保持；现有量化 NUMBA_DISABLE_JIT=1 保持。公式通过不代表实际编译，生产 JIT 与正式编译契约不改。
- 普通组仅按精确 markexpr 模拟可选 CCXT 不可用和 IPv6 宿主能力不可用，复用依赖原有分支并 cleanup；实际 socket/Popen/守卫不改。普通结果不证明这两种宿主能力，真实组不应用模拟。
- 各 check 独立 `.local/pytest/<check-id>`，真实 worker/session 各有 native-temp；不共享可变 basetemp。普通阶段不 mkdir，tempfile 只缓存已存在的仓库内路径值，真实写仍被守卫拒绝。工具的 TEMP/TMP/TMPDIR 只绑定到已批准仓库内目录，不用以逃守卫。
- 两个量化 E2E 各复制必要公开源码、策略和 schema 到独立真实临时仓库，各用一个 .venv junction；失败必须 fail，结束只 rmdir junction 本身。无私有行情、历史归档或缓存复制；生产暂存区外写入监控和验证缓存保护保持原样。

## 按执行顺序逐项清单

1. **verify.quant-real**：真实未保护；2 条：通用策略、海龟策略的最小真实运行；后者保留一次晋升。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.quant-real tests/local_quant_research/test_generic_e2e.py tests/local_quant_research/test_turtle_e2e.py -m real`

   paths 20 项 / inputs 21 项：完整 JSON 同 ID 的两个数组；允许检查间并行；单项超时 300 秒。

2. **verify.analysis-real**：真实未保护；3 条：标准分析 run→report；backtest、simulation 两种来源各一条真实只读 SQL。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.analysis-real tests/quant_analysis/test_standard_analysis_e2e.py tests/local_quant_research/test_analysis_data_views.py -m real`

   paths 19 项 / inputs 20 项：完整 JSON 同 ID 的两个数组；允许检查间并行；单项超时 300 秒。

3. **verify.quant-calculation**：普通守卫；普通公式、回调、输入、适配器及性能结果契约；不做真实 JIT 编译。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.quant-calculation tests/local_quant_research/test_turtle_indicators.py tests/local_quant_research/test_turtle_vectorbt_callbacks.py tests/local_quant_research/test_turtle_vectorbt_inputs.py tests/local_quant_research/test_turtle_vectorbt_delayed.py tests/local_quant_research/test_turtle_vectorbt_engine.py tests/local_quant_research/test_turtle_result_adapter.py tests/local_quant_research/test_turtle_strategy_module.py tests/local_quant_research/test_vectorbt_runtime.py tests/local_quant_research/test_turtle_vectorbt_performance.py -m "not real"`

   paths 29 项 / inputs 30 项：完整 JSON 同 ID 的两个数组；允许检查间并行；单项超时 300 秒。

4. **verify.sync-real**：真实未保护；1 条：模拟归档提交→再次同步幂等→校验→查询→CSV 导出，共用一份数据。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.sync-real tests/joinquant_sync/test_sync_pipeline.py -m real`

   paths 7 项 / inputs 8 项：完整 JSON 同 ID 的两个数组；允许检查间并行；单项超时 180 秒。

5. **verify.skill-layout**：普通守卫；技能布局、配置契约及工作树脚本静态约束。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.skill-layout tests/test_skill_layout.py tests/test_setup_worktree.py -m "not real"`

   paths 14 项 / inputs 15 项：完整 JSON 同 ID 的两个数组；允许检查间并行；单项超时 180 秒。

6. **verify.sync-unit**：普通守卫；归档/查询门禁、响应解析、浏览器 CLI 和同步规则的内存输入。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.sync-unit tests/joinquant_sync/test_archive.py tests/joinquant_sync/test_browser_research.py tests/joinquant_sync/test_manual_sync_cli.py tests/joinquant_sync/test_query.py tests/joinquant_sync/test_sync_pipeline.py -m "not real"`

   paths 11 项 / inputs 12 项：完整 JSON 同 ID 的两个数组；允许检查间并行；单项超时 180 秒。

7. **verify.browser-real**：真实未保护；3 条：一次 Chromium 会话/下载；Windows DPAPI 加密恢复；SQLite 持久一次性消费。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.browser-real tests/joinquant_sync/test_browser_research.py -m real`

   paths 7 项 / inputs 8 项：完整 JSON 同 ID 的两个数组；串行阶段；单项超时 180 秒。

8. **verify.docs-unit**：普通守卫；HTML/Markdown 转换、索引、参数解析和 UTF-8 输出的内存流验证。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.docs-unit tests/joinquant_docs_sync/test_cli.py -m "not real"`

   paths 8 项 / inputs 9 项：完整 JSON 同 ID 的两个数组；允许检查间并行；单项超时 180 秒。

9. **verify.docs-real**：真实未保护；1 条：公开离线 self-test 的预览→同步→幂等→校验链。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.docs-real tests/joinquant_docs_sync/test_cli.py -m real`

   paths 8 项 / inputs 9 项：完整 JSON 同 ID 的两个数组；允许检查间并行；单项超时 180 秒。

10. **verify.quant-unit**：普通守卫；契约、结果包、晋升拒绝/禁止重算、静态历史夹具结构和摘要连接；无私有全历史复算。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.quant-unit tests/local_quant_research/test_analysis_manifest_schemas.py tests/local_quant_research/test_archive_promotion.py tests/local_quant_research/test_result_package.py tests/local_quant_research/test_runner.py tests/local_quant_research/test_strategy_contract.py tests/local_quant_research/test_evidence.py tests/local_quant_research/test_contract_fixtures.py tests/local_quant_research/test_skill_contract.py tests/local_quant_research/test_strategy_identity.py tests/local_quant_research/test_local_research_v2_e2e.py tests/local_quant_research/test_local_research_equivalence.py -m "not real"`

   paths 36 项 / inputs 37 项：完整 JSON 同 ID 的两个数组；允许检查间并行；单项超时 300 秒。

11. **verify.market-unit**：普通守卫；行情规范化、经济收益、查询选择和基准契约的普通数据规则。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.market-unit tests/local_quant_research/test_market_data_candidate_screen.py tests/local_quant_research/test_market_data_economic_returns.py tests/local_quant_research/test_market_data_query.py tests/local_quant_research/test_market_data_storage.py tests/local_quant_research/test_benchmark_set_contract.py tests/local_quant_research/test_joinquant_export.py -m "not real"`

   paths 14 项 / inputs 15 项：完整 JSON 同 ID 的两个数组；允许检查间并行；单项超时 300 秒。

12. **verify.market-real**：真实未保护；1 条：小型 CSV 运输→导入→不可变快照→真实查询，包含公司行动输入。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.market-real tests/local_quant_research/test_joinquant_export.py -m real`

   paths 9 项 / inputs 10 项：完整 JSON 同 ID 的两个数组；允许检查间并行；单项超时 300 秒。

13. **verify.analysis-unit**：普通守卫；计划/schema、统计、报告及统一分析计算；使用现有数据接缝和内存 Arrow/JSON。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.analysis-unit tests/quant_analysis/test_analysis_plan.py tests/quant_analysis/test_reporting.py tests/quant_analysis/test_statistics.py tests/quant_analysis/test_unified_analysis.py -m "not real"`

   paths 24 项 / inputs 25 项：完整 JSON 同 ID 的两个数组；允许检查间并行；单项超时 300 秒。

14. **verify.worktree-setup**：真实未保护；1 条：同一 Git 仓库 main 初始化→worktree 共享环境→清单正常更新→正式验证入口。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest -p xdist.plugin --basetemp=.local/pytest/verify.worktree-setup tests/test_setup_worktree.py -m real`

   paths 7 项 / inputs 8 项：完整 JSON 同 ID 的两个数组；串行阶段；单项超时 180 秒。

## 7 个真实检查 → 12 条实际链

- verify.sync-real：`tests/joinquant_sync/test_sync_pipeline.py::test_public_simulation_archive_query_smoke`
- verify.docs-real：`tests/joinquant_docs_sync/test_cli.py::test_public_self_test_runs_complete_offline_flow`
- verify.browser-real：`tests/joinquant_sync/test_browser_research.py::test_persistent_context_and_download_capture`
- verify.browser-real：`tests/joinquant_sync/test_browser_research.py::test_authenticated_session_is_encrypted_and_restored`
- verify.browser-real：`tests/joinquant_sync/test_browser_research.py::test_paid_preview_is_bound_and_one_time`
- verify.quant-real：`tests/local_quant_research/test_generic_e2e.py::test_minimal_strategy_completes_public_run`
- verify.quant-real：`tests/local_quant_research/test_turtle_e2e.py::test_turtle_project_completes_full_single_scenario_entrypoint`
- verify.market-real：`tests/local_quant_research/test_joinquant_export.py::test_verified_transfer_snapshot_query_smoke`
- verify.analysis-real：`tests/quant_analysis/test_standard_analysis_e2e.py::test_standard_analysis_skill_runs_only_the_read_only_package_flow`
- verify.analysis-real：`tests/local_quant_research/test_analysis_data_views.py::test_joinquant_source_builds_six_read_only_logical_views`
- verify.analysis-real：`tests/local_quant_research/test_analysis_data_views.py::test_joinquant_simulation_requires_and_pins_one_snapshot`
- verify.worktree-setup：`tests/test_setup_worktree.py::test_main_worktree_update_and_verification_smoke`

## 真实输入和准备次数

- 同步：一份公开模拟响应和研究响应，一次归档数据；第二次同步验证幂等，不重新建独立归档。文档：一次离线目录树和同链输出。
- 浏览器：一次 Chromium launch/context/download；DPAPI 一次会话加密/恢复，不另启动浏览器；付费预览一次文件 SQLite 创建/消费，并以第二次消费拒绝证明持久约束。三种不可替代能力各一条。
- 通用研究：2 日、1 资产、1 批次、1 快照、1 run；海龟：70 个交易日、11 ETF、1 批次、1 快照、1 run、1 次晋升。两 case 各有宿主repo根内标准库TemporaryDirectory生成的短 .q 临时repo/venv junction，无新增session基线层；先rmdir junction再清owned目录，不清共享环境。
- 行情：一个 4 行/2 证券的小运输与一个公司行动输入；分析 run/report 复用同一计划、两小结果包和一份基准。
- 只读 SQL：backtest 和 simulation 各创建同 schema 的 6 张小 Parquet 表，共 12 张；非空结果/余额各 2 行、风险 1 行，其余允许空，仅读这些小树。simulation 固定一个 tiny-snapshot。没有扫描 backtests/111 或私有大历史。
- 工作树：一个 Git 仓库、一个真实环境；同链完成 main、worktree 与正常清单更新。仍复用现有 PowerShell/项目 .venv，不用静态字符串代替实际分支。本轮该check实测17.09秒，不承诺未来运行耗时。

## 覆盖变化与证据边界

- 转普通：同步/归档前置拒绝与响应数据，文档解析/编码内存流，结果包数据契约及复用/冲突规则，行情规范化/选择与摘要，分析计划/schema/Arrow 表/统计/报告计算等；保留原有有价值断言，使用已有公开数据接缝，不造 fake FS/DB。
- 按批准退休：锁竞争、系统重试、链接组合、异常回滚、历史格式、重复 CLI 接线的独有物理组合；生产保护未删，不能宣称物理异常覆盖完全不变。最后残留 backtests/111 全树/Parquet 历史实体验证也退休，不暗留未声明 LFS 依赖。
- 删除三条私有全历史重算；保留两项静态历史夹具结构/摘要连接和原研究证据。静态证据不等于自动精确复算，无隐藏 skip、无第二手动入口。重复独立 JIT 检查已去重，公式仍在。
- 初次清单阶段仅有静态检查，旧轮普通guard不能替代本轮。现d683正式14项实际PASS、7普通guard完整无违规、7真实12链全运行，无cache/skip/未运行；不以删除标记或AST解析冒称通过。

## 官方步骤与当前恢复位置

1. 旧本机准确仓库/项目.venv/2.7.1身份已核，未重装依赖；当前本机offline。恢复后先核新feature提交/候选SHA及旧目标80bebb，不绕本机依赖限制。
2. 用户已明确批准本次仅workers4→2；恢复后须经裸 `build-and-verify init --project "D:\My Project\Quant-Research-Lab" --config "D:\My Project\Quant-Research-Lab\myspec\changes\2026-10-24-migrate-guarded-validation\config.candidate.json" --overwrite` 备份并写入，核正式目标与候选一致；不手写目标。
3. 官方写入新候选后串行一次无cache固定基线14项，保持60hard/guard不变；新结果未有。旧d683的14PASS/47.07秒、清理和试验失败边界保留，不能移作新配置通过。4×2/6×2/8×2临时试验均13PASS/1旧布局契约FAIL；浏览器波动使总耗时差不能全归因并发。
4. 量化仓库 BAV 身份已基于官方 doctor 同步 2.7.1 / 源 0cecf8a，独立 CI 旧锁已同步；MySpec 旧锁不擅改。Ubuntu 干净 verify 的 skipped 仅工具链/结构证据，非 Windows 验收。正式规格仍待 Gate2。
