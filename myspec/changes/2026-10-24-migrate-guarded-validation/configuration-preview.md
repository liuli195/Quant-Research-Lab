# 新配置精确清单（待最终写入确认）

完整命令、逐项 paths/inputs 以 [config.candidate.json](config.candidate.json) 为准；此文件不是已写入的目标配置。

## 运行与权限边界

- 配置版本2；构建检查空集；16个检查，7普通守卫、9真实未保护。
- 最大外层并发4。仅工作树检查内部4个Pytest工作进程并显式加载xdist；浏览器和工作树检查在并行批次结束后分别串行执行，其他检查安全并行。原生库线程和业务子进程另行计数，不声称总进程硬上限16。
- 本机快/全验证60秒总硬截止，强制开关true；默认单检查180秒，原量化单项300秒保留。无3秒单测限制；云端/PR/CI沿工具原策略。
- Pytest插件自动加载关闭。原量化检查保留NUMBA_DISABLE_JIT=1，不把公式通过冒充编译通过。
- 每check独立basetemp为 .local/pytest/<check-id>；真实检查各worker/session另有native-temp，TEMP/TMP/TMPDIR随之绑定。会话共享基线只读、每case独立复制。守卫自身临时证据仍走工具原有每check隔离机制；启动官方工具前先把其临时根绑定在已确认的本repo目录，不逃守卫。
- 三个私有快照场景条件skip，静态覆盖不等于复算通过；不上传私有行情或凭据。

## 逐项清单

1. **verify.skill-layout**：普通守卫；技能布局与配置的静态契约；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.skill-layout tests/test_skill_layout.py tests/test_setup_worktree.py -m "not real"`

   paths/inputs：见完整JSON同ID条目。

2. **verify.skill-layout-real**：真实未保护；两项隔离进程导入边界；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.skill-layout-real tests/test_skill_layout.py -m real`

   paths/inputs：见完整JSON同ID条目。

3. **verify.sync-unit**：普通守卫；归档/浏览器响应/同步的内存契约与必要静态读取；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.sync-unit tests/joinquant_sync/test_archive.py tests/joinquant_sync/test_browser_research.py tests/joinquant_sync/test_manual_sync_cli.py tests/joinquant_sync/test_query.py tests/joinquant_sync/test_self_test.py tests/joinquant_sync/test_sync_pipeline.py -m "not real"`

   paths/inputs：见完整JSON同ID条目。

4. **verify.sync-real**：真实未保护；同步归档、原生查询和公开自检的持久主路；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.sync-real tests/joinquant_sync/test_archive.py tests/joinquant_sync/test_query.py tests/joinquant_sync/test_self_test.py tests/joinquant_sync/test_sync_pipeline.py -m real`

   paths/inputs：见完整JSON同ID条目。

5. **verify.browser-real**：真实未保护；Chromium/Windows加密会话及SQLite一次性消费；并行批次后串行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.browser-real tests/joinquant_sync/test_browser_research.py -m real`

   paths/inputs：见完整JSON同ID条目。

6. **verify.docs-unit**：普通守卫；HTML格式与API索引内存转换；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.docs-unit tests/joinquant_docs_sync/test_cli.py -m "not real"`

   paths/inputs：见完整JSON同ID条目。

7. **verify.docs-real**：真实未保护；文档CLI预览/同步/幂等/失败保留/校验；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.docs-real tests/joinquant_docs_sync/test_cli.py -m real`

   paths/inputs：见完整JSON同ID条目。

8. **verify.quant-unit**：普通守卫；量化配置、结果包和晋升的内存及静态契约；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.quant-unit tests/local_quant_research/test_analysis_manifest_schemas.py tests/local_quant_research/test_archive_promotion.py tests/local_quant_research/test_result_package.py tests/local_quant_research/test_runner.py tests/local_quant_research/test_strategy_contract.py tests/local_quant_research/test_evidence.py tests/local_quant_research/test_contract_fixtures.py tests/local_quant_research/test_skill_contract.py tests/local_quant_research/test_strategy_identity.py tests/local_quant_research/test_local_research_v2_e2e.py tests/local_quant_research/test_local_research_equivalence.py tests/local_quant_research/test_generic_e2e.py tests/local_quant_research/test_turtle_e2e.py -m "not real"`

   paths/inputs：见完整JSON同ID条目。

