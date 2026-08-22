# NexusCompanion Scaling 治理

本目录（`openspec/`）是 Scaling Program 的唯一维护目录：当前规格、活跃变更、已完成变更、Program 目标、历史材料、任务记录与证据全部在此。

## 内容入口

| 入口 | 定位 |
| --- | --- |
| [`PROJECT_CHECKLIST.md`](./PROJECT_CHECKLIST.md) | 完整项目进度观测 checklist：phase/capability 展开为子任务条目，只含简短描述与链接 |
| [`SCALING_ROADMAP.md`](./SCALING_ROADMAP.md) | Program 目标：North Star、capability map、依赖与状态（不承载实现任务与逐 commit 历史） |
| [`specs/`](./specs/) | 当前规格：生效规格与治理契约（scaling-governance） |
| [`changes/`](./changes/) | 活跃工作：增量设计、tasks 与状态；用 `openspec list` / `openspec status` 查询 |
| [`changes/archive/`](./changes/archive/) | 已完成变更历史 |
| [`records/`](./records/) | 已完成任务记录：实施叙述、验证方法与 gate 结论 |
| [`evidence/`](./evidence/) | 可复现证据：测试/基准脚本与原始结果数据 |
| [`archive/`](./archive/) | 历史材料：SCALING_PLAN 快照与 tombstone、架构对照、迁移清单 |

## 使用原则

1. 先以代码验证当前行为，再更新架构结论和流程图。
2. 文档冲突时，source-of-truth 优先级固定为：**已验证代码与测试证据 > openspec/specs > openspec/changes（含 archive）> 历史材料**；Program 目标以 `openspec/SCALING_ROADMAP.md` 为准。
3. 已完成状态只能由合并进 `main` 的 commit 或可复现证据支撑；“分支已存在”不代表 Phase 已完成。
4. 任务级拆分、实验记录和迁移命令放入 OpenSpec changes；已完成任务的叙述写入 `openspec/records/`，可复现脚本与原始结果写入 `openspec/evidence/`。
5. 更新旧文档时，应同步删除已失效的容量倍数、固定实例数、无依据成本和可无损回滚等假设。
6. 历史参考文档（`archive/`）保留审计价值，不删除；阅读时以其头部标注的更高优先级文档为准。
