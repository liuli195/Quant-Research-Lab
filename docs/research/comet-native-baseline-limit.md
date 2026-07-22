# Comet Native 基线快照容量限制调研

- 调研日期：2026-07-23
- 调研对象：本机 `@rpamis/comet@0.4.0-beta.8`
- 上游仓库：<https://github.com/rpamis/comet>
- 结论置信度：高（限制与失败路径有第一方源码和测试设计佐证）；中（“数据仓库适配缺陷”属于基于实现与公开产品承诺的判断，尚无上游定性）

## 结论

**64 MiB 总容量、5 MiB 单文件限制，以及任一条目被省略就拒绝创建 change，都是当前 Comet Native 的有意、fail-closed（失败关闭）设计，不是偶发异常。** 官方源码把限制写成默认常量，将超限文件记录为 `file-size` / `total-size` omission（省略项），又明确要求创建 change 的 baseline（基线）必须 `complete`；否则抛出 `NativeBaselineIncompleteError`，CLI 映射为 `baseline-incomplete`。

但公开文档只宣称“Creation records a complete baseline”，没有公开 5 MiB / 64 MiB 限制、配置入口或针对大文件/数据仓库的排除指南。源码虽然内部 API 接受 `options.limits`，已核查的项目配置、CLI 帮助和 Native 文档没有暴露该设置。因而：

1. **机制与失败策略是官方设计**；
2. **固定默认阈值本身是否为已知缺陷：未找到官方 issue、discussion 或 PR 将其认定为缺陷**；
3. **对合法追踪大文件或累计超过 64 MiB 的数据/模型仓库，这是可复现的适配缺陷（合理判断）**：用户无法通过公开配置提高阈值，也无法创建 change；错误的 `requiredAction: resolve-native-baseline` 没有对应的受支持修复路径。

建议向上游提交 issue，而不是在本仓库绕过或手改 runtime。

## 已证实事实

### 1. 本机安装来源、版本与上游

- `where comet` 指向 `C:\Users\liuli\AppData\Roaming\npm\comet.cmd`；该 shim（命令包装器）执行 `node_modules/@rpamis/comet/bin/comet.js`。
- `comet --version` 输出 `0.4.0-beta.8`。
- 本机包元数据写明：`"name": "@rpamis/comet"`、`"version": "0.4.0-beta.8"`、`"license": "MIT"`。npm registry（注册表）元数据的 `gitHead` 为 `0b3c6bae...`，tarball 为 `https://registry.npmjs.org/@rpamis/comet/-/comet-0.4.0-beta.8.tgz`。
- 官方仓库 README 的图片、CI、roadmap 链接均指向 `rpamis/comet`；GitHub API 显示公开仓库、默认分支 `master`。tag `0.4.0-beta.8` 指向同一提交 `0b3c6bae...`。

第一方来源：

- npm 包页：<https://www.npmjs.com/package/@rpamis/comet/v/0.4.0-beta.8>
- npm registry 版本文档：<https://registry.npmjs.org/@rpamis/comet/0.4.0-beta.8>
- 官方仓库：<https://github.com/rpamis/comet>
- 官方 tag：<https://github.com/rpamis/comet/tree/0.4.0-beta.8>
- 对应提交：<https://github.com/rpamis/comet/commit/0b3c6bae8e68bc6a99123eaf5dd62796a6ec395c>

### 2. 5 MiB / 64 MiB 是源码定义的默认值

官方源码：<https://github.com/rpamis/comet/blob/0.4.0-beta.8/domains/comet-native/native-snapshot.ts#L23-L28>

准确引用：

```ts
export const DEFAULT_NATIVE_SNAPSHOT_LIMITS = {
  maxFiles: 10_000,
  maxFileBytes: 5 * 1024 * 1024,
  maxTotalBytes: 64 * 1024 * 1024,
  maxManifestBytes: 1024 * 1024,
};
```

创建快照时，未传内部 `options.limits` 就使用这些值：<https://github.com/rpamis/comet/blob/0.4.0-beta.8/domains/comet-native/native-snapshot.ts#L2066-L2075>。

准确引用：

