## Context

`openspec/SCALING_ROADMAP.md` 是 program-level tracker，承载总体目标/决策（§1 North Star、§2 目标维度、§3 SLO、§6 依赖图、§7 状态定义、§8 完成规则）与进度观测（§4 capability map 逐项 status/evidence、§5 当前阶段/focus/blocker）。本项目已有成熟的细节承载面：`openspec/specs/`（规范）、`openspec/changes/`（增量 change 与 tasks）、`openspec/records/`（完成记录）、`openspec/evidence/`（可复现证据）。**约束：`SCALING_ROADMAP.md` 保持现状、不改动**——checklist 的目的是承接 phase 细化，避免 roadmap 无限膨胀。动机见 `proposal.md`。

## Goals / Non-Goals

**Goals:**
- `openspec/PROJECT_CHECKLIST.md` 成为完整项目进度观测面：分层级（父能力条目 → 子任务条目），每条 = 勾选框 + 一行简短描述 + 状态，详细描述一律链接到既有细节文件。
- 支持 phase 细化扩展：新增能力/任务时以"新增一条父条目 / 新增子条目"承载，不重写结构，也不往 `SCALING_ROADMAP.md` 追加细化条目。
- `SCALING_ROADMAP.md` 保持现状、不改动；`openspec/README.md` 入口表新增 checklist 行。

**Non-Goals:**
- 不创建新的细节文档；checklist 不内联 evidence / 迁移命令 / 完整 outcome。
- 不改变 `openspec/specs/scaling-governance/spec.md` 的 requirement（纯文档重构，`skip_specs: true`）。
- 不改动 `openspec/SCALING_ROADMAP.md` 的任何章节内容。
- 不覆盖非 scaling 的维护性工作（bugfix、常规发布）——checklist 范围为当前 capability map 定义的项目工作。
- 子条目不停留到逐 commit 粒度；子条目是功能切片，不是 commit / 任务编号的镜像。

## Decisions

**D1: 文件位置与命名 = `openspec/PROJECT_CHECKLIST.md`**
与 `SCALING_ROADMAP.md` 同级，属于 openspec 治理目录。理由：checklist 是 program-level 观测面，与 roadmap/specs/changes 同仓，相对链接最近（`./specs/...`、`./changes/...`）。备选：`docs/`（与治理目录分离，跨目录链接变长，否决）；项目根目录（脱离 openspec 治理边界，否决）。

**D2: 范围 = 当前全部能力 + 可扩展结构**
第一版条目 = `SCALING_ROADMAP.md` §4 capability map 的 11 项能力（GOV、C1、C1B、C1C、C1D、C0、C2、C3、C4、C5、C6）。结构上按 phase 分组，phase 细化时新增条目/小节即可扩展。

**D3: 条目结构 = 分层级：父能力条目 + 可展开的子任务条目**
- **父条目**：`- [ ] C1 Storage Foundation — 一行简短描述；状态标记；→ 详情链接`。对应一个 Capability，链接到其 spec / 主要 record。
- **子条目**：在父条目缩进层级下，`- [ ] 子任务名 — 一行简短描述；→ 详情链接`。子条目粒度 = 该能力真实落地的功能切片（实现级），不是任务编号（M0/M1）也不是 commit。
- 子条目数量不设上限；子条目可再缩进一层（如 provisioning 控制面 → readiness gate / control worker / 5000 基准），但默认鼓励一到两层，避免退化为逐 commit 清单。

**D4: 子任务粒度来源 = 以记录与代码为准，不臆测**
已实现能力（GOV、C1）的子条目从 `openspec/records/phase1-storage/`、`openspec/evidence/phase1-storage/` 与 `infra/storage/` 代码推导——例如 C1 的 M4H-1~M4H-5 对应「SQLite/PG 双 adapter 与共同 interface」「tenant 贯穿」「pool + bounded executor」「provisioning 控制面」「merge readiness 与验证」。计划中能力（C1B-C1D、C0、C2-C6）的子条目从其 capability outcome 描述拆出（如 C1B = 批量 COPY / 断点续传 / 机器可读校验），不虚构超出既有规划的粒度。子条目勾选与状态同样遵守 §8 完成状态规则。

**D5: 按 phase 分组**
小节标题 = phase（Phase 1 已完成、Phase 1B 进行中、Phase 1C、Phase 2 后续能力），其下为该 phase 的 checklist 项。使"进一步细化的 phase 进展"直接可读（每 phase 的完成度 = 该节勾选比例）。GOV 作为治理基线放最前。

**D6: 状态语义复用 `SCALING_ROADMAP.md` §7/§8**
checklist 的状态标记复用 §7 六态（planned/proposed/in_progress/blocked/verified/retired）；勾选框仅在 `verified`（满足 §8：merge commit + 可复现证据）时勾选。checklist 是派生观测面，不是新的 source of truth——状态更新只允许在有证据时进行，与 change 的 tasks/`openspec status` 保持同步。

**D7: `SCALING_ROADMAP.md` 不改动；checklist 与 roadmap 的职责边界**
`SCALING_ROADMAP.md` 保持现状、内容不动。职责划分：roadmap 承载总体目标与决策（§1/§2/§3/§6/§7/§8）与既有进度观测（§4/§5），是 program-level 的 source；checklist 承载 phase 细化的展开（父能力 → 实现级子任务），是细化观测面。后续 phase 细化只写进 checklist，不往 roadmap 追加；roadmap §4/§5 的既有状态若与 checklist 同步演进，只由 checklist 引用并保持口径一致，不回改 roadmap 结构。

**D8: `README.md` 入口表更新**
新增 `PROJECT_CHECKLIST.md` 行（定位：完整项目进度观测 checklist，phase 细化的展开面），不改动 `SCALING_ROADMAP.md` 行的描述。

## Risks / Trade-offs

- [checklist 状态与 change/roadmap 状态漂移] → 状态更新仅在有 evidence 时进行（复用 §8 规则）；checklist 头部声明 source-of-truth 优先级与派生身份。
- [链接失效（细节文件改名/移动）] → 链接仅指向稳定目录（specs/records/evidence/changes），README 已固化这些目录；每次 checklist 编辑时检查链接目标存在。
- [与 SCALING_ROADMAP §4/§5 状态重复导致口径不一] → roadmap 是 program-level source，保留 §7/§8 状态定义与完成规则的权威文本；checklist 父条目状态与 roadmap 保持口径一致（引用而非另立一套），子条目按 §8 以 evidence 为准；编辑 checklist 时对照 roadmap §4/§5 校对。
- [子条目粒度漂移为任务编号/commit 镜像，或反过来退化为父级粗粒度] → D4 规定子条目 = 功能切片且来源是记录与代码；编辑 checklist 时以 record 文件而非 commit log 为粒度基准。
