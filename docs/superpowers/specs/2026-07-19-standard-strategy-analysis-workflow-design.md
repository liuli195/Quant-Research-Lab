---
comet_change: build-turtle-etf-robustness-analysis-workflow
role: technical-design
canonical_spec: openspec
status: superseded
superseded-by: standard-result-package-handoff
---

# 标准结果包交接设计

原设计把来源类型、目录位置、快照和重复摘要放入一层登记协议。这层协议已被删除，因为标准结果包本身已经能够证明身份、冻结参数和完整性。

量化分析现在只接收三类显式输入：一个或多个标准结果包、分析计划、独立基准清单。结果包由谁产生、位于哪个目录、使用什么文件名，都不参与分析身份或计算分支。

结果包提供四类共同事实和可选扩展。共同计算只读四类事实；归因按 `time`（时间）、`event_id`（事件标识）和 `event_type`（事件类型）识别，不依赖扩展名称。缺少可选能力时只降级对应结论。

分析交付记录内容摘要、计划摘要、基准摘要、公式版本和证据矩阵。路径仅用于本次打开文件，不写入分析身份。分析全程离线、只读，不启动任何上游流程。
