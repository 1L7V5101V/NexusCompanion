## Context

现状（详见 proposal.md - Why 与 explore 调查结论）：`docs/scaling/` 混装架构设想、长期 roadmap、Phase 状态（SCALING_PLAN.md）、已自标历史的对照（architecture_comparison.md）、已自标历史的执行清单（migration_checklist.md）；`docs/tasks/phase1-storage/` 是已完成的里程碑证据族（m4.5、m4h-2/3/4、vector-validation），`results/` 存基准原始数据，两者将一并迁入 openspec 并分离为 records（任务记录）与 evidence（可复现脚本与原始结果）。OpenSpec 已初始化，`openspec/specs/` 为空（sync-specs 尚未运行）；config.yaml 已写入 context/rules/operations（早期 apply 产物，含"roadmap 状态更新点""见 docs/scaling/SCALING_PLAN.md"等将被替代的表述）。Phase 1 storage foundation 已 merge（0a83314d），但 SCALING_PLAN 头部"当前结论"仍称 Phase 1 只完成"后端兼容接入的一部分"，与已修正的 Phase 1 current-state 状态直接冲突；审阅共发现 5 处以上过期或矛盾表述。

早期方案（本 change 原 design 的 D3）"SCALING_PLAN 职责收缩、保留为活跃架构层 source of truth、只修正状态段"，以及随后"OpenSpec 成为唯一规范与开发状态管理入口、不建立任何 roadmap 文件"的方案，经审阅均被否决：前者保留活跃架构 source of truth 仍会产生双轨状态；后者让 OpenSpec 承担超出 capability/change 粒度的 Program 级目标追踪，且丢失 Program 级视图。本设计采用**三类事实来源**的治理模型：OpenSpec 管当前规范与独立变更，SCALING_ROADMAP 管 Program 级目标与依赖，代码/测试/results 是实现事实与证据。

本 change 不引入新运行时能力，全部产物是文档组织与治理契约，因此"实现"即：创建 openspec/SCALING_ROADMAP.md、更新 delta spec 与 config.yaml 规则、`git mv` SCALING_PLAN 到 openspec/archive 并写历史块、tombstone 与快照同放 openspec/archive、将 docs/tasks 与 results 迁入 openspec/records 与 openspec/evidence、更新 README 与历史文档 banner、跑 `openspec validate`。

source-of-truth 层级（本 change 后生效，四个 artifact 使用完全一致表述）：

- **行为/规格**：已验证代码与测试证据 > openspec/specs > openspec/changes（含 archive）> 历史材料
- **Program 目标**：openspec/SCALING_ROADMAP.md
- **历史**：openspec/changes/archive（变更历史）、openspec/archive（历史设计材料）、openspec/records 与 openspec/evidence（历史证据）

SCALING_PLAN 不再出现在活跃 source-of-truth 层级中。docs/scaling、docs/tasks、results 原路径退役。

## Goals / Non-Goals

**Goals:**
- 让三类事实来源各司其职：OpenSpec 管规范与独立变更，SCALING_ROADMAP 管 Program 级目标与依赖，代码/测试与 openspec/records、openspec/evidence 是实现事实与证据。
- 让 SCALING_PLAN 作为历史快照退役，不再维护 Phase 状态与 exit gate checkbox。
- 让每个 capability 可关联到 OpenSpec change，每个 active change 有明确 exit gate 与 evidence，形成 capability → change → evidence 闭环。
- 让 verified 状态只能由 merge/test/benchmark 证据支撑，日期只表示 last reviewed。
- 让后续 Phase change（1B/1C、Phase 2+）有固定的生命周期顺序：apply → review → sync-specs → archive。

**Non-Goals:**
- 不修改应用代码、不触碰 `_passive_runtime_lock`。
- 不实现 Phase 0 / TurnAdmission / inbox/outbox / 多 Worker / WebChat / Redis / PostgreSQL primary 切换。
- 不逐项修复历史快照正文中的过期或矛盾表述（历史快照原样保存，只加历史块）。
- 不把 SCALING_ROADMAP 的完整内容复制进 current-state main specs；不创建永久存在的 `scaling-roadmap` OpenSpec change。
- 不删除任何证据文件。
- 本 update 阶段不落盘 config.yaml / docs 改动（那是 apply 阶段 tasks）。

## Decisions

### D1: 三类事实来源，三者不能混用【新增】

治理模型固定为三个层次，各层只负责自己的事实：

