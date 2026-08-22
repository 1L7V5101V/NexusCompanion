## Why

Scaling 文档体系先于 OpenSpec 存在，`docs/scaling/` 与 `docs/tasks/phase1-storage/` 把架构设想、长期 roadmap、Phase 状态和已执行的里程碑记录混在同一组文件里，靠手动同步维持一致。`docs/scaling/SCALING_PLAN.md` 同时承担架构结论、长期 roadmap、任务拆分、完成时间线与历史状态，内部存在 5 处以上过期或互相矛盾的状态表述（其中"当前结论"段落仍称 Phase 1 只完成"后端兼容接入的一部分"，与已合并进 main 的 `0a83314d` current-state 直接冲突）。继续维护这份混合文档必然持续漂移。

本 change 的治理模型修正为**三类事实来源**，各自承担单一职责，避免任何文档同时管理多类信息：

1. **OpenSpec**：`openspec/specs/` 承载当前生效的系统能力和治理契约；`openspec/changes/` 承载当前正在设计或实施的独立变更；`openspec/changes/archive/` 承载已完成变更历史。
2. **Scaling Roadmap**：`openspec/SCALING_ROADMAP.md` 作为 program-level tracker，承载整个 Scaling Program 的 North Star、可度量目标、capability map、依赖、当前 focus、阻塞项与 exit evidence；不承载 implementation task、逐 commit 记录或手动完成 checkbox，不作为当前系统行为的规范 source of truth。
3. **Code / tests / results**：代码与测试是实现事实；`openspec/records/`（已完成任务记录）与 `openspec/evidence/`（可复现脚本与原始结果）是实施、迁移与 benchmark 证据；不承担当前项目进度管理。

## What Changes

- **OpenSpec 管理当前规范和独立变更**：current-state 进入 openspec/specs，active work 进入 openspec/changes，已完成变更进入 openspec/changes/archive。
- **SCALING_ROADMAP 管理整个 Scaling Program 的目标和依赖**：新建 `openspec/SCALING_ROADMAP.md`，只跟踪 program-level outcome、capability map、依赖、状态与 exit evidence。
- **SCALING_PLAN 冻结为历史快照**：完整正文保存为 `openspec/archive/SCALING_PLAN-2026-08-22.md`，tombstone/navigation 页 `openspec/archive/SCALING_PLAN.md` 与快照同目录。
- **建立 capability → change → evidence 的追踪闭环**：每个 capability 可关联到 OpenSpec change，change 的 task 携带验证命令与证据路径；verified 状态必须有 merge/test/benchmark 证据。
- **不再使用一份文档同时管理架构、任务、完成状态和历史**：SCALING_PLAN 退役后，规范、进度、目标、证据各归其位，冲突裁决无需逐次讨论。
- **实施证据与任务记录并入 openspec**：已完成任务记录迁入 `openspec/records/`，可复现脚本与原始结果迁入 `openspec/evidence/`；`docs/scaling/`、`docs/tasks/`、`results/` 退役，openspec 成为唯一维护目录。

## Non-Goals

- 不实现 Scaling 功能。
- 不实现 PostgreSQL migration。
- 不实现 TurnAdmission。
- 不实现 inbox/outbox。
- 不实现多 Worker。
- 不实现 WebChat。
- 不实现 Redis。
- 不把完整 roadmap 复制进 OpenSpec current-state specs。
- 不创建永久的 roadmap change（`scaling-roadmap` 不作为永久 OpenSpec change 存在）。
- 不逐项修复历史快照正文中的过期或矛盾表述。
- 不删除历史证据。
- 不修改应用代码。

## Capabilities

### New Capabilities

- `scaling-governance`: Scaling 文档体系与 OpenSpec、Scaling Roadmap 之间的治理契约——三类事实来源（OpenSpec / SCALING_ROADMAP / 代码·测试·证据）各自职责、capability → change → evidence 追踪闭环、active change 管理规则、verified 状态证据要求、apply→review→sync-specs→archive 生命周期、SCALING_PLAN 历史快照退役规则、openspec/records 与 openspec/evidence 的 evidence-only 角色。不描述运行时行为，描述文档与项目治理系统必须满足的组织与一致性约束。

### Modified Capabilities

（无。`openspec/specs/` 当前为空，不存在既有 capability 需要修改。）

## Impact

- **文档**：新建 `openspec/SCALING_ROADMAP.md`；`openspec/archive/SCALING_PLAN.md` 为 tombstone（与快照同目录，指向 SCALING_ROADMAP 与 OpenSpec），正文冻结为 `openspec/archive/SCALING_PLAN-2026-08-22.md`；`openspec/README.md` 提供入口与使用原则；`openspec/archive/` 历史文档 banner 指向 OpenSpec；`docs/scaling/`、`docs/tasks/`、`results/` 退役，任务记录与证据迁入 `openspec/records/` 与 `openspec/evidence/`。
- **配置**：`openspec/config.yaml` 更新 context 与 rules（context 指向 openspec/specs 与 `openspec/SCALING_ROADMAP.md`；"roadmap 状态更新点"改为 OpenSpec change 状态与 SCALING_ROADMAP 状态更新；apply 阶段执行）。
- **OpenSpec**：建立 capability spec（`specs/scaling-governance/spec.md`）。
- **代码**：不修改任何应用代码；不触碰 `_passive_runtime_lock`、Phase 0、PostgreSQL migration、TurnAdmission、inbox/outbox、多 Worker、WebChat、Redis。
- **测试证据**：`openspec/evidence/phase1-storage/results/m4h4_partition_provisioning.json` 与 `openspec/evidence/phase1-storage/benchmark/` 引用关系被记录，保留为历史证据，不重跑不改数据。
