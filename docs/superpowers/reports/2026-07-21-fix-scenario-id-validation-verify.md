# 场景标识校验热修复验证报告

日期：2026-07-21
变更：`fix-scenario-id-validation`（场景标识校验热修复）
基线提交：`67ffcd2`（既有移动止损提交）

## 结论

实现、规格和验证均通过。运行器现在会在读取行情或启动策略进程前，拒绝不符合标准分析规则的 `scenario_id`（场景标识）；没有改变结果包格式、公开命令或历史结果包。

## 需求与实现核对

| 核对项 | 结果 | 证据 |
| --- | --- | --- |
| 提前拒绝不兼容标识 | 通过 | `result_contract.py`（共同结果契约）定义 `SCENARIO_ID_PATTERN`（场景标识正则）和校验函数；`runner.py`（运行器）在配置加载中调用。 |
| 拒绝发生在策略执行前 | 通过 | `load_run_config`（运行配置加载）在声明输入、行情读取和固定子进程启动前抛出 `invalid_scenario_id`（无效场景标识）。 |
| 与标准分析一致 | 通过 | 回归测试直接比较共同契约和分析 JSON Schema（JSON 结构约束）的正则。 |
| 工作流规格 | 通过 | 增量规格新增了“不兼容场景标识必须在读取行情或启动策略前拒绝”的验收场景。 |

OpenSpec（开放规格）任务为 3/3 完成，增量需求为 1/1 覆盖。实现遵循设计：Python（编程语言）运行期复用共同契约，静态 JSON Schema（JSON 结构约束）保留声明式规则，并以回归防止两者漂移。

## 验证证据

| 检查 | 结果 |
| --- | --- |
| 先失败的定向回归 | 修复前 2 项失败：运行器未抛出异常、共同契约缺少正则；修复后 2 项通过。 |
| 相关单元测试 | `tests/local_quant_research/test_runner.py` 与 `tests/quant_analysis/test_analysis_plan.py`：44 通过。 |
| 端到端回归 | `tests/local_quant_research/test_generic_e2e.py` 与 `tests/quant_analysis/test_standard_analysis_e2e.py`：4 通过。 |
| 项目构建 | `build_and_verify.py build --project .`：通过。 |
| 完整验证 | `build_and_verify.py verify --project . --full`：通过，36.7 秒，`full-not-run: false`。 |
| OpenSpec（开放规格）严格校验 | `openspec validate --all --strict --no-interactive`：10/10 通过。 |
| 差异完整性 | `git diff --check`：通过。 |
| 安全复核 | 已审阅本次实现与测试差异，未发现硬编码凭证；命中的敏感词仅为既有配置键拦截列表和测试中的随机变量名。 |

## 审查说明

Comet（工作流）的自动代码审查处于 `review_mode: off`（审查模式关闭）。本次是 2 个源文件、2 个测试文件和 1 个增量规格的低风险热修复：不新增依赖、网络访问、公开接口或结果包字段；因此以严格规格校验、完整验证和人工差异复核作为验证门槛。

Comet（工作流）守卫未能自动识别 Python（编程语言）构建命令。已实际运行项目构建与完整验证；仅在守卫状态推进时使用 `COMET_SKIP_BUILD=1`（跳过守卫重复构建）避免其错误的“未推断构建工具”结果，不代表跳过构建或测试。

## 待处理门槛

分支处理尚待用户决定。当前分支还包含先前单独提交的移动止损改动，因此若创建 Pull Request（拉取请求），其范围会同时包含该提交和本热修复。