| 层 | 负责内容 | 位置 |
|---|---|---|
| Program-level tracking | North Star、可度量目标、capability map、依赖图、current focus、blockers、exit evidence | `openspec/SCALING_ROADMAP.md` |
| Capability-level specification | 当前生效行为、稳定架构不变量、可验证 requirement | `openspec/specs/`（main specs） |
| Change-level execution | proposal、design、delta specs、tasks、branch/worktree、verification、apply/sync/archive 生命周期 | `openspec/changes/` |

三者**不能混用**：Roadmap 不承载 implementation task 与逐 commit 历史；current-state specs 不承载 Program 级目标和 future 能力；change 不承载已完成变更的历史管理。

- **备选：OpenSpec 单独承担 Program 级目标追踪（早期方案）** — 否决。capability/change 是行为粒度，Program 级目标（如"5000 users"拆分为注册租户/在线会话/turn/ingress/LLM 并发/数据量）是度量粒度，混入 OpenSpec 会让 spec 与目标混淆，且 change 生命周期（apply→archive）会不断迁移 Program 状态。
- **备选：继续维护 SCALING_PLAN 作为 Program tracker（旧 D3）** — 否决。SCALING_PLAN 混装架构/任务/状态/历史，是本次要消除的双轨源头；Program tracker 必须是新文件、新职责。

### D2: SCALING_ROADMAP 是 program-level tracker，不是 capability spec、task list 或永久 change【新增】

`openspec/SCALING_ROADMAP.md` 至少包含：

1. North Star；
2. "5000 users" 的拆分指标：registered tenants / concurrent online sessions / active turns / ingress rate / LLM concurrency / data volume；
3. Capability map；
4. 每个 capability 的 outcome、dependency、active OpenSpec change、status、exit evidence；
5. 当前阶段、当前 focus、当前 blocker、next decision；
6. Scaling capability dependency graph；
7. 状态定义：planned / proposed / in_progress / blocked / verified / retired；
8. 完成状态规则。

**完成状态规则**：

- 只有 merge commit、可复现测试或 benchmark 证据存在时，才能使用 `verified`；
- OpenSpec change 未完成、未 sync、未 archive 时，不能把 capability 标为 `verified`；
- 日期只能表示 last reviewed，不得作为完成依据。

Roadmap 是 program-level tracker：不复制 SCALING_PLAN 的架构内容；不承载 implementation task、逐 commit 记录或手动完成 checkbox；不作为当前系统行为的规范 source of truth。

- **备选：把 Roadmap 建成长期滚动 change（`scaling-roadmap`）** — 否决。change 生命周期以 apply→archive 为终，永久 active change 会长期处于未归档状态，与"同时最多一个 implementation change"冲突。
- **备选：Roadmap 同时承载 implementation task** — 否决。任务与进度由 OpenSpec change 的 tasks 与 `openspec status` 承载；Roadmap 只跟踪 outcome、依赖、状态与 exit evidence。

### D3（替代旧 D3）: SCALING_PLAN 冻结并退役

旧 D3"SCALING_PLAN 职责收缩，不重构全文"被废弃。SCALING_PLAN 不再作为活跃 source of truth。迁移方式：

1. **完整正文保存为** `openspec/archive/SCALING_PLAN-2026-08-22.md`（`git mv`）。
2. **历史快照头部**明确写明：冻结日期为 2026-08-22；文件可能包含跨时期积累的过期或内部矛盾状态；不再修正、不再维护；不作为当前状态或规格依据；当前规范以 `openspec/specs` 为准；当前变更以 `openspec/changes` 与 `openspec status` 为准；当前项目目标与依赖以 `openspec/SCALING_ROADMAP.md` 为准；实现事实以代码、测试和 benchmark 证据为准。
3. **tombstone** `openspec/archive/SCALING_PLAN.md` 与快照同目录，是简短导航文件，仅指向：`openspec/SCALING_ROADMAP.md`、`openspec/specs/`、`openspec/changes/`、`openspec/changes/archive/`、`openspec/records/`、`openspec/evidence/`、历史快照。不得保留 Phase 状态、任务 checkbox、完成时间线、详细实施拆分或当前架构 source-of-truth 声明。
4. **openspec/README.md**（由 `docs/scaling/README.md` 迁入）不再把 SCALING_PLAN 列为活跃 source of truth；入口改为 openspec/specs、openspec/changes、SCALING_ROADMAP、openspec/records、openspec/evidence、openspec/archive 等。
5. **openspec/archive/ 历史文档**头部不再写"以 SCALING_PLAN 为准"，改为指向 openspec/specs、openspec/changes + `openspec status`、代码/测试证据。
6. **docs/tasks/phase1-storage 与 results** 迁入 openspec/records/phase1-storage 与 openspec/evidence/phase1-storage，作为历史实施证据，不再要求同步当前状态（见 D6）。
7. **不建立新的长期滚动 roadmap change**；Program 级目标由 `openspec/SCALING_ROADMAP.md` 承载，未开始的未来工作不提前创建永久 active change；当工作进入正式设计或实施时，才创建独立 OpenSpec change。
8. **稳定的跨 change 约束进入 scaling-governance capability spec**。
9. **不把具体 Phase 完成日期、commit checklist 和短期任务写入 main specs**。

