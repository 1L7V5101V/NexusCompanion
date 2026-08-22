# Scaling 架构文档

本目录集中维护 NexusCompanion 面向 5000 用户目标的容量扩展、存储迁移和分布式架构设计。

## 内容入口

| 入口 | 定位 |
| --- | --- |
| `openspec/specs` | 当前规格：生效规格与治理契约（scaling-governance） |
| `openspec/changes` | 活跃工作：增量设计、tasks 与状态；用 `openspec list` / `openspec status` 查询 |
| `docs/tasks/` 与 `results/` | 历史证据：实施记录、commit、测试脚本、基准 JSON |
| [`archive/`](./archive/) | 历史材料：SCALING_PLAN 快照、架构对照、迁移清单 |

## 使用原则

1. 先以代码验证当前行为，再更新架构结论和流程图。
2. 文档冲突时，source-of-truth 优先级固定为：**已验证代码与测试证据 > openspec/specs > openspec/changes（含 archive）> 历史材料**。
3. 已完成状态只能由合并进 `main` 的 commit 或可复现证据支撑；“分支已存在”不代表 Phase 已完成。
4. 任务级拆分、实验记录和迁移命令放入 `docs/tasks/` 或 OpenSpec changes；本目录只维护架构层文档。
5. 更新旧文档时，应同步删除已失效的容量倍数、固定实例数、无依据成本和可无损回滚等假设。
6. 历史参考文档（archive/）保留审计价值，不删除；阅读时以其头部标注的更高优先级文档为准。