```ts
maxFileBytes: options.limits?.maxFileBytes ?? DEFAULT_NATIVE_SNAPSHOT_LIMITS.maxFileBytes,
maxTotalBytes: options.limits?.maxTotalBytes ?? DEFAULT_NATIVE_SNAPSHOT_LIMITS.maxTotalBytes,
```

### 3. 超限会被记录为 omission，完整性因此变为 false

单文件与累计容量判断：

- <https://github.com/rpamis/comet/blob/0.4.0-beta.8/domains/comet-native/native-snapshot.ts#L2198-L2208>
- <https://github.com/rpamis/comet/blob/0.4.0-beta.8/domains/comet-native/native-snapshot.ts#L2305-L2315>

准确引用：

```ts
if (before.size > limits.maxFileBytes) {
  omit({ path: relative, size: before.size, type: 'file', reason: 'file-size' });
  return;
}
if (totalBytes + before.size > limits.maxTotalBytes) {
  omit({ path: relative, size: before.size, type: 'file', reason: 'total-size' });
  return;
}
```

manifest（清单）的完整性直接由 omission 数量决定：<https://github.com/rpamis/comet/blob/0.4.0-beta.8/domains/comet-native/native-snapshot.ts#L2694-L2703>。

准确引用：

```ts
complete: omittedCount === 0,
```

### 4. 创建 change 明确拒绝不完整 baseline

官方源码：

- 错误类型：<https://github.com/rpamis/comet/blob/0.4.0-beta.8/domains/comet-native/native-change.ts#L118-L136>
- 创建与拒绝路径：<https://github.com/rpamis/comet/blob/0.4.0-beta.8/domains/comet-native/native-change.ts#L632-L653>

准确引用：

```ts
const baseline = await createNativeContentSnapshot(options.paths, {
  now: options.now,
  origin: 'change-created',
});
if (!baseline.complete) {
  // ...
  throw new NativeBaselineIncompleteError(...);
}
await writeNativeBaselineManifest(options.paths, state.name, baseline);
```

已安装 runtime 还将此错误映射为 CLI exit code 65、`requiredAction: "resolve-native-baseline"` 和：

```ts
error: { code: "baseline-incomplete", message: error.message }
```

对应官方 tag 源码入口：<https://github.com/rpamis/comet/tree/0.4.0-beta.8/domains/comet-native>。

### 5. 官方文档承诺“完整基线”，但未披露容量阈值

README：<https://github.com/rpamis/comet/blob/0.4.0-beta.8/README.md#L551-L559>

准确引用：

> **Auditable implementation scope** — Creation records a complete baseline, and leaving Build derives a content-addressed implementation scope from before/after snapshots instead of agent claims.

README 也将 Native 描述为 “high-performance, native and recoverable”：<https://github.com/rpamis/comet/blob/0.4.0-beta.8/README.md#L47-L56>。

在已核查的 README、Native `SKILL.md`、`reference/commands.md`、`reference/artifacts.md`、`reference/recovery.md`、CLI help 与 `.comet/config.yaml` schema 中，未找到 `maxFileBytes`、`maxTotalBytes`、5 MiB、64 MiB 或支持的 snapshot limit 配置项。

### 6. 功能很新；来源 PR 没有讨论该限制

Native 功能由 PR #216 合入，标题为：

> feat: ship a recoverable self-contained Native workflow

PR：<https://github.com/rpamis/comet/pull/216>；合并提交：<https://github.com/rpamis/comet/commit/d059ae7a788364cbaf49d43ccfe1c7f41c2a78a7>。

PR 正文强调 “Native change creation” 和 “verification, archiving, ambient resume”，但没有披露 5 MiB / 64 MiB 阈值，也没有把大仓库排除在支持范围外。自动审查因 PR 有 502 个文件而跳过（“Too many files!”）：<https://github.com/rpamis/comet/pull/216#issuecomment-5035832262>。这不能证明限制错误，但说明该大改动没有获得该机器人完整代码审查。

## 合理判断

