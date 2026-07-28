# 问题追踪器：本地 Markdown

本仓库的工作项以 Markdown 文件形式保存在 `openspec/changes/` 下。

## 变更目录结构

每项变更使用以下结构：

```text
openspec/changes/<change-name>/
├── spec.md
└── issues/
    ├── 01-<ticket-name>.md
    ├── 02-<ticket-name>.md
    └── ...
```

需要时，由问题追踪技能创建变更目录。

## 变更名称

变更名称采用以下格式：

```text
YYYY-MM-DD-kebab-case-change-name
```

示例：

```text
2026-07-28-improve-local-research
```

使用当前本地日期作为前缀，后接简短、小写的 kebab-case（短横线命名）描述。创建目录前，必须确认名称符合 `openspec/changes/` 的要求，并且不存在同名目录。

## 规格

当技能要求将规格发布到问题追踪器时，写入：

```text
openspec/changes/<change-name>/spec.md
```

未经确认，不得覆盖已有规格。

## 票据

当技能要求发布票据时，每张票据分别写入：

```text
openspec/changes/<change-name>/issues/
```

票据从 `01` 开始，按照依赖顺序编号；前置票据排在被阻塞票据之前。每张票据都要记录其状态和前置票据。

如果会话中已经确定了相关变更目录，后续技能必须复用该目录；否则根据当前工作创建符合规则的变更名称。

## 问题分流状态

标准问题分流状态直接记录在本地 Markdown 票据中。新发布且可由 Agent（代理）处理的规格和票据使用 `ready-for-agent`。
