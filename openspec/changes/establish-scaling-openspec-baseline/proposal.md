## Why

Scaling 文档体系先于 OpenSpec 存在，`docs/scaling/` 与 `docs/tasks/phase1-storage/` 把架构设想、长期 roadmap、Phase 状态和已执行的里程碑记录混在同一组文件里，靠手动同步维持一致。最初方案保留 `docs/scaling/SCALING_PLAN.md` 作为活跃的架构级 source of truth，只把任务级内容移交 OpenSpec；但审阅发现该方案仍会产生双轨状态：SCALING_PLAN 内部存在 5 处以上过期或互相矛盾的状态表述，其中"当前结论"段落仍称 Phase 1 只完成"后端兼容接入的一部分"，与同文件已修正的"Phase 1 已合并进 main（`0a83314d`，current-state）"直接冲突。人工维护多份活跃文档必然持续漂移。因此废弃旧方案，改为：**OpenSpec 成为 Scaling 唯一规范和开发状态管理入口，SCALING_PLAN 冻结为历史快照**。

## What Changes

- **SCALING_PLAN 原正文历史化**：完整正文保存为历史快照 `docs/scaling/archive/SCALING_PLAN-2026-08-22.md`，头部注明冻结日期、可能含跨时期积累的过期或内部矛盾、不再修正与维护、不作为当前状态或规格依据。
- **原路径只保留废弃说明与 OpenSpec 导航**：`docs/scaling/SCALING_PLAN.md` 改为 tombstone/navigation 文件，仅指向 openspec/specs、openspec/changes、历史快照、docs/tasks、results 与 `openspec list`/`openspec status` 查询命令。
- **current-state 进入 openspec/specs**：当前已生效规格、架构不变量与治理契约以 main specs 为准。
- **active work 进入 openspec/changes**：正在设计或实施的增量以活跃 change 的 design、tasks 与验收条件为准。
- **completed work 进入 openspec/changes/archive**：已完成 change 的规格演进历史以 change 归档为准。
- **docs/tasks 与 results 只保留 evidence**：历史实施记录、benchmark、迁移报告与机器可读证据；不管理当前进度，不作为规范 source of truth。
- **不再维护人工 Phase 完成时间表**：SCALING_PLAN 中的 Phase/exit gate 时间表退役，不建立新的长期滚动 roadmap 文件。
- **当前 change 状态只由 `openspec status` 与 tasks checkbox 表达**。
- **capability 更名**：`scaling-docs-baseline` → `scaling-governance`（main specs 尚未 sync，更名无迁移成本）。

## Non-Goals

- 不把 SCALING_PLAN 的全部未来设想复制进 main specs。
- 不把尚未实现的 TurnAdmission、outbox、多 Worker、WebChat 公网生产、Redis cache、PostgreSQL production primary 等描述成当前能力。
- 不创建永久存在的"总 roadmap change"；未开始的未来工作不提前创建永久 active change。
- 不修改应用代码。
- 不删除历史证据。

## Capabilities

### New Capabilities

- `scaling-governance`: Scaling 文档体系与 OpenSpec 之间的治理契约——OpenSpec 作为唯一规范与开发状态管理入口、current/future-state 分离、active change 管理规则、change/requirement/task/evidence 可追踪、apply→review→sync-specs→archive 生命周期、SCALING_PLAN 历史快照退役规则、docs/tasks/results 的 evidence-only 角色。不描述运行时行为，描述文档系统必须满足的组织与一致性约束。

### Modified Capabilities

（无。`openspec/specs/` 当前为空，不存在既有 capability 需要修改。）

## Impact

- **文档**：`docs/scaling/SCALING_PLAN.md` 冻结为历史快照并改为 tombstone；`docs/scaling/README.md` 移除对 SCALING_PLAN 的活跃 source of truth 表述；`docs/scaling/archive/` 历史文档 banner 由"以 SCALING_PLAN 为准"改为指向 OpenSpec。
- **配置**：`openspec/config.yaml` 更新 context 与 rules（移除对 SCALING_PLAN 的活跃引用、"roadmap 状态更新点"改为 OpenSpec change 状态更新；apply 阶段执行）。
- **OpenSpec**：建立 capability spec（`specs/scaling-governance/spec.md`）。
- **代码**：不修改任何应用代码；不触碰 `_passive_runtime_lock`、Phase 0、PostgreSQL migration、TurnAdmission、inbox/outbox、多 Worker、WebChat、Redis。
- **测试证据**：`results/m4h4_partition_provisioning.json` 与 `docs/tasks/phase1-storage/tests/` 引用关系被记录，保留为历史证据，不重跑不改数据。
