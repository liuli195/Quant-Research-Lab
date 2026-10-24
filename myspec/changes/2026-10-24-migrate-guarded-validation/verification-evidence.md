# 已验收4×4历史与当前4×2待验收边界

## 身份与结果

- 仓库：liuli195/Quant-Research-Lab；分支 codex/migrate-quant-guarded-validation。
- 固定验收实现提交：`d6838e126cd86ed455a3b855ee906f3e1fe7b08a`；代码树 `1f7baca21a183498051af47b82ab2cd2b06aa7ba`。基线 `bd07f9ce5f13d18d0a17831759721d91647a727b`。
- 最高公开入口：本机项目 .venv 环境中的裸 `build-and-verify verify`，固定基线、execution-context=local。这是一次正式固定基线运行，不以诊断或单节点替代，也不额外声称执行过另一次 --full。
- 退出码 0，总计 **47.07 秒**，14 项均实际 PASS；无缓存命中、skip、未启动或未运行。7 普通组 guard startup/finish 完整，无违规；7 真实组实际跑完 12 条核心链。
- 版本 2；本机 fullBudgetSeconds=60、enforceLocalBudget=true；外层4，既有工作树内部4，build 空。没有单测3秒限制；本轮未触发60秒硬截止。此前总截止的真实失败证据保留，不因本轮通过改写历史。
- 目标 config 与候选 SHA256 同为 `80bebb691467584e27d83b400885805a88161c5ed8188113d7a4bacb452fd9e6`；由本机官方 init 在 `6aaaf81bbf983b199d103dc4e30d891916646bc4` 写入，备份 `.build-and-verify/backups/config-20261025-000356.json`。精确清单授权：用户 `Sentinel_90e2361f53fc8191913c065be55408f3` 回复完整清单。

## 单项证据

| ID | 角色 | 秒 | 结果 |
|---|---|---:|---|
| verify.quant-real | 真实 | 17.98 | PASS |
| verify.analysis-real | 真实 | 4.62 | PASS |
| verify.quant-calculation | 普通守卫 | 9.30 | PASS |
| verify.sync-real | 真实 | 2.91 | PASS |
| verify.skill-layout | 普通守卫 | 6.91 | PASS |
| verify.sync-unit | 普通守卫 | 1.33 | PASS |
| verify.browser-real | 真实 | 7.12 | PASS |
| verify.docs-unit | 普通守卫 | 1.48 | PASS |
| verify.docs-real | 真实 | 1.86 | PASS |
| verify.quant-unit | 普通守卫 | 7.39 | PASS |
| verify.market-unit | 普通守卫 | 1.75 | PASS |
| verify.market-real | 真实 | 1.58 | PASS |
| verify.analysis-unit | 普通守卫 | 2.53 | PASS |
| verify.worktree-setup | 真实 | 17.09 | PASS |

检查并行，单项耗时不可相加冒充总耗时。完整12链映射/命令/paths/inputs见 configuration-preview.md 和 config.candidate.json。

## 环境与警告

- 本机准确项目 .venv，Python3.12.10、pytest9.0.3、xdist3.8、playwright1.61、Chromium1228；BAV2.7.1开发绑定既有技能源码0cecf8a。未安装或升级业务依赖。
- 只有 quant-calculation 的4条依赖日期别名 d 弃用告警；本轮无 UTF-8 线程解码警告。移除未消费 mklink 输出的 text=True 后没有再出现该警告，不据此倒推此前警告唯一根因已被证明。
- 两个独占 .q 临时repo正常删除；本次 owned 进程无残留；共享 .venv 仍存在；验收前后工作树 clean。
- 原始证据仅保留本机仓库内 `.local/quant-validation-migration/final-c61629eb53bb4dd0885785e8e801611b/`。本文件只记录最小结果，不上传临时日志、私人行情或凭据。

## 已接受的覆盖变化

