## MODIFIED Requirements

### Requirement: Skill 编排与共用脚本分层
系统 SHALL（必须）由 `run-local-quant-research` Skill（技能）只负责用户意图、流程顺序、输入输出、停止状态和安全边界；配置契约、运行身份、共享行情中心、项目安全调用和证据收口 SHALL（必须）由 `scripts/research/` 下与具体策略解耦的共用脚本实现。

#### Scenario: 有效配置调用策略模块
- **WHEN** 调用者提供通过契约校验的 `snapshot_id`（快照标识）、单场景配置、仓库内 `strategy.root/module/symbol` 和声明输入
- **THEN** Skill 调用共用运行器，运行器按“快照校验、配置校验、固定子进程执行、结果包校验、证据收口”的固定顺序执行并记录每一步状态

#### Scenario: 拒绝不安全策略入口
- **WHEN** 配置缺少必需字段、包含旧 `command/project_entry` 字段、引用仓库外策略或未声明输入
- **THEN** 系统停止在策略代码执行前，只允许固定 `_execute` 子进程命令，不拼接配置命令或扩大路径范围

#### Scenario: 拒绝与分析不兼容的场景标识
- **WHEN** 单场景配置的 `scenario_id`（场景标识）不符合小写字母、数字和连字符组成的 1 至 64 位规则
- **THEN** 运行器在读取行情或启动策略进程前拒绝配置，不发布本地研究结果包

#### Scenario: 通用层不解释策略语义
- **WHEN** 使用非海龟策略模块运行同一流程
- **THEN** Skill、共用脚本和共享行情中心无需海龟资产、参数、信号、风险或报告规则即可完成运行
