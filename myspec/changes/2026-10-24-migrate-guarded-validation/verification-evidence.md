# 当前4×2正式验收与一次长项诊断

## 当前正式验收

- 仓库liuli195/Quant-Research-Lab；分支codex/migrate-quant-guarded-validation；基线bd07f9ce5f13d18d0a17831759721d91647a727b。
- 固定实现HEAD：`2f5399a0baa4db959725008bd467eb6f26bd1c11`，树`7753cd54f05a3b33a36e8b1d3bac1c8258b5656a`。裸build-and-verify verify、本机项目.venv、固定基线、execution-context=local；不额外声称又执行一次--full。
- 退出0；工具报告总 **42.04秒**，外层命令墙钟 **42.13秒**。14项全部实际PASS，无cache/skip/未运行；7普通guard startup/finish完整且无违规，7真实组实际跑完12核心链。
- 当前资源：outer4，仅工作树内部2，浏览器/工作树保留既有串行阶段；build空、插件禁自动加载。60秒本机总硬截止开启，无单测3秒；本轮未触发硬截止。
- 正式目标与候选SHA256均为`6ba9c42fd344cb2883577432d5780fec255e7211336e7b837ff0dadecde2c395`；本机官方init仅把worker4改2，备份`.build-and-verify/backups/config-20261025-014335.json`。用户最新采用授权为Sentinel_90c7ae7dfa508191a155b0b9d242a4d0。
- Python3.12.10、pytest9.0.3、xdist3.8、playwright1.61、Chromium1228及既有BAV2.7.1/dev源0cecf8a；无业务依赖安装或升级。
- 4条既有依赖日期别名d弃用告警；本轮无报告的UTF8解码告警。owned进程核无残留，已记录.qg4dr9v72已清理；这不是全瞬时路径连续审计，不夸大为额外证明。验收前后clean。
- 原始证据仅在本机仓库`.local/quant-validation-migration/accepted-b041204919fd4b6d9fa33d5d2210c25e/`，不把大量临时日志、私人行情或凭据上传。

| ID | 角色 | 秒 | 结果 |
|---|---|---:|---|
| verify.quant-real | 真实 | 18.73 | PASS |
| verify.analysis-real | 真实 | 4.83 | PASS |
| verify.quant-calculation | 普通守卫 | 10.00 | PASS |
| verify.sync-real | 真实 | 3.05 | PASS |
| verify.skill-layout | 普通守卫 | 8.45 | PASS |
| verify.sync-unit | 普通守卫 | 1.34 | PASS |
| verify.browser-real | 真实 | 2.67 | PASS |
| verify.docs-unit | 普通守卫 | 1.58 | PASS |
| verify.docs-real | 真实 | 1.86 | PASS |
| verify.quant-unit | 普通守卫 | 7.69 | PASS |
| verify.market-unit | 普通守卫 | 1.80 | PASS |
| verify.market-real | 真实 | 1.56 | PASS |
| verify.analysis-unit | 普通守卫 | 2.73 | PASS |
| verify.worktree-setup | 真实 | 15.75 | PASS |

检查并行，单项不能求和当总耗时。12链/命令/paths/inputs见configuration-preview.md。

## 已批准覆盖变化与不变保障

用户Sentinel_5eba966575b48191aa0b9a7d916fb320确认从253旧real函数收敛为12条领域核心链，独立选择/缓存。静态312函数=300普通+12真实，参数化实例另计。

- 可内存化解析、契约、计算、门禁、结果包复用/冲突和报告规则用已有公开数据接缝、Arrow表/流验证；本轮有实际guard证据，不只是删real标记。
- 锁竞争、系统重试、链接矩阵、异常回滚、历史格式及重复CLI接线的独有物理组合按批准退休，不宣称覆盖完全不变。三私有全历史精确复算删除，保静态历史夹具结构/hash连接和既有研究证据，无隐藏skip或第二手动专项入口。
- 生产正确性、原子/冲突/暂存区外写保护、self-test公开能力、正式JIT编译合同保留。普通仅模拟可选CCXT/IPv6宿主能力不可用，不改socket/Popen/guard，不证明这两项宿主能力；真实不应用。当前禁JIT公式通过不代表实际编译。
- 工作树make_repo唯一调用前原双写依赖/双提交改为初始空依赖且完整配置后一次提交，保4次真实setup和全部断言；确定减少2个git进程/2次覆盖写，但单次噪声不能证明节省多少秒，不将其当15秒大头。

