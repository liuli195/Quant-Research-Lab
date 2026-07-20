# 核验报告：align-local-research-specs

## 摘要

| 维度 | 结果 |
| --- | --- |
| 完整性 | 3/3 任务完成；1 项修改规格已覆盖 |
| 正确性 | 组合式 E2E（端到端）与 60 秒性能预算语义均有实现和测试证据 |
| 一致性 | OpenSpec（开放规格）设计、差异规格、主规格与历史设计的实现差异说明一致 |

## 证据

- `tests/local_quant_research/test_generic_e2e.py` 先调用 `import_batch`（导入批次）和 `create_snapshot`（创建快照），再以子进程调用公开 `run`（运行）入口。
- `.build-and-verify/config.json` 将 `fullBudgetSeconds`（完整验证预算秒数）设为 60；运行器把超预算写入性能报告和告警，功能状态单独判定。
- 2026-07-20 执行 `.venv\\Scripts\\python.exe -m pytest tests\\test_skill_layout.py tests\\local_quant_research\\test_generic_e2e.py`：16 项通过。
- 2026-07-20 执行 `openspec validate --all --strict`：10 项通过，0 项失败。
- 2026-07-20 执行仓库 build（构建检查）：`build.placeholder` 通过。
- 已检查本次变更，未新增账号、密码、Token（访问令牌）或 Cookie（浏览器凭证）。

## 实现差异处理

历史设计中已过期的 `quick_validate.py`（快速校验脚本）验收描述，已按确认结果补充“实现差异”说明；现行验收以 `openspec/specs/local-quant-research-workflow/spec.md` 为准。

## 结论

未发现 CRITICAL（必须修复）、WARNING（应修复）或 SUGGESTION（建议）问题。变更可进入归档确认。
