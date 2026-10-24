# 门禁二准备：规格与交付（尚未就绪）

## 核心摘要

实现固定提交 d6838e126cd86ed455a3b855ee906f3e1fe7b08a 已通过本机正式固定基线验证：14 实际 PASS、47.07 秒、7 普通 guard 完整无违规、12 真实核心链全运行，无 cache/skip/未运行。独立 bd07…d683 限定审查无剩余 P1/P2。完整最小证据见 [verification-evidence.md](verification-evidence.md)，配置清单见 [configuration-preview.md](configuration-preview.md)。

目前正式规格未改，未创建本任务 PR、未合并或发布。本文件不是正式 Delta 或完整规格预览，不能据此称 Gate2 ready。必须先由官方 MySpec 2.7.1 完成目标工作树会话、完整冲突记录和全部文件差异，再向用户展示完整门禁并取得明确确认。旧 Gate1/精简/feature 推送批准不能替代 Gate2。

## 规格准备范围（仅修改说明，非 Delta）

预计仅修改三个既有 Requirement，保留原标题，不新增/删除/改名能力或 Requirement；最终数量以官方校验预览为准：

1. local-quant-research-workflow / Skill 结构和通用性验证：普通受保护与每领域/场景最小真实核心链、独立路径选择/缓存、必要静态读取及公开入口；旧“保留全部物理矩阵”改为已批准覆盖边界；本机快/全 60 秒总硬截止，超时终止 owned 进程、停止队列并失败，云端/PR/CI 工具原策略不变。保留 CSV→Parquet→快照→公开研究运行完整主路，内存规则不冒称全部物理实测。
2. joinquant-archive-sync / 端到端回归必须覆盖手动同步入口：常规验收以受控 public sync-active-simulations→幂等→verify→query/export 核心链为准，不再要求重复 Codex/Claude self-test 子进程和逐异常物理矩阵；能在既有数据接缝验证的规则仍普通守卫。生产 self-test 能力、完整性规则与无真实联网边界保留。
3. local-quant-research-runtime / 重构必须保持结果一致并提供性能观测：保留生产结果正确性、既有真实规模/扩展/延迟研究证据和 cold/warm 各 180 秒要求；明确本次默认自动验证不再重算三个私有全历史场景，静态夹具/hash 不能证明其自动精确复算，无第二手动验证入口或隐藏 skip。不删除生产正确性要求，不声称丢失覆盖仍受测。

不修改 turtle-etf-local-research 的正式 Numba 编译契约，也不把当前禁 JIT 公式通过当编译证据。仅处理与本次批准有关的三份规格，不扩为全仓审计。

## 官方会话前置及精确命令

执行地点：已核实的 D:\My Project\Quant-Research-Lab。只用既有 2.7.1 裸 myspec，不换模式、不安装，不调用包内脚本。云端旧 CLI 不可替代。正式 Delta 只能在该工作树 .local/spec-work/current/delta/，不能预写 current、强加锁或提交忽略的规格候选到 feature。

1. 正常拉取本次仅文档 checkpoint，核准确 remote/分支/HEAD/clean；区分“实现验收 HEAD=d683”与“新文档 HEAD”。两配置 SHA 仍80bebb。执行并保存下列两项结果，均须为空：

   git diff --name-only bd07f9ce5f13d18d0a17831759721d91647a727b -- myspec/specs/
   git ls-files --others --exclude-standard -- myspec/specs/

2. 只读核 .local/spec-work/lock 与 current 不存在；若存在立即停报，不覆盖、删锁或按年龄恢复。用当前 Git 对象作为 Agent 证据指纹（官方另行记录并复核实际文件内容指纹），PowerShell 中：

   $W = 'D:\My Project\Quant-Research-Lab\.local\spec-work'
   $S = 'D:\My Project\Quant-Research-Lab\myspec\specs'
   $SF = 'git:' + (git rev-parse 'HEAD:myspec/specs').Trim()
   $IF = 'git:' + (git rev-parse 'HEAD^{tree}').Trim()
   myspec state-init $W add $SF $IF

   SF 绑定全部主规格 Git 树；IF 绑定已选固定实现/配置/测试和已授权证据文档的当前 Git 树。不要把占位值、旧 HEAD 或不相同的本机树当指纹。state-init 结果须回传目标/身份/状态，再继续；这只是合法预览会话，绝非正式应用。