用户 `Sentinel_5eba966575b48191aa0b9a7d916fb320` 确认从253个旧real函数收敛为12条领域核心链，保持独立检查/缓存。当前静态312测试函数=300普通+12真实；参数化实例另计。

- 可内存化分支迁到已有公开数据/输入接缝、Arrow表和流，包括解析、规则、完整性前置拒绝、结果包复用/冲突、统计/报告计算；本轮实际 guard 已验证，非仅删 real 标记。
- 锁竞争、系统重试、链接矩阵、异常回滚、历史格式及重复CLI接线的独有物理组合退休；生产保护保留，不能宣称物理覆盖完全不变。
- 三条私有全历史精确重算删除，静态历史夹具结构/摘要连接及既有研究证据保留；它们不再自动证明三历史场景完全一致，不新造手动专项入口。
- 生产结果正确性、原子/冲突/暂存区外写保护、生产self-test能力、正式JIT编译契约不删。本次保留公式但未进行真实JIT编译验收。

## 修复与审查边界

- 6aa新版14初轮7普通guard通过，但sync项目根/profile路径拒绝，quant等待子CLI至总截止；browser/worktree未启动，不能算通过。sync改真实隔离项目根而非绕校验。
- 窄诊断证明generic数据准备小于0.085秒，真实子命令存在；深层request266字符、TEMP267字符且启动无输出，但未证明精确OS机制。改宿主repo内独占短目录后，generic一次真实节点6.26秒通过；最终14全部通过。只陈述行为证据，不将长路径假设写成确定机制。
- 独立GPT-6-A基于bd07…d683完成最终限定审查，无剩余P1/P2；多次修复均仅复核相应差异。
- 正式规格前置检查 `git diff --name-only bd07… -- myspec/specs/` 与 `git ls-files --others --exclude-standard -- myspec/specs/` 均空。正式规格尚未修改。
- 后续若只改本change文档，交付文档HEAD会不同于d683；必须明确这一区别。仅文档不改变已验收代码/配置，不为更新证据无价值重跑全14。正式规格和PR交付仍须Gate2。

## 用户追加资源试验与当前采用

用户已明确批准4×2（Sentinel_90c7ae7dfa508191a155b0b9d242a4d0）：outer保持4，仅工作树内部4→2，其他检查、串行阶段与60hard不变。候选6ba9c42fd344cb2883577432d5780fec255e7211336e7b837ff0dadecde2c395及布局契约已更新，正式目标仍80bebb，因本机offline尚未官方写入/正式复验。

| 临时方案 | 总秒 | quant-real | browser-real | worktree | 结果 |
|---|---:|---:|---:|---:|---|
| 4×2 | 42.78 | 18.23 | 4.53 | 15.28 | 13 PASS / 1旧布局契约FAIL |
| 6×2 | 40.75 | 19.05 | 1.83 | 15.12 | 13 PASS / 1旧布局契约FAIL |
| 8×2 | 42.84 | 20.80 | 1.84 | 15.30 | 13 PASS / 1旧布局契约FAIL |

三组都实际完整执行14项且恢复原配置/clean，但不是整体验收通过；browser波动主导部分总差异，不能把改善全归因并发或挑最小数字承诺未来速度。用户选择4×2保持外层较小，仅降低单真实worktree场景的worker准备；不继续网格试验。长单项压缩仅做现有证据/源码ROI调查，不自动扩大生产改动、抽象或测试退役。

当前另做一项等价测试准备去重：make_repo仅一个调用者，原先先写base/dev并add/commit，调用者在首次setup前又覆盖为空并add/commit。现在一开始就空依赖，仅在完整配置准备后保留一次add/commit，省两个git进程与两次覆盖写；四次真实setup和原独立共享/更新/公开验证断言不变。无历史提交内容断言依赖。收益尚未实测，预计只亚秒级，不将其冒称15秒长项主因；不引入缓存/框架或删cold/warm、隔离子进程。