- **备选 A：只把任务级内容移出，保留 SCALING_PLAN 为活跃架构 source of truth（旧 D3）** — 否决。审阅发现 5 处以上过期/矛盾表述；保留活跃架构 source of truth 仍会产生双轨状态。
- **备选 B：把 SCALING_PLAN 拆成多份活跃架构文档** — 拒绝。拆散反而扩大双轨面，且仍需人工维护。
- **备选 C：删除 SCALING_PLAN** — 拒绝。丢失决策演进历史；历史快照保留审计价值，archive 内 tombstone 保留导航。

### D4: config.yaml 治理规则结构【修订】

apply 阶段更新 `openspec/config.yaml`：

- `context`：移除"扩展目标 5000 用户（见 docs/scaling/SCALING_PLAN.md）"的活跃引用，5000 用户目标改为指向 openspec/specs（scaling-governance）与 openspec/SCALING_ROADMAP.md。
- `rules.tasks`：把"roadmap 状态更新点"替换为"openspec change 状态更新（tasks checkbox / `openspec status`）与 SCALING_ROADMAP 状态更新（仅在有 evidence 时）"；管理闭环元素改为 branch/worktree + 测试证据 + openspec change 状态更新。
- `operations`：apply/archive guidance 中的"roadmap 状态"表述同步改为 OpenSpec change 状态与 SCALING_ROADMAP 状态。

- **备选：把全部约定写进 SCALING_PLAN 或 SCALING_ROADMAP** — 拒绝。SCALING_PLAN 已退役、Roadmap 不承载治理规则；config.yaml 是 OpenSpec 读取的权威入口，AI 写 artifact 时自动获得约束。
- 关联：specs `scaling-governance` 的治理约束由本 D4 与 delta spec 共同落地。

### D5（修订）: 管理闭环 = 每任务含 修改对象/完成条件/验证方法/evidence 位置

后续 Phase change 的 tasks 必须含：修改对象、完成条件、验证方法、evidence 位置，以及 branch/worktree、openspec change 状态更新点（tasks checkbox / `openspec status`）。该约束写入 config.yaml `rules.tasks`。**修订点**：原先"roadmap 状态更新"随 SCALING_PLAN 退役一并移除，改为 OpenSpec change 状态更新（仅在有证据时更新）与 SCALING_ROADMAP 状态更新（仅在有 evidence 时）。

- **备选：新建独立 template 文档** — 拒绝。模板文档会再次成为"需同步的第二事实"；规则进 config.yaml 由 CLI 强制，比文档更不易漂移。

### D6: 已完成任务记录与证据分离，并入 openspec【新增】

`docs/tasks/` 与 `results/` 不再作为独立维护目录。全部并入 openspec，并按职责分离：

| 内容 | 迁移目标 |
|---|---|
| 已完成任务的记录文档（m4.5、m4h-2/3/4、phase1-storage、storage-interface、vector-validation） | `openspec/records/phase1-storage/` |
| 可复现脚本与原始结果（partition_test.py、recall_bench2.py、benchmark README、*.txt、m4h4_*.json） | `openspec/evidence/phase1-storage/` |

- `records/` 承载"做了什么、怎么验证、gate 结论"的叙述；`evidence/` 承载可复现的脚本与原始数据。两者分开管理，叙述必须引用 evidence 路径。
- 迁移后 `docs/scaling/`、`docs/tasks/`、`results/` 原路径退役（README/ROADMAP 迁入 openspec 根，SCALING_PLAN tombstone 与快照同入 openspec/archive），openspec 成为 Scaling 唯一维护目录。
- **备选：docs/tasks 原地保留为 evidence-only** — 否决。用户明确要求"只维护 openspec 文件夹内的内容"，并行维护 docs 与 openspec 会产生双份路径引用与漂移。
- **备选：全部证据混放在一个 openspec/evidence/ 目录** — 否决。任务叙述（records）与可复现脚本/原始数据（evidence）职责不同，分开便于审阅与复用。

