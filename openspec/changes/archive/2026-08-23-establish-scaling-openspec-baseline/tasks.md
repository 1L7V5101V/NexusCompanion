## 1. 盘点现有 SCALING_PLAN 内容并分类

- [x] 1. 盘点现有 SCALING_PLAN 内容，按七类建立迁移矩阵并与用户确认
  - 修改对象：`docs/scaling/SCALING_PLAN.md`（及 archive 快照 `docs/scaling/archive/SCALING_PLAN-2026-08-22.md`）。
  - 完成条件：按七类（program goal / stable capability·invariant / future capability / implementation task / evidence / historical material / stale status）建立内容迁移矩阵，覆盖 SCALING_PLAN 全部章节且无遗漏；与用户确认归类结果，验证无未来设想（TurnAdmission、outbox、多 Worker、WebChat 公网、Redis cache、PostgreSQL primary 切换）被误归为"stable capability"。
  - 验证方法：逐章对照分类矩阵；用户确认记录存在。
  - evidence 位置：迁移矩阵（写入 apply 记录或本 change 提交说明）。

  **apply 记录（迁移矩阵，2026-08-22 用户确认）**：

  | 章节 | 主要分类 | 去向 |
  |---|---|---|
  | §0 架构复审结论 | historical material + stale status + stable invariant | 冻结快照；§0.2 保留决策（PG/pgvector/SQLite adapter）作为 stable invariant 摘录 |
  | §1 容量目标与 SLO | program goal | 迁入 Roadmap：六维度拆分、画像 A/B、服务目标 SLO |
  | §2 不可破坏的架构约束 | stable capability / invariant | 冻结快照（不变量上下文，不复制全文） |
  | §3 目标拓扑 | future capability | 冻结快照 |
  | §4 关键 module 与 seam | stable invariant（TenantResolver/StorageRuntime 已落地）+ future capability（TurnAdmission/ModelGateway/DeliveryDispatcher/RuntimeCoordinator 未实现） | 已落地 seam 留快照；未实现 module 作为 future capability 摘录 |
  | §5 分阶段实施计划 | program goal + implementation task + evidence + stale status | Phase 目标与 exit gate → Roadmap capability map；M0-M4 任务/证据与 Phase 1 旧状态 → 冻结快照 |
  | §6 阶段依赖与并行规则 | program goal（dependency） | 迁入 Roadmap dependency graph |
  | §7 容量与成本模型 | program goal + historical material | 公式口径 → Roadmap 目标维度上下文；被删除项（10 Worker/50 连接、$1900/月）→ 冻结快照 |
  | §8 迁移状态机与回滚 | future capability + implementation task | 冻结快照（Phase 1B/1C 实施依据） |
  | §9 可观测性清单 | program goal + future capability | 冻结快照（Phase 0 目标摘录） |
  | §10 风险登记 | program goal + evidence | 冻结快照；已缓解风险（tenant 贯穿、provisioning 控制面）→ evidence |
  | §11 上线验收总清单 | program goal | Roadmap exit evidence 参考（不复制清单） |
  | §12 长期演进触发条件 | future capability | 冻结快照 |
  | §13 下一步建议 | program goal + historical material | next decision → Roadmap；"scaling-docs-baseline" 旧引用 → stale（capability 已更名 scaling-governance） |

## 2. 设计并创建 SCALING_ROADMAP.md

- [x] 2. 设计并创建 SCALING_ROADMAP.md
  - 修改对象：新建 `openspec/SCALING_ROADMAP.md`。
  - 完成条件：包含 8 个必需要素——North Star；"5000 users" 拆分指标（registered tenants / concurrent online sessions / active turns / ingress rate / LLM concurrency / data volume）；capability map；每个 capability 的 outcome / dependency / active OpenSpec change / status / exit evidence；当前阶段、当前 focus、当前 blocker、next decision；dependency graph；状态定义（planned / proposed / in_progress / blocked / verified / retired）；完成状态规则（verified 需 merge/test/benchmark 证据；change 未 apply/sync/archive 不得 verified；日期只表示 last reviewed）。
  - 验证方法：检查文件结构包含全部要素；确认未承载 implementation task 与逐 commit 记录。
  - evidence 位置：`openspec/SCALING_ROADMAP.md` 本身。

