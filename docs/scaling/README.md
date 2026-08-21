# Scaling 架构文档

本目录集中维护 NexusCompanion 面向 5000 用户目标的容量扩展、存储迁移和分布式架构设计。

## 文档索引

| 文档 | 定位 | 当前状态 |
| --- | --- | --- |
| [`SCALING_PLAN.md`](./SCALING_PLAN.md) | 架构复审结论、目标架构、Phase 0-6、exit gate、迁移状态机与风险登记 | 架构级 source of truth |
| [`architecture_comparison.md`](./architecture_comparison.md) | 早期“当前架构 vs 目标架构”对照与方案演进背景 | 历史参考，待按主计划同步 |
| [`migration_checklist.md`](./migration_checklist.md) | 早期迁移执行项、命令和回滚清单 | 历史参考，执行前需重切任务 |

## 使用原则

1. 先以代码验证当前行为，再更新架构结论和流程图。
2. 文档冲突时，优先级为：已验证代码与测试证据、`SCALING_PLAN.md`、其余历史设计文档。
3. “分支已存在”不代表 Phase 已完成；完成状态以 `SCALING_PLAN.md` 中的 exit gate 和可复现证据为准。
4. 任务级拆分、实验记录和迁移命令应放入仓库根路径下的 `docs/tasks/`；本目录只维护架构层文档。
5. 更新旧文档时，应同步删除已失效的容量倍数、固定实例数、无依据成本和可无损回滚等假设。
