## Why

项目需要一个完整项目 checklist 作为进度观测面：随 phase 细化，能力会拆出实现级子任务，如果这些细化全部堆进 `SCALING_ROADMAP.md`，roadmap 会无限膨胀、无法一眼看清"整个项目到哪了"。需要一个**只含 checklist + 简短描述**、细节通过链接指向其他文件的项目总表来承载 phase 细化；`SCALING_ROADMAP.md` 保持现状、不改动，定位继续为总体目标与决策，后续不再往 roadmap 无限制追加细化条目。

## What Changes

- 新增 `openspec/PROJECT_CHECKLIST.md`：完整项目 checklist，**分层级**——每个 Capability 是一个父条目，可展开为其下若干子任务项；每个条目（父级与子级）= 勾选框 + 简短描述 + 状态；详细描述全部以链接指向既有文件（`specs/`、`changes/`、`records/`、`evidence/`、代码）。
- **粒度要求**：子任务粒度需达到实现级特征，而非仅 capability 名。已实现能力（如 C1 Storage Foundation）的子项即其真实落地的功能切片，例如 C1 展开为「SQLite/PG 双 adapter 与共同 interface」「tenant 贯穿」「pool + bounded executor」「provisioning 控制面」；子项从记录与代码推导，见 `records/phase1-storage/m4.5-architecture-hardening.md` 的 M4H-1~M4H-5。
- checklist 覆盖当前全部能力（GOV、C1、C1B、C1C、C1D、C0、C2、C3、C4、C5、C6），并预留结构化扩展方式以承载后续 phase 细化（新增能力即新增一条父条目，子任务随之细化）。
- **不改动 `openspec/SCALING_ROADMAP.md`**：保持现状。新增 checklist 的目的正是让后续 phase 细化落在 checklist 上，而不是让 roadmap 无限扩展；两文件通过互相引用衔接（checklist 引用 roadmap 的 §7/§8 状态规则，roadmap 引用 checklist 为细化入口）。
- 更新 `openspec/README.md` 内容入口表：新增 `PROJECT_CHECKLIST.md` 行（不改动 SCALING_ROADMAP 行的描述）。
- 纯文档重构，无行为变更，`skip_specs: true`。

## Capabilities

### New Capabilities
无（纯文档重构，无行为变更）。

### Modified Capabilities
无（`openspec/specs/scaling-governance/spec.md` 的 12 条 requirement 不变；`skip_specs: true`）。

## Impact

- **文档**：新增 `openspec/PROJECT_CHECKLIST.md`；修改 `openspec/README.md`（入口表新增一行）。`openspec/SCALING_ROADMAP.md` 不改动。
- **程序行为**：无。不影响代码、API、依赖或系统。
- **验收口径**：checklist 与 roadmap 的 `verified` 判定仍以既有 `SCALING_ROADMAP.md` §8 完成状态规则为准；`openspec validate` 需通过。