## 3. 将 program-level 目标、capability map、依赖和状态迁移到 Roadmap

- [x] 3. 将 program-level 目标、capability map、依赖和状态迁移到 Roadmap
  - 修改对象：`openspec/SCALING_ROADMAP.md`（承接 SCALING_PLAN 中的 Program 级内容）。
  - 完成条件：SCALING_PLAN 中属于 program goal / capability map / dependency / program-level status 的内容迁移到 Roadmap；迁移矩阵相应标记"已迁移"；未来能力不写成已实现。
  - 验证方法：对照步骤 1 的迁移矩阵逐项核对已迁移项。
  - evidence 位置：迁移矩阵 + `openspec/SCALING_ROADMAP.md`。

## 4. 将 SCALING_PLAN 完整正文冻结到 archive

- [x] 4. 将 SCALING_PLAN 完整正文冻结到 archive 并写历史块
  - 修改对象：`docs/scaling/SCALING_PLAN.md` → `openspec/archive/SCALING_PLAN-2026-08-22.md`。
  - 完成条件：`git mv` 后正文完整保留；头部增加历史状态块，声明冻结日期为 2026-08-22、内容可能含跨时期过期或内部矛盾、不再修正与维护、不作为当前状态或规格依据、当前规范以 `openspec/specs` 为准、当前变更以 `openspec/changes` 与 `openspec status` 为准、当前项目目标与依赖以 `openspec/SCALING_ROADMAP.md` 为准、实现事实以代码、测试和 benchmark 证据为准；不逐项修复正文过期/矛盾表述。
  - 验证方法：`git status` 显示为 rename；`git diff` 除头部块外无其他正文改动。
  - evidence 位置：`openspec/archive/SCALING_PLAN-2026-08-22.md` 头部块 + `git diff` 输出。

## 5. 创建 SCALING_PLAN.md tombstone

- [x] 5. 创建 SCALING_PLAN.md tombstone/navigation 页
  - 修改对象：`docs/scaling/SCALING_PLAN.md`（原路径，后迁 `openspec/archive/SCALING_PLAN.md`，与快照同目录）。
  - 完成条件：改为简短 tombstone/navigation，仅指向 `openspec/SCALING_ROADMAP.md`、`openspec/specs/`、`openspec/changes/`、`openspec/changes/archive/`、`openspec/records/`、`openspec/evidence/`、历史快照路径；不得含 Phase 状态、任务 checkbox、完成时间线、详细实施拆分、当前架构 source-of-truth 声明。
  - 验证方法：检查文件无上述禁止内容；所有链接可达。
  - evidence 位置：`openspec/archive/SCALING_PLAN.md`。

## 6. 更新 docs/scaling/README.md

- [x] 6. 更新 README 入口与使用原则并迁入 openspec
  - 修改对象：`docs/scaling/README.md` → `openspec/README.md`。
  - 完成条件：移除"SCALING_PLAN.md 为活跃 source of truth"条目；入口改为 openspec/specs（当前规格）、openspec/changes（活跃工作）、`openspec/SCALING_ROADMAP.md`（Program 目标）、openspec/records 与 openspec/evidence（证据）、openspec/archive（历史材料）；使用原则第 2 条 source-of-truth 优先级移除 SCALING_PLAN，改为"已验证代码与测试证据 > openspec/specs > openspec/changes（含 archive）> 历史材料"，Program 目标以 openspec/SCALING_ROADMAP 为准。
  - 验证方法：README 不再把 SCALING_PLAN 列为活跃 source of truth；表述与 design 一致。
  - evidence 位置：`openspec/README.md`。

## 7. 修正活跃文档中的 source-of-truth 表述

- [x] 7. 修正历史文档 banner 与全仓 SCALING_PLAN 活跃引用
  - 修改对象：`docs/scaling/archive/architecture_comparison.md`、`docs/scaling/archive/migration_checklist.md` 等历史文档头部（后迁 `openspec/archive/`）；全仓非 archive 文档中对 SCALING_PLAN 的活跃引用。
  - 完成条件：archive 文档头部由"以 SCALING_PLAN 与已验证代码为准"改为"当前规格以 openspec/specs 为准；当前 change 状态以 openspec/changes 与 `openspec status` 为准；Program 目标以 openspec/SCALING_ROADMAP.md 为准；实现事实以代码和测试证据为准"；全仓非 archive 文档不再把 SCALING_PLAN 称为 source of truth、不再通过其 checkbox 管理当前状态。
  - 验证方法：`ls` 检查相对路径有效；grep `SCALING_PLAN` 逐条核对无活跃 source-of-truth 残留。
  - evidence 位置：archive 文档头部 + 全仓引用检查结果。

