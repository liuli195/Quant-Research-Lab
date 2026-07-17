## Why

当前 `verify --full`（全量验证）约需 221–242 秒，主要时间消耗在重复启动 pytest（测试运行器）、嵌套并行和 Numba JIT（即时编译），无法满足本仓库 30 秒内完成全量验证的目标。现有测试已经具备完整覆盖，优化应只改变本地验证编排，不减少测试或改变产品行为。

## What Changes

- 合并零碎 pytest 入口并消除检查级并行与 pytest-xdist（测试并行）的嵌套过度并发。
- 对不验证 JIT 编译本身的测试关闭 Numba JIT，并保留最小真实 JIT 校验。
- 让 `.py_func` 测试在关闭 JIT 时仍验证同一 Python 实现，不跳过任何测试。
- 仅在共享状态隔离成立时并行运行 E2E（端到端）检查。
- 向量化等价性回归中的连续价格派生，并消除现金二分对相同整数目标的重复计算，保持冻结结果不变。
- 用非缓存、连续的 `verify --full` 运行证明总耗时稳定不超过 30 秒。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `local-quant-research-workflow`：补充仓库全量验证必须保留全部测试、完全离线并在 30 秒内完成的验收要求。

## Impact

- 影响 `.build-and-verify/config.json`（构建与验证配置）及其配置契约测试，最小调整直接访问 Numba dispatcher（调度器）的测试辅助代码，并允许受限子进程继承 Numba 官方禁用 JIT 环境变量；同时优化连续价格和现金可行性计算热点，由冻结等价性夹具约束输出不变。
- 不修改上游 build-and-verify 运行时，不新增依赖，不减少测试，不联网调用外部系统。
- 测试只在本机主进程或子进程内执行；临时文件和本机进程间隔离仍被允许。
