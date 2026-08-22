## Context

现状（详见 proposal.md - Why 与 explore 调查结论）：`docs/scaling/` 混装架构设想、长期 roadmap、Phase 状态（SCALING_PLAN.md）、已自标历史的对照（architecture_comparison.md）、已自标历史的执行清单（migration_checklist.md）；`docs/tasks/phase1-storage/` 是已完成的里程碑证据族（m4.5、m4h-2/3/4、vector-validation）；`results/` 存基准原始数据。OpenSpec 已初始化，`openspec/specs/` 为空（sync-specs 尚未运行）；config.yaml 已写入 context/rules/operations（早期 apply 产物，含"roadmap 状态更新点""见 docs/scaling/SCALING_PLAN.md"等将被替代的表述）。Phase 1 storage foundation 已 merge（0a83314d），但 SCALING_PLAN 头部"当前结论"仍称 Phase 1 只完成"后端兼容接入的一部分"，与已修正的 Phase 1 current-state 状态直接冲突；审阅共发现 5 处以上过期或矛盾表述。

旧方案（本 change 原 design 的 D3）"SCALING_PLAN 职责收缩、保留为活跃架构层 source of truth、只修正状态段"经审阅被否决：即使只把任务级内容移出，SCALING_PLAN 仍会同时承担架构结论、长期 roadmap 与 Phase 状态，人工同步负担与双轨漂移无法消除。本设计改为将 SCALING_PLAN 整体冻结为历史快照，OpenSpec 成为唯一规范状态管理入口。

本 change 不引入新运行时能力，全部产物是文档组织与治理契约，因此"实现"即：更新 delta spec 与 config.yaml 规则、`git mv` SCALING_PLAN 到 archive 并写历史块、原路径写 tombstone、更新 README 与历史文档 banner、跑 `openspec validate`。

新 source-of-truth 层级（本 change 后生效，四个 artifact 使用完全一致表述）：

1. 已验证代码和测试证据：实现事实
2. openspec/specs：当前生效规格和治理契约
3. openspec/changes：活跃增量、设计、tasks 和状态
4. openspec/changes/archive：已完成变更历史
5. docs/tasks 和 results：历史证据
6. docs/scaling/archive：历史设计材料

SCALING_PLAN 不再出现在活跃 source-of-truth 层级中。

## Goals / Non-Goals

**Goals:**
- 让 OpenSpec 成为 Scaling 唯一规范与开发状态管理入口，冲突裁决无需逐次讨论。
- 让 SCALING_PLAN 作为历史快照退役，不再维护 Phase 状态与 exit gate checkbox。
- 让 current-state 只由 main specs（openspec/specs）承载，future-state 只由活跃 change 的 delta spec 承载。
- 让已完成变更进入 openspec/changes/archive，docs/tasks 与 results 只保留 evidence。
- 让后续 Phase change（1B/1C、Phase 2+）有固定的生命周期顺序：apply → review → sync-specs → archive。

**Non-Goals:**
- 不修改应用代码、不触碰 `_passive_runtime_lock`。
- 不实现 Phase 0 / TurnAdmission / inbox/outbox / 多 Worker / WebChat / Redis / PostgreSQL primary 切换。
- 不逐项修复历史快照正文中的过期或矛盾表述（历史快照原样保存，只加历史块）。
- 不把 SCALING_PLAN 的未来设想复制进 main specs。
- 不删除任何证据文件。
- 本 update 阶段不落盘 config.yaml / docs 改动（那是 apply 阶段 tasks）。

## Decisions

### D1: 旧文档历史化位置 = `docs/scaling/archive/`（原地降级 + 头部标记）【修订】

过时文档移动到 `docs/scaling/archive/`，头部加"历史参考"状态块。**修订点**：这些文档头部不得再写"以 SCALING_PLAN 为准"，改为"当前规格以 openspec/specs 为准；当前 change 状态以 openspec/changes 与 `openspec status` 为准；实现事实以代码和测试证据为准"。

- **备选 A：移入 `openspec/changes/archive/`** — 拒绝。那是 OpenSpec 生命周期归档（change 完成后的封闭记录），把通用文档历史混入会破坏 change 归档语义。
- **备选 B：直接删除** — 拒绝。丢失决策演进历史，与"降级为版本历史"目标冲突。
- **备选 C：原地保留仅加头部标记** — 部分采用：与移动结合（archive/ 子目录 + 标记双保险），让 `docs/scaling/` 根目录只留 tombstone 与 README。

理由：git 已保留历史版本，archive/ 目录是当前 tree 里对"历史"的显式表达；README 索引同步更新后，读者从入口就知道哪些是活跃、哪些是历史。

### D2: config.yaml 治理规则结构【修订】

apply 阶段更新 `openspec/config.yaml`：

- `context`：移除"扩展目标 5000 用户（见 docs/scaling/SCALING_PLAN.md）"的活跃引用，5000 用户目标改为指向 openspec/specs（scaling-governance）。
- `rules.tasks`：把"roadmap 状态更新点"替换为"openspec change 状态更新（tasks checkbox / `openspec status`）"；管理闭环元素改为 branch/worktree + 测试证据 + openspec change 状态更新。
- `operations`：apply/archive guidance 中的"roadmap 状态"表述同步改为 OpenSpec 状态。

- **备选：把全部约定写进 SCALING_PLAN** — 拒绝。SCALING_PLAN 已退役；config.yaml 是 OpenSpec 读取的权威入口，AI 写 artifact 时自动获得约束。
- 关联：specs `scaling-governance` 的治理约束由本 D2 与 delta spec 共同落地。

### D3（替代旧 D3）: SCALING_PLAN 冻结并退役，OpenSpec 为唯一规范状态管理入口

