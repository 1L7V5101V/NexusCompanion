## Purpose

定义 NexusCompanion Scaling 文档体系与 OpenSpec 之间的治理契约：OpenSpec 作为 Scaling 唯一规范与开发状态管理入口、current/future-state 分离、active change 管理规则、apply→review→sync-specs→archive 生命周期、SCALING_PLAN 历史快照退役规则、docs/tasks/results 的 evidence-only 角色，使文档组织一致、状态可核验、后续 Phase 变更无需手动对账。

## ADDED Requirements

### Requirement: OpenSpec 是 Scaling 唯一规范管理入口

OpenSpec（main specs、active changes、changes archive）是 Scaling 规范与开发状态的唯一管理系统。`docs/scaling/SCALING_PLAN.md` 不再作为活跃 source of truth，其正文冻结为历史快照；冲突裁决不得再以 SCALING_PLAN 为依据。当前规范与状态按以下层级获取：

1. 已验证代码和测试证据：实现事实
2. openspec/specs：当前生效规格和治理契约
3. openspec/changes：活跃增量、设计、tasks 和状态
4. openspec/changes/archive：已完成变更历史
5. docs/tasks 和 results：历史证据
6. docs/scaling/archive：历史设计材料

#### Scenario: 冲突裁决只依据 OpenSpec 与代码证据

- **WHEN** 某文档的状态与另一文档冲突，且涉及 `SCALING_PLAN` 历史快照内容
- **THEN** 以已验证代码/测试证据与 openspec/specs、openspec/changes 为准，SCALING_PLAN 历史快照不参与裁决

#### Scenario: 新读者定位当前状态

- **WHEN** 需要了解 Scaling 的当前规范或某 change 的状态
- **THEN** 从 openspec/specs（当前规格）、openspec/changes 与 `openspec status`（活跃状态）、openspec/changes/archive（已完成历史）获取，而不是从 `SCALING_PLAN`

### Requirement: current-state 与 future-state 分离

已完成状态只能由合并进 main 的提交或可复现证据支撑；分支存在不得作为完成依据。计划中的工作必须明确标注为 future-state，且只出现在对应活跃 change 的 delta spec 中，不得写入 current-state main specs。

#### Scenario: 阶段完成声明的依据

- **WHEN** 文档声明某 Phase / Milestone 已完成
- **THEN** 必须引用合并进 main 的 commit 或可复现的测试/基准证据，否则视为 future-state

#### Scenario: 分支不代表完成

- **WHEN** 一个分支存在但尚未合并，或缺少可复现证据
- **THEN** 相关文档不得把该工作标记为已完成，须标注为进行中或计划中

### Requirement: active change 管理规则

每个实现目标对应一个独立 change；未开始的未来工作不得提前创建永久 active change。活跃 change 必须包含明确的 design、tasks 与验收条件，其状态由 tasks checkbox 与 `openspec status` 表达。

#### Scenario: 新工作立项

- **WHEN** 一项未来工作进入正式设计或实施
- **THEN** 为该工作创建独立 OpenSpec change，记录 design、tasks 与验收条件；未开始的设想不创建 change

#### Scenario: 无长期滚动 roadmap change

- **WHEN** 需要表达长期演进设想
- **THEN** 不创建永久存在的"总 roadmap change"；设想仅在进入正式设计或实施时转化为独立 change

### Requirement: change/branch-worktree/requirement/task/evidence 可追踪

每个 change 的 requirement、task 与验证 evidence 必须可互相追溯；task 应记录关联 branch/worktree、验证命令与证据路径。

#### Scenario: 任务到证据的追溯

- **WHEN** 审阅某 change 的一个 task 是否完成
- **THEN** 通过该 task 记录的 branch/worktree、验证命令与证据路径核实，而非依赖口头或文档声明

#### Scenario: 全仓证据可复现

- **WHEN** 需要复核某里程碑结论
- **THEN** 从 change 的 tasks/evidence、docs/tasks、results 找到可复现的 commit 哈希、测试脚本或基准 JSON

### Requirement: apply → review → sync-specs → archive 生命周期

change 的生命周期顺序固定为：apply（实施）→ review（审阅）→ sync-specs（delta 合入 main specs）→ archive（归档）。sync-specs 不得在 apply 完成并被用户审阅之前执行。

#### Scenario: sync-specs 时机

- **WHEN** 一个 change 尚未完成 apply 或未经用户审阅确认
- **THEN** 不得运行 sync-specs；delta 保持为 change 内部规格

#### Scenario: 归档前置条件

- **WHEN** 执行 archive
- **THEN** sync-specs 已完成且 main specs 验证通过，且所有任务勾选完成

### Requirement: 历史文档不可覆盖 main specs

docs/scaling、docs/tasks、results 中的历史文档不得作为当前规格来源，不得覆盖 openspec/specs；历史文档如声明其内容以某活跃文档为准，该引用必须指向 OpenSpec。

#### Scenario: 历史文档的效力

- **WHEN** 阅读 archive/ 历史文档或 docs/tasks 实施记录
- **THEN** 其表述视为历史证据；当前规格以 openspec/specs 为准，当前状态以 openspec/changes 与 `openspec status` 为准

### Requirement: SCALING_PLAN 历史快照退役规则

`SCALING_PLAN` 正文冻结为历史快照并退役：不再维护 Phase 状态、不再修正任务状态、不作为冲突裁决依据。原路径只保留 tombstone/navigation 文件。

#### Scenario: 历史快照内容过期

- **WHEN** 发现历史快照正文含过期或内部矛盾的状态
- **THEN** 不修正快照正文；在快照头部注明冻结日期并提示内容可能过期，不逐项修复

#### Scenario: 原路径导航

- **WHEN** 读者访问 `docs/scaling/SCALING_PLAN.md` 原路径
- **THEN** 看到 tombstone/navigation，指向 openspec/specs、openspec/changes、历史快照与 docs/tasks/results，而不是当前状态内容

### Requirement: docs/tasks/results 的 evidence-only 角色

docs/tasks 与 results 只承载历史实施记录、benchmark、迁移报告与机器可读证据，不管理当前进度，不作规范 source of truth。

#### Scenario: 历史实施记录定位

- **WHEN** 阅读 docs/tasks/phase1-storage 或 results 中的基准 JSON
- **THEN** 视为已发生工作的历史证据；不据此推断当前进度，当前进度以 `openspec status` 与 change tasks 为准

### Requirement: 未实现能力不得进入 current-state main specs

main specs（current-state）不得把尚未实现的能力描述为当前具备。计划中的能力只出现在对应 active change 的 delta spec 中，并标注为未来目标。

#### Scenario: 未来能力与当前规格分离

- **WHEN** 撰写 main specs 或 change 的 current-state 内容
- **THEN** 不得把未实现的 TurnAdmission、outbox、多 Worker、WebChat 公网生产、Redis cache、PostgreSQL production primary 等描述为当前能力；这些仅在对应 change 中作为未来目标

### Requirement: 同时最多一个 implementation change

任何时刻最多存在一个 implementation change（正在编码或 apply 的 change）；可额外存在一个 design-only change（仅设计，未进入实施）。

#### Scenario: 新 implementation change 立项前检查

- **WHEN** 要开始一个需要实施的新 change
- **THEN** 若已存在未归档的 implementation change，先完成其 apply→review→sync-specs→archive，或明确替代关系

#### Scenario: design-only change 例外

- **WHEN** 只有设计、无编码/apply 的 change
- **THEN** 允许与当前 implementation change 并存，但不得同时出现两个 implementation change