1. **fail-closed 是安全/可审计设计，而非 bug。** “任一 omission 就不允许建立 change”与官方“complete baseline”承诺一致，避免后续 implementation scope（实施范围）声称覆盖实际未哈希的内容。
2. **阈值选择是资源保护策略。** `maxFiles`、单文件、总字节、manifest 和执行时间均有界，明显用于限制 I/O、内存、运行时间及拒绝服务风险。
3. **产品适配存在缺口。** 对源代码仓库，5/64 MiB 可能常见地足够；对量化数据仓库、ML（机器学习）仓库、Git LFS 未指针化的二进制资产、Parquet/CSV/模型文件，则很容易成为硬阻断。当前公开接口既不能调大，也不能按受信规则排除已追踪数据，因此“安全默认值”变成“无法使用 Native”。
4. **更准确的缺陷分类**：不是“baseline-incomplete 检测错误”，而是“容量策略不可配置、错误恢复不可操作、文档未披露支持边界”。

## 未找到证据

截至调研日期，通过 GitHub API/搜索检查官方仓库全部公开 issues，并检索 issues、PR、discussion 中的 `baseline-incomplete`、`maxFileBytes`、`maxTotalBytes`、`large file`、`snapshot baseline`、`data repository` 等组合：

- 未找到报告相同 5 MiB / 64 MiB 阻断的社区 issue；
- 未找到 maintainer（维护者）把它认定为 bug 或明确说“不支持数据仓库”；
- 未找到调整阈值、暴露配置或加入 Git/LFS 数据排除策略的已开 PR；
- GitHub Discussions 页面未启用/不可用：<https://github.com/rpamis/comet/discussions>；
- 未找到官方设计文档解释为何具体选择 5 MiB 与 64 MiB。

可复核搜索：

- <https://github.com/rpamis/comet/issues?q=%22baseline-incomplete%22>
- <https://github.com/rpamis/comet/issues?q=%22maxFileBytes%22>
- <https://github.com/rpamis/comet/issues?q=%22maxTotalBytes%22>
- <https://github.com/rpamis/comet/issues?q=%22large+file%22>
- <https://github.com/rpamis/comet/issues?q=snapshot+baseline>

“未找到社区反馈”不能证明无人遇到；Native 在 `0.4.0-beta.7` 才公开，调研时仅发布约一天，反馈窗口很短。

## 建议提交的上游 issue

建议标题：

> fix(native): make baseline snapshot limits operable for data and large-file repositories

建议正文最小复现：

1. 安装 `@rpamis/comet@0.4.0-beta.8` 并初始化 Native；
2. 在 Git 跟踪范围内加入一个 `> 5 MiB` 文件，或让符合快照选择规则的文件总量 `> 64 MiB`；
3. 创建 Native change；
4. 实际：exit 65，`error.code = baseline-incomplete`，omission reason 为 `file-size` 或 `total-size`，`requiredAction = resolve-native-baseline`；
5. 期望：至少提供一种受支持且仍可审计的路径。

建议上游按优先级选择最小修复：

1. **文档与错误信息（最低成本）**：公开所有默认限制；错误输出明确指出阈值、超限路径和可用修复，不再只写 `resolve-native-baseline`。
2. **项目级配置**：在 `.comet/config.yaml` 暴露有上限校验的 `native.snapshot_limits`，并将实际 limits 固化进 baseline manifest，保证恢复与审计可重现。
3. **大文件语义**：对 Git LFS 指针按指针内容取证；或提供显式、版本化、默认拒绝的 exclusion（排除）策略，并把排除策略 hash 绑定到 contract/baseline。不要静默跳过文件。
4. **回归测试**：覆盖单文件恰好 5 MiB、超过 1 byte、累计恰好 64 MiB、超过 1 byte，以及配置提高后可完整创建 change。

issue 应询问维护者两个产品决策：

- Native 是否承诺支持含受版本控制数据文件的仓库？
- 如果不支持，官方支持边界和迁移方案是什么；如果支持，配置/排除/LFS 三种路径中哪一种符合其完整基线信任模型？

## 调研方法与边界

- 只读检查本机 CLI shim、npm package metadata、安装技能和生成 runtime；未修改实现或配置。
- 以官方仓库 tag `0.4.0-beta.8` 为引用基线，避免引用随 `master` 漂移的行号。
- 使用 GitHub API 枚举公开 issue、PR 文件、提交历史与评论；使用官方 README/源码为主要证据。
- 本报告本身是用户明确要求的研究产物，存放于仓库既有 `docs/research/` 约定位置。