旧 D3"SCALING_PLAN 职责收缩，不重构全文"被废弃。SCALING_PLAN 不再作为活跃 source of truth。迁移方式：

1. **完整正文保存为** `docs/scaling/archive/SCALING_PLAN-2026-08-22.md`（`git mv`）。
2. **历史快照头部**明确写明：冻结日期为 2026-08-22；文件可能包含跨时期积累的过期或内部矛盾状态；不再修正、不再维护；不作为当前状态或规格依据。
3. **原路径** `docs/scaling/SCALING_PLAN.md` 改成简短 tombstone/navigation 文件，仅指向：openspec/specs/、openspec/changes/、历史快照、docs/tasks/、results/、`openspec list`/`openspec status` 查询命令。
4. **docs/scaling/README.md** 不再把 SCALING_PLAN 列为活跃 source of truth。
5. **docs/scaling/archive/ 历史文档**头部不再写"以 SCALING_PLAN 为准"，改为指向 openspec/specs、openspec/changes + `openspec status`、代码/测试证据。
6. **docs/tasks/phase1-storage** 保留为历史实施证据，不再要求同步当前状态。
7. **不建立新的长期滚动 roadmap 文件**。未开始的未来工作不提前创建永久 active change；当工作进入正式设计或实施时，才创建独立 OpenSpec change。
8. **稳定的跨 change 约束进入 scaling-governance capability spec**，例如：current-state 与 future-state 必须分开；每个实现目标对应独立 change；同时最多一个 implementation change；requirement、task 和 evidence 可追踪；apply 完成后 review，再 sync-specs，最后 archive。
9. **不把具体 Phase 完成日期、commit checklist 和短期任务写入 main specs**。

- **备选 A：只把任务级内容移出，保留 SCALING_PLAN 为活跃架构 source of truth（旧 D3）** — 否决。审阅发现 5 处以上过期/矛盾表述，其中"当前结论"与已修正的 Phase 1 current-state 直接冲突；保留活跃架构 source of truth 仍会产生双轨状态与人工同步负担。
- **备选 B：把 SCALING_PLAN 拆成多份活跃架构文档** — 拒绝。拆散反而扩大双轨面，且仍需人工维护。
- **备选 C：删除 SCALING_PLAN** — 拒绝。丢失决策演进历史；历史快照保留审计价值，原路径 tombstone 保留导航。

### D4（修订）: 管理闭环 = config.yaml tasks rule + change 生命周期模板

后续 Phase change 的 tasks 必须含：branch/worktree、测试证据（命令 + 结果路径）、openspec change 状态更新（tasks checkbox / `openspec status`）。该约束写入 config.yaml `rules.tasks`。**修订点**：原先"roadmap 状态更新"随 SCALING_PLAN 退役一并移除，改为 OpenSpec 自身状态。

- **备选：新建独立 template 文档** — 拒绝。模板文档会再次成为"需同步的第二事实"；规则进 config.yaml 由 CLI 强制，比文档更不易漂移。

## Risks / Trade-offs

- [历史快照被误认为当前规格] → 快照头部醒目历史块 + 原路径 tombstone 只做导航 + README 明确分类。
- [原路径读者找不到内容] → tombstone 内给出历史快照与 openspec 导航链接，链接有效性纳入 tasks 检查。
- [未来 change 忘记生命周期顺序] → config.yaml `rules.tasks` 编码 + `openspec validate`/review 阶段检查。
- [未开始工作被过早创建 change] → 明确"进入正式设计或实施时才创建独立 change"，不提前创建永久 active change。
- [config.yaml 规则过严导致 AI 写 artifact 僵化] → 规则用短句约束关键点（scope 边界、行为契约、证据要求），不写成 checklist 长篇；可在后续 change 中修订。
- [证据文件被误当历史清理] → 明确非目标"不删除任何证据文件"；docs/tasks 与 results 保持 evidence-only 角色。

## Migration Plan

apply 阶段顺序（对应 tasks.md，顺序符合生命周期，sync-specs 位于 apply 完成与用户审阅之后）：

1. **内容归类**：按当前规范 / 稳定治理约束 / 未来候选工作 / 历史实施细节 / benchmark-evidence / 已过期状态，建立 SCALING_PLAN 迁移矩阵。
2. **规格与规则**：更新 scaling-governance delta spec 与 config.yaml 规则（D2）。
3. **冻结历史快照**：`git mv` SCALING_PLAN → archive/SCALING_PLAN-2026-08-22.md，写历史状态块（D3）；不逐项修复正文矛盾。
4. **tombstone**：原路径写 tombstone/navigation 文件（D3）。
5. **README**：移除 SCALING_PLAN 活跃 source of truth 表述，改为 openspec/specs、openspec/changes、archive/、docs/tasks/results 四类入口。
6. **历史文档 banner**：archive 文档头部由"以 SCALING_PLAN 为准"改为 openspec/specs + openspec/changes/status + 代码证据（D1 修订）。
7. **全仓引用检查**：非 archive 当前文档不得再把 SCALING_PLAN 称为 source of truth，不得再通过其 checkbox 管理当前状态；相对链接有效。
8. **验证**：`openspec validate --change establish-scaling-openspec-baseline`；文档验证。
9. **用户审阅 gate**：apply 结果经用户确认后 `openspec sync-specs`；sync 完成并验证 main specs 后 `openspec archive`。

回滚：全部为可逆文件操作（移动/编辑），git 可还原；config.yaml 规则为增量写入，无破坏性。SCALING_PLAN 冻结为单向（快照后不再维护），但快照可从 archive 恢复原路径。

## Open Questions

无。capability 已固定（scaling-governance，由 scaling-docs-baseline 更名），生命周期顺序明确，不需要延期决策。