3. state-init 后在 current 内完成三目标分析、保存完整 conflicts.json；已有明确最新授权的正常修改不凭空制造未知冲突，但也不能为省步骤漏列真正未决来源冲突、删除或低可信项。首次展示前必须调用：

   myspec state-set-conflicts $W "$W\current\conflicts.json" $SF $IF
   myspec state-status $W $SF $IF

   若 WAITING_DECISION，只按 state-current 一次显示一条完整候选，保存相应明确决定；不得重新扫描、批量接受或假造零冲突。没有未决后才写 current/delta 三份完整 MODIFIED Requirement。

4. 全部正式候选仅在 current/delta，执行官方校验及独立预览：

   $D = "$W\current\delta"
   $P = "$W\current\preview"
   myspec validate-delta $D $S
   myspec apply-delta $S $D $P $W $SF $IF
   myspec validate-main $P
   myspec diff $S $P
   myspec state-status $W $SF $IF

   此处输出是 preview，不是 $S；严禁把第三参数提前改成正式 specs。保存完整、不截断的 diff，核 READY_TO_APPLY、三目标/需求数、原规格未变和身份/指纹绑定一致。任一步非零即停并保留现场。

## Gate2 必须展示的内容

- 明确当前为“门禁二——规格与交付”；正式三目标摘要、实际 Requirement 操作数量，以及官方完整文件级差异的可读引用。
- d683 固定验收47.07秒、14全跑及guard证据；后续只有change文档不冒称重新验收，不为文档无价值重跑全量。
- 已批准的物理矩阵/三全历史重算覆盖退让、CCXT/IPv6普通宿主能力边界、未真实JIT编译、4条依赖弃用告警；生产保障/self-test/JIT合同不删。
- 准确 PR 动作：建议官方 pr-flow-complete 创建/同步本feature到main的PR，按仓库 Full Verify/CodeQL 与GitHub review gate等待检查，通过后采用配置的 merge 提交方式合并、同步基础分支并非强制清理源分支。保留 primary worktree 实体，不加 --remove-worktree，不强制覆盖历史，不做正式发布。这些动作目前都未授权，必须本次完整Gate2另获确认。
- PR Summary/Scope/Closing References 按既有模板；没有对应GitHub关闭票据时写None，不假造issue编号。当前不手改MySpec旧工具锁；BAV身份维持已核2.7.1/0cecf8a。complete前核官方doctor：若拟更新MySpec身份，须先在Gate2披露.pr-flow/toolchain.json及.github/workflows/pr-flow-toolchain.yml的准确受管变化并获同意，不能以笼统授权替代。
- 官方complete会读取工具身份，并可能自动同步、提交受管工具链文件（不能假定身份变化必然停在写入前）；如需要本机仓库外写入（如技能repo的.git对象）仍须具体位置/用途/影响授权，不能把Gate2笼统当仓库外操作许可。发现未披露身份变化时不得启动complete；分支占用、检查或审查阻塞均按原步骤停止，不跳过门禁。

## 确认后进入的下一步

仅在官方完整preview已生成并展示、用户明确批准本次Gate2后，复核原状态与指纹，按官方my-spec-add将同一Delta原子应用到正式specs并校验；成功后仅提交已批准正式spec（及获批交付文档），核实际diff范围与clean后，再调用已加载pr-flow-complete执行已确认完整交付。complete只可能自动提交受管toolchain，不会代提交正式spec或普通文档。不手写formal、不提前complete/merge/release。当前恢复位置是“本机官方state-init”，不是“可以应用”。
