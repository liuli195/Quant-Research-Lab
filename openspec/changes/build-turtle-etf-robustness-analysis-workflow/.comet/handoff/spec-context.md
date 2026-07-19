# Comet Spec Context

- Change: build-turtle-etf-robustness-analysis-workflow
- Phase: design
- Mode: beta
- Context hash: f874769970bb2b213ef1dc431f05ebb288bf43fd8e30b8000abc8d29f26a0bef

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This beta context pack verbatim-projects spec files and references supporting artifacts by hash, not an agent-authored summary.

## Source References

- Source: openspec/changes/build-turtle-etf-robustness-analysis-workflow/proposal.md
- SHA256: 448505d85105d54406170045d3bb980f2c9ffdccb7c666c0f62fec717ebe9c31
- Source: openspec/changes/build-turtle-etf-robustness-analysis-workflow/design.md
- SHA256: a2d6f9e70706ac3e72a60afb941b6d5cc6187755dc9fac9efae8edad95f84d4c
- Source: openspec/changes/build-turtle-etf-robustness-analysis-workflow/tasks.md
- SHA256: 02b4f89cf0d3f8ef36288d8792f84050ffdb33fee5e4cf4fc742fcfe619662f0
- Source: openspec/changes/build-turtle-etf-robustness-analysis-workflow/specs/standard-strategy-analysis-workflow/spec.md
- SHA256: 4beead3f656330d52c1673ece382a773dd822b17d5d2c61c3251af7dfb5f4b71

## Acceptance Projection

## openspec/changes/build-turtle-etf-robustness-analysis-workflow/specs/standard-strategy-analysis-workflow/spec.md

- Source: openspec/changes/build-turtle-etf-robustness-analysis-workflow/specs/standard-strategy-analysis-workflow/spec.md
- Lines: 1-41
- SHA256: 4beead3f656330d52c1673ece382a773dd822b17d5d2c61c3251af7dfb5f4b71

```md
## ADDED Requirements

### Requirement: 标准分析必须使用显式来源登记
系统 MUST（必须）使用版本化来源登记清单逐项绑定 `scenario_id`、仓库相对路径、声明来源类型、预期清单 SHA256（完整性摘要）和聚宽模拟交易快照身份。分析 MUST 拒绝 `latest`（最新）、目录扫描、推测替代对象、绝对路径和任何摘要、类型或快照不一致的来源。

#### Scenario: 明确登记三类来源
- **WHEN** 调用方为本地研究结果包、聚宽回测归档或聚宽模拟交易根目录提供完整来源登记
- **THEN** 系统验证清单身份与摘要，并只打开登记的对象；模拟交易还必须验证所有核心数据文件位于登记快照目录

#### Scenario: 模拟交易快照漂移
- **WHEN** 聚宽模拟交易根清单、指定快照或已声明数据路径在分析前后不一致
- **THEN** 系统拒绝该来源并输出“证据不足”，不得改用根清单的当前快照或任何历史快照

### Requirement: 三类来源必须复用共同分析事实
系统 MUST（必须）对三类来源复用 `results`（收益）、`balances`（资产）、`positions`（持仓）和 `orders`（订单）四张已对齐的共同事实表及独立基准集。共同绩效、风险和稳健性计算 MUST 只使用这些已验证事实和显式配置，不复制、转换或改写来源归档。

#### Scenario: 本地研究与聚宽来源计算相同共同指标
- **WHEN** 本地研究、聚宽回测和聚宽模拟交易各自提供通过门禁的共同事实表与相同分析配置
- **THEN** 系统使用同一计算入口产生共同指标，并在输出中保留来源类型与核算精度

#### Scenario: 来源不具备物理风险表
- **WHEN** 本地研究来源没有物理 `risk`（风险）或 `period_risks`（分期风险）表
- **THEN** 系统继续完成共同事实表可证明的计算，将官方风险参考标记为 `missing_at_source`（来源缺失），不得生成伪造表

### Requirement: 深度归因和稳健性必须按证据能力降级
系统 MUST（必须）分别验证本地研究归因扩展和聚宽归档 `attribution_log`（归因日志），并仅在其具备所需字段、文件摘要和时间范围时投影为只读归因输入。每项深度归因或稳健性检查 MUST 声明所需能力；来源缺失时 MUST 输出“证据不足”，但不得阻断无依赖的共同分析。

#### Scenario: 聚宽归因数据集可用
- **WHEN** 聚宽来源的 `attribution_log` 文件摘要、字段和时间范围通过验证
- **THEN** 系统输出事件级深度归因，并把归因来源摘要写入交付

#### Scenario: 归因证据缺失
- **WHEN** 来源没有归因扩展或归因日志，或其字段、摘要或时间范围无法验证
- **THEN** 系统保留共同分析，将深度归因及其依赖结论标记为“证据不足”，不得以订单、持仓或后验价格推断事件归因

### Requirement: 分析交付必须可追溯且保持只读
系统 MUST（必须）输出确定性 JSON（结构化数据）与 Markdown（标记文档）报告，包含来源登记摘要、来源类型、模拟快照身份、能力状态、分析配置、脚本版本、证据矩阵和未适用或证据不足项。分析 MUST NOT（不得）启动、提交、同步、恢复或修改本地研究、聚宽回测、聚宽模拟交易及其归档。

#### Scenario: 从真实发布入口完成离线分析
- **WHEN** 调用者从仓库 Skill（技能）入口运行固定三类来源样例
- **THEN** 系统完成共同分析、可用归因、稳健性和报告交付，不访问网络、不调用上游流程，且全部原始来源摘要保持不变

```

Full source files remain canonical. If a required heading or scenario is missing here, regenerate the handoff or read the source spec directly. Supporting files (proposal, design, tasks) are referenced by hash only.