## Risks / Trade-offs

- [历史快照被误认为当前规格] → 快照头部醒目历史块 + archive 内 tombstone 只做导航 + README 明确分类。
- [Roadmap 被误当作 current-state spec 或 task list] → Roadmap 明确只跟踪 program-level outcome/依赖/状态/evidence；spec 与 task 由 OpenSpec 承载；delta spec requirement 3 强制 Roadmap 不承载 implementation task。
- [verified 状态被滥用] → 只有 merge/test/benchmark 证据存在才允许 verified；change 未 apply/sync/archive 时禁止 verified。
- [读者找不到历史内容] → tombstone 内给出历史快照与 openspec/roadmap 导航链接，链接有效性纳入 tasks 检查。
- [未来 change 忘记生命周期顺序] → config.yaml `rules.tasks` 编码 + `openspec validate`/review 阶段检查。
- [未开始工作被过早创建 change] → 明确"进入正式设计或实施时才创建独立 change"，不提前创建永久 active change。
- [config.yaml 规则过严导致 AI 写 artifact 僵化] → 规则用短句约束关键点（scope 边界、行为契约、证据要求），不写成 checklist 长篇；可在后续 change 中修订。
- [证据文件被误当历史清理] → 明确非目标"不删除任何证据文件"；openspec/records 与 openspec/evidence 保持 evidence-only 角色。

## Migration Plan

apply 阶段顺序（对应 tasks.md，顺序符合生命周期，sync-specs 位于 apply 完成与用户审阅之后）：

1. **内容盘点与分类**：按 program goal / stable capability·invariant / future capability / implementation task / evidence / historical material / stale status 建立 SCALING_PLAN 迁移矩阵，与用户确认。
2. **设计并创建 SCALING_ROADMAP.md**：按 D2 建立 program-level tracker（写入 `openspec/SCALING_ROADMAP.md`）。
3. **迁移 program-level 内容到 Roadmap**：把 SCALING_PLAN 中的 North Star、5000 users 拆分指标、capability map、依赖、状态迁移到 Roadmap（D2）。
4. **冻结历史快照**：`git mv` SCALING_PLAN → `openspec/archive/SCALING_PLAN-2026-08-22.md`，写历史状态块（D3）；不逐项修复正文矛盾。
5. **tombstone**：`openspec/archive/SCALING_PLAN.md` 写 tombstone/navigation 文件（D3），与快照同目录，指向 SCALING_ROADMAP、openspec/specs/changes/records/evidence、历史快照等。
6. **迁移证据与任务记录**：docs/tasks 与 results 迁入 openspec/records 与 openspec/evidence（D6），叙述与脚本/数据分离；修正移动文件间的相对链接。
7. **README 与 ROADMAP/PLAN 迁入 openspec**：`docs/scaling/README.md` 移为 `openspec/README.md`，移除 SCALING_PLAN 活跃 source of truth 表述；SCALING_ROADMAP.md 迁入 openspec 根，SCALING_PLAN.md tombstone 与快照同入 openspec/archive。
8. **历史文档 banner**：openspec/archive 文档头部由"以 SCALING_PLAN 为准"改为 openspec/specs + openspec/changes/status + 代码证据（D3）。
9. **更新 config.yaml 治理规则**：context 与 rules/operations（D4），路径改为 `openspec/SCALING_ROADMAP.md`。
10. **退役原路径**：docs/scaling、docs/tasks、results 空目录删除；全仓引用与相对链接修正（含快照内 `./archive/` 旧链接）。
11. **验证**：`openspec validate establish-scaling-openspec-baseline`；确认未修改应用代码。
12. **文档链接与残留引用检查**：全仓 Markdown 相对链接有效；SCALING_PLAN、docs/tasks、results 无活跃 source-of-truth 残留。
13. **用户审阅 gate**：apply 结果经用户确认。
14. **sync specs**：`openspec sync-specs --change establish-scaling-openspec-baseline`，验证 `openspec/specs/scaling-governance/spec.md` 存在且含全部 requirement。
15. **archive**：sync 验证通过后 `openspec archive`；输出建议 commit message 等用户确认（不自动 commit）。

回滚：全部为可逆文件操作（移动/编辑），git 可还原；config.yaml 规则为增量写入，无破坏性。SCALING_PLAN 冻结为单向（快照后不再维护），但快照可从 archive 恢复原路径。

## Open Questions

无。capability 已固定（scaling-governance），三类事实来源与生命周期顺序明确，不需要延期决策。