## 8. 更新 config.yaml 治理规则

- [x] 8. 更新 openspec/config.yaml 治理规则
  - 修改对象：`openspec/config.yaml`。
  - 完成条件：`context` 移除"扩展目标 5000 用户（见 docs/scaling/SCALING_PLAN.md）"的活跃引用，改为指向 openspec/specs（scaling-governance）与 openspec/SCALING_ROADMAP.md；`rules.tasks` 把"roadmap 状态更新点"替换为"openspec change 状态更新（tasks checkbox / `openspec status`）与 SCALING_ROADMAP 状态更新（仅在有 evidence 时）"；`operations` 中"roadmap 状态"表述同步改为 OpenSpec 状态与 SCALING_ROADMAP 状态。
  - 验证方法：yaml 语法合法（`python -c "import yaml;yaml.safe_load(open('openspec/config.yaml'))"`）；`openspec instructions tasks --change establish-scaling-openspec-baseline --json` 的 rules 字段可见新表述。
  - evidence 位置：`openspec/config.yaml` + CLI 输出。

## 9. 运行 OpenSpec validation

- [x] 9. 运行 OpenSpec validation 并确认未改应用代码
  - 修改对象：无（验证步骤）。
  - 完成条件：`openspec validate establish-scaling-openspec-baseline` 全部通过；确认未修改任何应用代码（`git status --porcelain` 仅含文档与 openspec/ 文件，`infra/`、`agent/`、`session/`、`memory2/` 无改动）。
  - 验证方法：CLI 返回通过；`git status --porcelain` 核对。
  - evidence 位置：CLI 输出 + `git status` 结果。

## 10. 运行文档链接和残留引用检查

- [x] 10. 运行全仓 Markdown 链接和 SCALING_PLAN 残留引用检查
  - 修改对象：无（验证步骤）。
  - 完成条件：全仓 Markdown 相对链接有效（无 404）；SCALING_PLAN 不再作为活跃 source of truth、不再通过其 checkbox 管理当前状态；无指向 docs/scaling、docs/tasks、results 原路径的活跃引用。
  - 验证方法：逐条 grep `SCALING_PLAN`、`docs/tasks`、`results` 核对；检查指向 openspec/specs、openspec/changes、openspec/archive、openspec/records、openspec/evidence、openspec/SCALING_ROADMAP.md 的相对链接。
  - evidence 位置：链接检查与引用检查结果。

## 11. 迁移证据与任务记录进 openspec

- [x] 11. 迁移 docs/tasks 与 results 进 openspec/records 与 openspec/evidence
  - 修改对象：`docs/tasks/phase1-storage/`、`docs/tasks/storage-interface.md`、`results/`。
  - 完成条件：已完成任务记录文档迁入 `openspec/records/phase1-storage/`（m4.5、m4h-2/3/4、phase1-storage、storage-interface、vector-validation）；可复现脚本与原始结果迁入 `openspec/evidence/phase1-storage/`（benchmark/ 存 partition_test.py、recall_bench2.py、benchmark README；results/ 存 *.txt 与 m4h4_*.json）；叙述与脚本/数据分离（D6）。
  - 验证方法：git status 显示为 rename；records 与 evidence 目录内容分类正确。
  - evidence 位置：`openspec/records/` 与 `openspec/evidence/` + git status。

## 12. 迁移 ROADMAP/tombstone/README/archive 进 openspec 并退役 docs