## 历史轨迹（不是当前配置验收）

- 66d67f03官方red：布局版本1!=2，12pass/1fail，总8.70秒。
- d6838e1旧4×4/80bebb：正式14全实际PASS，47.07秒，7guard完整；保留该证据，不移作当前新配置通过。
- 深层隔离repo曾使generic子CLI等待至60硬切；数据准备<0.085秒，旧request266/TEMP267字符。改短独占.q目录后generic一次真实节点6.26秒通过；精确OS机制未定论。

| 临时试验 | 总秒 | quant-real | browser-real | worktree | 结果 |
|---|---:|---:|---:|---:|---|
| 4×2 | 42.78 | 18.23 | 4.53 | 15.28 | 13PASS/1旧布局契约FAIL |
| 6×2 | 40.75 | 19.05 | 1.83 | 15.12 | 13PASS/1旧布局契约FAIL |
| 8×2 | 42.84 | 20.80 | 1.84 | 15.30 | 13PASS/1旧布局契约FAIL |

三组均完整执行14项并恢复，不是整体验收。browser波动主导部分总差，不能把约9%或当前总改善全归因并发，也不据最小数字继续网格。用户选择4×2后，配置/契约同步才取得本文件首节的新正式全绿。

## 一次长项诊断：只定位，不代正式验收

在同一2f5399实现上，仅临时改generic/turtle/worktree三个测试文件，记录既有helper与performance数据，不改生产/guard/配置/60hard。官方无base dirtyfast实际3checks（不是2）：quant-real15.66秒、skill-layout4.50秒、worktree14.98秒，全pass，总33.94秒；无其他全量重跑。源3files/env已按原bytes恢复、clean；本次owned/.q记录已清理。

| 已记录阶段 | 秒 |
|---|---:|
| generic公开run墙钟 | 5.150288 |
| turtle公开run墙钟 | 5.472226 |
| turtle公开promote墙钟 | 0.966664 |
| generic既有cold/warm口径 | 0.066391 / 0.065296 |
| turtle既有cold/warm口径 | 0.217283 / 0.212915 |
| generic/turtle落盘阶段 | 0.004794 / 0.015438 |
| generic/turtle读取校验阶段 | 0.005657 / 0.009782 |
| worktree首次main setup | 7.695189 |
| worktree共享setup | 0.506540 |
| worktree正常main更新 | 3.319228 |
| worktree再次共享setup | 0.536633 |
| worktree内官方smoke | 0.900675 |

Git init0.093013、config mail0.064629/name0.064833、add0.079007、commit0.147420、worktree add0.171529秒。四次setup分别承担初始化、共享、正常更新、再次共享，不删其中保障；首次main内部未进一步拆venv/pip，不能把7.70秒全部归pip。cold/warm和stages是既有性能证据口径，可能包含共享阶段，不能直接求和与CLI墙钟相减后全归因导入；后者还含解释器/子进程、冻结/校验与其他准备。

结论：已观察纯计算/落盘小、真实入口和环境准备占主要墙钟；没有证明可直接删除的高ROI重复核心步骤。保子进程隔离、cold/warm一致性和4次工作树真实阶段；不为几秒新增session缓存/框架、共享宿主runtime cache或削保障，按当前42.13秒方案收口。不承诺未来机器或运行恒定同速。原始诊断仅在`.local/quant-validation-migration/breakdown-cc1b7a7ee31e4875b7e8dff89f21cadc/`的两个PID JSONL及verification.log。

## 交付边界

独立审查已覆盖bd07…d683及后续短小4×2/准备去重差异，无剩余P1/P2；正式spec的tracked/untracked两项检查均空。后续只文档HEAD不等于重新测试，验收实现仍2f5399，不为文档无价值重跑全14。原MySpec会话已官方ABORTED完整归档；新文档树固定后import-aborted重建完整预览，再展示Gate2。尚未formal apply、PR complete、main合并或发布。
