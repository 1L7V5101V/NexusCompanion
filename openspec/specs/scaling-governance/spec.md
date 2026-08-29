# scaling-governance Specification

## Purpose
定义 NexusCompanion Pilot 治理契约：三类事实来源（OpenSpec / Pilot Roadmap / 代码·测试·证据）各自职责、capability → change → evidence 追踪闭环、active change 管理规则、verified 状态证据要求、apply→review→sync-specs→archive 生命周期、SCALING_PLAN 历史快照退役规则、openspec/records 与 openspec/evidence 的 evidence-only 角色，使 Pilot 目标、当前规范、独立变更与历史证据分离且状态可核验。

## Requirements

### Requirement: Pilot Program 必须有可度量的 North Star 和目标维度

Pilot Program 必须有明确的 North Star 和一组可度量的目标维度。Pilot 级目标（如"10–30 invited accounts"）必须拆分为可观测维度（invited accounts / concurrent WebChat sessions / active turns / queue backlog / recovery window），由 `openspec/PILOT_ROADMAP.md` 承载，不在 main specs 中作为已实现行为声明。

#### Scenario: 新读者了解 Program 目标

- **WHEN** 需要了解 Pilot Program 的 North Star 或"10–30 invited accounts"的目标拆分
- **THEN** 从 `openspec/PILOT_ROADMAP.md` 获取可度量维度，而不是从 main specs 或历史快照

#### Scenario: 目标维度缺失

- **WHEN** Roadmap 声称达到某目标却没有可度量维度支撑
- **THEN** 该目标视为未定义，不能作为 verified 依据

### Requirement: Pilot Roadmap 只跟踪 program-level outcome、依赖、状态和 evidence

`openspec/PILOT_ROADMAP.md` 只承载 program-level 的 outcome、capability map、依赖、状态、exit evidence、current focus、blockers 与 next decision。它不作为当前系统行为的规范 source of truth，不承载 implementation task、逐 commit 记录或手动完成 checkbox。

#### Scenario: Roadmap 与当前规格分工

- **WHEN** 需要判断某行为的当前规格
- **THEN** 以 openspec/specs 为准，Roadmap 只提供 Program 级目标、依赖与状态上下文，不作为行为规范

#### Scenario: Roadmap 更新范围

- **WHEN** 更新 PILOT_ROADMAP.md
- **THEN** 只更新 outcome、依赖、状态与 evidence，不写入实现步骤或逐 commit 历史

### Requirement: Roadmap 不得承载 implementation task 和逐 commit 历史

PILOT_ROADMAP 不得包含 implementation task 清单、逐 commit 记录或阶段完成时间线；这些由 OpenSpec change 的 design/tasks 与 `openspec status` 承载。

#### Scenario: 任务归属检查

- **WHEN** 在 PILOT_ROADMAP 中发现 implementation task 或逐 commit 记录
- **THEN** 该内容应迁移到对应 OpenSpec change 的 tasks，Roadmap 只保留 capability 状态与 exit evidence

#### Scenario: 无永久 roadmap change

- **WHEN** 需要表达长期演进设想
- **THEN** 不创建永久存在的 `pilot-roadmap` OpenSpec change；Program 级跟踪由 PILOT_ROADMAP.md 承担，能力级工作进入正式设计或实施时才创建独立 change

### Requirement: 每个 capability 必须可关联到一个或多个 OpenSpec change

PILOT_ROADMAP 中的每个 capability 必须能关联到一个或多个 OpenSpec change；每个 active change 必须明确其服务的能力或目标。

#### Scenario: capability 到 change 的追溯

- **WHEN** 审阅某 capability 的状态
- **THEN** 通过 Roadmap 中记录的 active OpenSpec change 定位该 capability 的设计、tasks 与 evidence

#### Scenario: capability 无对应 change

- **WHEN** 某 capability 状态为 in_progress 或 verified，但无关联 OpenSpec change
- **THEN** 视为跟踪断裂，需补建 change 或修正状态

### Requirement: 每个 active change 必须有明确的 exit gate 和 evidence

每个 active change 必须定义明确的 exit gate（验收条件）与 evidence（merge commit、可复现测试或 benchmark 结果）；change 的状态由 tasks checkbox 与 `openspec status` 表达。

#### Scenario: change 完成判定

- **WHEN** 判断某 change 是否完成
- **THEN** 依据其 exit gate 与 evidence 核实，而非依赖口头声明或 Roadmap 状态

#### Scenario: active change 状态表达

- **WHEN** 需要了解当前活跃变更及其进度
- **THEN** 从 openspec/changes 与 `openspec status` 获取，而不是从历史文档

### Requirement: current-state specs 不得把未来能力写成已实现

main specs（current-state）不得把尚未实现的能力描述为当前具备。计划中的能力只出现在对应 active change 的 delta spec 中，并标注为未来目标。

#### Scenario: 未来能力与当前规格分离

- **WHEN** 撰写 main specs 或 change 的 current-state 内容
- **THEN** 不得把未实现的 TurnAdmission、outbox、多 Worker、WebChat 公网生产、Redis cache、PostgreSQL production primary 等描述为当前能力；这些仅在对应 change 中作为未来目标

#### Scenario: 阶段完成声明的依据

- **WHEN** 文档声明某 Phase / Milestone 已完成
- **THEN** 必须引用合并进 main 的 commit 或可复现的测试/基准证据，否则视为 future-state