- [x] 12. SCALING_ROADMAP.md、SCALING_PLAN.md tombstone、README、archive 历史材料迁入 openspec，docs 原路径退役
  - 修改对象：`docs/scaling/{SCALING_ROADMAP.md,SCALING_PLAN.md,README.md}`、`docs/scaling/archive/*`。
  - 完成条件：`SCALING_ROADMAP.md` → `openspec/SCALING_ROADMAP.md`；`SCALING_PLAN.md` tombstone → `openspec/archive/SCALING_PLAN.md`（与快照同目录）；`README.md` → `openspec/README.md`（入口更新）；`docs/scaling/archive/*` → `openspec/archive/*`；迁移后 `docs/scaling/`、`docs/tasks/`、`results/` 空目录删除，openspec 成为唯一维护目录。
  - 验证方法：git status 显示为 rename；docs/ 仅剩 Graph/；openspec 目录结构符合 D6。
  - evidence 位置：git status + 目录列表。

## 13. 修正移动后相对链接与全仓引用

- [x] 13. 修正快照 ./archive/ 旧链接与全仓路径引用
  - 修改对象：`openspec/archive/SCALING_PLAN-2026-08-22.md`（`./archive/architecture_comparison.md` → `./architecture_comparison.md`、`./archive/migration_checklist.md` → `./migration_checklist.md`）；移动文件间的相对链接；config.yaml context 路径（`docs/scaling/SCALING_ROADMAP.md` → `openspec/SCALING_ROADMAP.md`）。
  - 完成条件：全仓 Markdown 相对链接有效（无 404）；无指向 docs/scaling、docs/tasks、results 的活跃引用残留。
  - 验证方法：重跑全仓相对链接检查脚本；grep SCALING_PLAN / docs/tasks / results 逐条核对。
  - evidence 位置：链接检查输出 + 引用核对结果。

## 14. 用户审阅 apply 结果

- [x] 14. 用户审阅 apply 结果 gate
  - 修改对象：无（gate）。
  - 完成条件：向用户展示 apply 结果（迁移矩阵、SCALING_ROADMAP、历史快照、tombstone、README、records/evidence 迁移、config.yaml、引用检查），用户确认后才执行 sync-specs。
  - 验证方法：用户确认记录存在。
  - evidence 位置：本 change 审阅记录。

## 15. 审阅通过后执行 sync specs

- [x] 15. 审阅通过后执行 sync specs
  - 修改对象：`openspec/specs/scaling-governance/spec.md`。
  - 完成条件：用户确认后运行 sync（本 CLI 版本无独立 `sync-specs` 命令，sync 由 `openspec archive` 完成——archive 会更新 main specs）；验证 `openspec/specs/scaling-governance/spec.md` 存在且含全部 requirement。
  - 验证方法：`openspec validate` 通过、main specs 验证完成。
  - evidence 位置：`openspec/specs/scaling-governance/spec.md` + CLI 输出。

## 16. sync 验证通过后 archive change

- [x] 16. sync 验证通过后 archive change 并给出建议 commit message
  - 修改对象：`openspec/changes/establish-scaling-openspec-baseline` → `openspec/changes/archive/`。
  - 完成条件：sync 验证通过后 `openspec archive`；输出建议 commit message（描述三类事实来源治理基线、openspec 唯一维护目录与 SCALING_PLAN 退役，不加 Co-Authored-By、无 emoji），等用户审阅确认后再提交；不自动执行 commit。
  - 验证方法：archive 汇总记录同步结果与证据；`git status` 确认未自动提交。
  - evidence 位置：archive 输出 + 建议 commit message。

## 14. 本 change 闭环样例（作为后续 Phase change 模板）

- **branch/worktree**：当前 `main`，无独立分支/worktree（本 change 纯文档治理，直接在 main 工作树执行）。
- **修改对象**：每个 task 明确列出（见上）。
- **完成条件**：每个 task 的完成条件达成。
- **验证方法**：`openspec validate establish-scaling-openspec-baseline`（含 `--all`）；全仓 Markdown 相对链接检查（活跃文档无 404 相对路径）；`git diff --check` 无空白错误。
- **evidence 位置**：每个 task 记录的 evidence 路径。
- **openspec 状态更新**：tasks checkbox 逐项勾选（`- [ ]` → `- [x]`），`openspec status --change establish-scaling-openspec-baseline` 反映完成进度；SCALING_ROADMAP 状态仅在存在 evidence 时更新。
- **生命周期**：apply（本阶段）→ 用户审阅 gate → sync-specs → archive。
