# 迁移设计边界与证据（当前增量）

本文件是 change 候选的调查记录，不是正式规格或运行通过报告。基线为 `bd07f9ce5f13d18d0a17831759721d91647a727b`。

## 已确认模型与最小设计

- 普通检查：内存计算/数据及必要静态读取，由新版运行时 guard 实际证明；名字含 unit 或数据源叫 memory 都不是证据。
- 真实检查：不可替代外部环境的显式未保护角色。按领域/场景一核心链，不能将旧矩阵塞进少数入口。DuckDB 即使 :memory: 仍为原生边界；文件 SQLite 防重复消费须真实保留。
- 三类环境是 Python 文件/数据、Chromium/Windows 会话、Git/PowerShell/共享环境，不是三个巨型检查。14 checks=7普通+7真实，后者承接12链。
- 优先已有公开数据接缝、Arrow 内存表/流和数据模型；分析复用既有 _AnalysisLedger/result_contract，不拦截 Path 或重造 SQL/文件系统。两个真实研究 CLI 独立小 repo，其他场景同链复用准备；不为两个 case 多造 session 基线层。
- 外4/工作树内4为已验收起始方案，用户最新正式采用外4/仅工作树内2；浏览器/工作树保留既有串行阶段，长组前置。无新调度器/网格benchmark；旧4×4在47.07秒通过，新4×2仍待契约同步后的正式复验。

## 用户批准的覆盖变化

用户 `Sentinel_5eba966575b48191aa0b9a7d916fb320` 确认整体12链及物理覆盖退让，取代早期18→4、22→11及保留三私有条件复算的中间方案。退休锁竞争、系统重试、链接矩阵、异常回滚、历史格式、重复 CLI 接线；生产保护及有价值内存规则保留，但这些独有物理组合不再逐项实测。最后残留 backtests/111 全树/Parquet 验证按历史矩阵退休，不恢复大历史 inputs。

三条私有全历史重算删除，只保静态夹具结构/摘要连接及已有研究证据，不再自动证明三个历史场景完全一致；无隐藏 skip，无第二手动运行器。历史数据/凭据/证据本身未删除或上传。重复独立 JIT 名称去重，普通风险公式、生产 use_numba=True 和正式编译契约仍保留；本次不新增或声称真实编译验收。

## 代码与工具证据

- 原主干配置 v1、18 checks、outer16、build 空、总预算60仅告警；布局测试硬锁旧清单。五处 DuckDB 顶层导入和包初始化使普通路径提前加载原生库，已按现有连接/查询/存储接口延迟实际导入。
- 原 .pr-flow/toolchain.json 及独立 pr-flow-toolchain.yml 固定旧 BAV 源 ece96f9。已基于官方 doctor 更新 BAV 2.7.1/source0cecf8a/implementationIdentity sha256:796c6ff086f5cc7031d1b7a404371a0204242af343bdb98984b61ec7be5b93d8；MySpec 旧锁未改。
- 本机官方 doctor 已确认 dev 绑定 D:\My Project\my-agent-skills，2.7.1，无 lock/pending；准确量化 remote/main/feature/.venv 已核实。Python3.12.10、pytest9.0.3、xdist3.8、playwright1.61、Chromium1228 存在，正式运行仍按固定提交核对，不以安装身份冒称新实现通过。
- 云端无量化项目 .venv，旧安装工具不能写 v2。本次只用用户明确批准的云端现有 venv/标准库 AST 只读解析检查语法，不 import 业务、不测试、不安装环境。正式 Windows 依赖步骤由父协调本机。

## 已运行的旧版中间证据与修复

- 66d67f03 官方 red：仅布局，12 pass/1 fail，config v1!=2，总8.70秒。
- 官方首次16项发现 real basetemp 父目录缺失、dill gettempdir 探针写被 guard 拒绝、docs 内存HTML缺原“参数/返回”标题。最小修：仅 real 准备仓库内父目录；ordinary 只缓存已存在仓库内 temp 值；恢复原 docs 夹具结构，不放宽提取器。
- 一次支持的官方无 base 脏树诊断定位量化导入探测：ccxt vendored Versioneer 启动 git，urllib3 IPv6 创建 socket。依赖无现成关闭导入选项；普通精确 not real 模拟可选 ccxt 缺失及 socket.has_ipv6=False 并 cleanup，实际外部调用和 guard 不动，真实不应用。不是 dill 或误报，普通不证明这两种宿主能力。
- ff734 旧16项：7普通 guard startup/finish完整且无违规，sync-real通过；总60.14硬截止，旧历史复算被cut、browser/worktree未启动，不能报全部通过。两个量化 E2E 的现有漂移诊断精确发现并发 .build-and-verify/cache JSON 写入；只添加总数/前8相对路径，无内容/失败条件改变。
- 当前两个 E2E 改用独立小真实 repo/.venv junction，生产 _repo_state、缓存可信性与忽略范围均不放宽。新12链进一步精简，不继续优化旧253矩阵。

## 选择和缓存闭包

每 check 的 paths/inputs 包含实际命令读取、配置、公共 conftest、包两层 __init__、领域共享测试数据接缝和静态 schema；不为退休全历史重算保留大历史 cache hash。计算额外精确声明 minimal_strategy、minimal_strategy_b 和 performance-baseline.json。SQL 两来源各生成6张同schema小Parquet，不扫大归档。完整逐项以 config.candidate.json 与 configuration-preview.md 为准。

## 正式规格待交付差异

正式 `myspec/specs/local-quant-research-workflow/spec.md` 的“全部端到端保留”和“60秒只告警”须在 Gate2 增量更新为已批准的每领域核心链及本机60秒总硬截止；云端/PR/CI 保持工具原策略。凡现行规格提到三历史自动精确复算或已退休物理测试矩阵的验收证据，也须明确新的静态证据/核心冒烟边界，不能承诺未运行覆盖。生产能力、JIT编译合同不删。本次尚未直接改正式specs。

## 当前状态与执行门禁

固定实现d683：312个静态函数=300普通+12真实，实际正式14检查全PASS/47.07秒，无cache/skip/未运行，7普通guard完整，详见 verification-evidence.md。这是旧4×4/80bebb、已由6aa官方init应用；当前4×2候选与明确契约已更新，正式目标仍80bebb待本机恢复官方init；旧2d99不再使用。两个研究case改宿主repo内标准库独占短.q目录，先清junction再清owned目录；短路径修后行为通过，但精确OS停滞机制未定论。

旧MySpec会话dd901807已生成完整预览，但其IF96a4绑定旧8e2e输入；当前资源采用会改变树，不能以旧READY应用。待完成：新4×2官方配置/正式复验，合法state-abort保存完整旧预览再state-init --import-aborted建立新IF并重验预览，用户Gate2确认及后续官方应用/交付。三份正式目标为local-quant-research-workflow验证要求、joinquant-archive-sync回归接缝、local-quant-research-runtime历史证据边界；生产正确性/self-test和turtle正式编译合同保留。完整Delta只放.local/spec-work/current/delta，不提前改formal。没有PR/main推送/合并/发布。