### Requirement: verified 状态必须有 merge/test/benchmark evidence

capability 或 change 只有在 merge commit、可复现测试或 benchmark 证据存在时才能标记为 `verified`。OpenSpec change 未完成、未 sync、未 archive 时，不得把 capability 标记为 `verified`；日期只能表示 last reviewed，不得作为完成依据。

#### Scenario: verified 需要证据

- **WHEN** 把 capability 或 change 状态更新为 verified
- **THEN** 必须引用合并进 main 的 commit、可复现测试或 benchmark 结果，且关联 change 已完成 apply→review→sync-specs→archive

#### Scenario: 日期不是完成依据

- **WHEN** 某 capability 状态仅由日期或手动 checkbox 支撑，无证据
- **THEN** 该状态只能视为 last reviewed，不能视为 verified

### Requirement: apply → review → sync-specs → archive 生命周期

change 的生命周期顺序固定为：apply（实施）→ review（审阅）→ sync-specs（delta 合入 main specs）→ archive（归档）。sync-specs 不得在 apply 完成并被用户审阅之前执行。

#### Scenario: sync-specs 时机

- **WHEN** 一个 change 尚未完成 apply 或未经用户审阅确认
- **THEN** 不得运行 sync-specs；delta 保持为 change 内部规格

#### Scenario: 归档前置条件

- **WHEN** 执行 archive
- **THEN** sync-specs 已完成且 main specs 验证通过，且所有任务勾选完成

### Requirement: 历史文档不能覆盖 current-state specs 或 active roadmap 状态

openspec/archive、openspec/records、openspec/evidence 中的历史文档不得作为当前规格来源，不得覆盖 openspec/specs；不得覆盖 openspec/PILOT_ROADMAP 的 active 状态。历史文档如声明其内容以某活跃文档为准，该引用必须指向 OpenSpec 或 openspec/PILOT_ROADMAP。

#### Scenario: 历史文档的效力

- **WHEN** 阅读 openspec/archive 历史文档或 openspec/records 实施记录
- **THEN** 其表述视为历史证据；当前规格以 openspec/specs 为准，当前状态以 openspec/changes 与 `openspec status` 为准，Program 目标以 openspec/PILOT_ROADMAP 为准

### Requirement: 同时最多一个 implementation change

任何时刻最多存在一个 implementation change（正在编码或 apply 的 change）；可额外存在一个 design-only change（仅设计，未进入实施）。

#### Scenario: 新 implementation change 立项前检查

- **WHEN** 要开始一个需要实施的新 change
- **THEN** 若已存在未归档的 implementation change，先完成其 apply→review→sync-specs→archive，或明确替代关系

#### Scenario: design-only change 例外

- **WHEN** 只有设计、无编码/apply 的 change
- **THEN** 允许与当前 implementation change 并存，但不得同时出现两个 implementation change

### Requirement: SCALING_PLAN 历史快照退役规则

`SCALING_PLAN` 不再作为活跃 source of truth，其完整正文冻结为历史快照 `openspec/archive/SCALING_PLAN-2026-08-22.md`，tombstone/navigation 页 `openspec/archive/SCALING_PLAN.md` 与快照同目录：不再维护 Phase 状态、不再修正任务状态、不作为冲突裁决依据。tombstone 只提供导航，指向 openspec/PILOT_ROADMAP、openspec/specs、openspec/changes、历史快照与 openspec/records、openspec/evidence。

#### Scenario: 历史快照内容过期

- **WHEN** 发现历史快照正文含过期或内部矛盾的状态
- **THEN** 不修正快照正文；在快照头部注明冻结日期并提示内容可能过期，不逐项修复

#### Scenario: tombstone 导航

- **WHEN** 读者访问 `openspec/archive/SCALING_PLAN.md` tombstone
- **THEN** 看到导航，指向 openspec/PILOT_ROADMAP、openspec/specs、openspec/changes、历史快照与 openspec/records、openspec/evidence，而不是当前状态内容

### Requirement: openspec 为 Pilot 唯一维护目录，records 与 evidence 分离

Pilot Program 的文档与证据全部位于 `openspec/`：`specs/`（当前规格）、`changes/`（活跃变更）、`changes/archive/`（已完成变更）、`PILOT_ROADMAP.md`（program 目标）、`archive/`（历史材料）、`records/`（已完成任务记录）、`evidence/`（可复现脚本与原始结果）。`docs/scaling/`、`docs/tasks/`、`results/` 不作为维护目录。已完成任务记录（叙述、验证方法与 gate 结论）与 evidence（可复现脚本、原始结果）必须分开存放，任务记录引用 evidence 路径。

#### Scenario: 新证据落盘位置

- **WHEN** 产生新的 benchmark 或测试原始结果
- **THEN** 写入 `openspec/evidence/` 对应 capability 子目录，不写入 docs/ 或 results/

#### Scenario: 已完成任务记录

- **WHEN** 记录已完成任务的叙述、验证方法与 gate 结论
- **THEN** 写入 `openspec/records/` 对应 capability 子目录并引用 openspec/evidence 下的证据路径，不在 openspec 之外并行维护

#### Scenario: 任务与证据分离

- **WHEN** 审阅某已完成任务的证据
- **THEN** 从 openspec/records 的叙述跳转到 openspec/evidence 的脚本与原始结果，两者位置稳定且不混放