9. **verify.quant-real**：真实未保护；量化真实运行、结果包固化及晋升的核心成功/恢复路径；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.quant-real tests/local_quant_research/test_analysis_manifest_schemas.py tests/local_quant_research/test_archive_promotion.py tests/local_quant_research/test_result_package.py tests/local_quant_research/test_runner.py tests/local_quant_research/test_strategy_contract.py tests/local_quant_research/test_evidence.py tests/local_quant_research/test_contract_fixtures.py tests/local_quant_research/test_skill_contract.py tests/local_quant_research/test_strategy_identity.py tests/local_quant_research/test_local_research_v2_e2e.py tests/local_quant_research/test_generic_e2e.py tests/local_quant_research/test_turtle_e2e.py -m real`

   paths/inputs：见完整JSON同ID条目。

10. **verify.quant-calculation**：普通守卫；海龟与vectorbt计算、输入、回调公式；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.quant-calculation tests/local_quant_research/test_turtle_indicators.py tests/local_quant_research/test_turtle_vectorbt_callbacks.py tests/local_quant_research/test_turtle_vectorbt_inputs.py tests/local_quant_research/test_turtle_vectorbt_delayed.py tests/local_quant_research/test_turtle_vectorbt_engine.py tests/local_quant_research/test_turtle_result_adapter.py tests/local_quant_research/test_turtle_strategy_module.py tests/local_quant_research/test_vectorbt_runtime.py tests/local_quant_research/test_turtle_vectorbt_performance.py -m "not real"`

   paths/inputs：见完整JSON同ID条目。

11. **verify.market-unit**：普通守卫；行情、基准、导出与收益的内存契约；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.market-unit tests/local_quant_research/test_market_data_candidate_screen.py tests/local_quant_research/test_market_data_economic_returns.py tests/local_quant_research/test_market_data_query.py tests/local_quant_research/test_market_data_storage.py tests/local_quant_research/test_benchmark_set_contract.py tests/local_quant_research/test_joinquant_export.py -m "not real"`

   paths/inputs：见完整JSON同ID条目。

12. **verify.market-real**：真实未保护；CSV/Parquet/快照的原生持久、冲突和锁保证；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.market-real tests/local_quant_research/test_market_data_candidate_screen.py tests/local_quant_research/test_market_data_economic_returns.py tests/local_quant_research/test_market_data_query.py tests/local_quant_research/test_market_data_storage.py tests/local_quant_research/test_benchmark_set_contract.py tests/local_quant_research/test_joinquant_export.py -m real`

   paths/inputs：见完整JSON同ID条目。

13. **verify.analysis-unit**：普通守卫；标准分析的纯计算、输入和报告契约；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.analysis-unit tests/quant_analysis/test_analysis_plan.py tests/quant_analysis/test_reporting.py tests/quant_analysis/test_standard_analysis_e2e.py tests/quant_analysis/test_statistics.py tests/quant_analysis/test_unified_analysis.py tests/local_quant_research/test_analysis_data_views.py -m "not real"`

   paths/inputs：见完整JSON同ID条目。

14. **verify.analysis-real**：真实未保护；标准分析run/report与真实SQL视图/源完整性；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.analysis-real tests/quant_analysis/test_analysis_plan.py tests/quant_analysis/test_reporting.py tests/quant_analysis/test_standard_analysis_e2e.py tests/quant_analysis/test_statistics.py tests/quant_analysis/test_unified_analysis.py tests/local_quant_research/test_analysis_data_views.py -m real`

   paths/inputs：见完整JSON同ID条目。

15. **verify.quant-equivalence**：真实未保护；三个固定机器私有快照条件复算，缺数据明确skip；检查间可并行。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& set NUMBA_DISABLE_JIT=1&& .\.venv\Scripts\python.exe -m pytest --basetemp=.local/pytest/verify.quant-equivalence tests/local_quant_research/test_local_research_equivalence.py -m real`

   paths/inputs：见完整JSON同ID条目。

16. **verify.worktree-setup**：真实未保护；真实Git/PowerShell共享虚拟环境及缺失/过期恢复；并行批次后串行，内部4工作进程。

   命令：`set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .\.venv\Scripts\python.exe -m pytest -p xdist.plugin --basetemp=.local/pytest/verify.worktree-setup tests/test_setup_worktree.py -m real`

   paths/inputs：见完整JSON同ID条目。

## 写入前尚需实际核验

- 在准确本机量化工作树检查项目Python、Pytest/xdist及命令依赖可用；检查逐项字面路径与通配输入。
- 检查 .build-and-verify 及默认备份目录可写、备份留在repo内；先展示问题、影响及建议。
- 用户最终确认后，才执行裸 build-and-verify init --project <已核实量化工作树> --config <已确认本文件旁JSON> --overwrite；旧配置由工具备份到 .build-and-verify/backups/config-YYYYMMDD-HHMMSS.json，并补齐cache/runs/backups忽略规则；随即按官方契约验证结构。
- 配置变化将选择全部16项，不能声称只运行一个普通项；正式执行次数与是否跑完整模式仍按原授权及门禁处理。

## CI身份来源

BAV开发模式身份来自已实际运行的官方doctor：源码提交0cecf8a48865aab9483cfe21527f58ab2f98c930，实现身份sha256:796c6ff086f5cc7031d1b7a404371a0204242af343bdb98984b61ec7be5b93d8，CLI/包2.7.1。仅同步量化仓库BAV记录及工作流BAV源码锁，MySpec旧身份不变；不提前执行PR、合并或正式complete。Ubuntu工具链快速验证若跳过，只计工具链/结构证据，Windows真实验收仍独立完成。
