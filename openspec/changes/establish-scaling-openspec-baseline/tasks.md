## 1. SCALING_PLAN 内容归类与迁移矩阵

- [x] 1.1 按六类（当前规范 / 稳定治理约束 / 未来候选工作 / 历史实施细节 / benchmark-evidence / 已过期状态）建立 `docs/scaling/SCALING_PLAN.md` 的内容迁移矩阵，输出到 apply 记录，验证矩阵覆盖 SCALING_PLAN 全部章节（0-13）且无遗漏
- [x] 1.2 与用户确认归类结果，验证无未来设想（TurnAdmission、outbox、多 Worker、WebChat 公网、Redis cache、PostgreSQL primary 切换）被误归为"当前规范"

## 2. scaling-governance delta spec 与 config.yaml 规则

- [x] 2.1 确认 change 内 delta spec 路径为 `specs/scaling-governance/spec.md`（capability 已由 scaling-docs-baseline 更名），验证 `openspec validate` 引用一致
- [x] 2.2 更新 `openspec/config.yaml` `context`：移除"扩展目标 5000 用户（见 docs/scaling/SCALING_PLAN.md）"的活跃引用，改为指向 openspec/specs（scaling-governance），验证 yaml 语法合法（`python -c "import yaml;yaml.safe_load(open('openspec/config.yaml'))"`）
- [x] 2.3 更新 `openspec/config.yaml` `rules.tasks` 与 `operations`：以"openspec change 状态更新（tasks checkbox / `openspec status`）"替换"roadmap 状态更新点"，验证 `openspec instructions tasks --change establish-scaling-openspec-baseline --json` 的 rules 字段可见新表述

## 3. 冻结历史快照

- [x] 3.1 `git mv docs/scaling/SCALING_PLAN.md docs/scaling/archive/SCALING_PLAN-2026-08-22.md`，验证 `git status` 显示为 rename
- [x] 3.2 在快照头部增加历史状态块：冻结日期为 2026-08-22、文件可能含跨时期积累的过期或内部矛盾状态、不再修正与维护、不作为当前状态或规格依据，验证头部块醒目且含全部要素
- [x] 3.3 不逐项修复快照正文中的过期/矛盾表述（如"当前结论"与 Phase 1 current-state 的冲突原样保留），验证 `git diff` 除头部块外无其他正文改动

## 4. 原路径 tombstone/navigation

- [x] 4.1 在 `docs/scaling/SCALING_PLAN.md` 原路径创建简短 tombstone/navigation 文件，仅指向 openspec/specs/、openspec/changes/、历史快照、docs/tasks/、results/ 与 `openspec list`/`openspec status` 查询命令，验证所有链接可达且无当前状态内容

## 5. 更新 docs/scaling/README.md

- [x] 5.1 README 移除"SCALING_PLAN.md 为活跃 source of truth"条目，改为四类入口：openspec/specs（当前规格）、openspec/changes（活跃工作）、archive/（历史材料）、docs/tasks 与 results（evidence），验证 README 不再把 SCALING_PLAN 列为活跃 source of truth
- [x] 5.2 使用原则第 2 条 source-of-truth 优先级移除 SCALING_PLAN，改为"已验证代码与测试证据 > openspec/specs > openspec/changes（含 archive）> 历史材料"，验证表述与 design 一致

## 6. 更新历史文档 banner 和链接

- [x] 6.1 `docs/scaling/archive/architecture_comparison.md` 与 `archive/migration_checklist.md` 头部由"以 SCALING_PLAN 与已验证代码为准"改为"当前规格以 openspec/specs 为准；当前 change 状态以 openspec/changes 与 `openspec status` 为准；实现事实以代码和测试证据为准"，验证两文件头部更新
- [x] 6.2 `git mv docs/main-test-debt-prompt.md docs/scaling/archive/`，头部标注任务性质由 OpenSpec 后续 change 承接，验证 archive/ 下历史文档头部标记齐全
- [x] 6.3 全仓非 archive 文档中对 SCALING_PLAN 的活跃引用改为指向历史快照或 OpenSpec，验证相对路径有效（`ls` 检查）

## 7. 全仓引用检查

- [x] 7.1 全文搜索确认非 archive 当前文档不得再把 SCALING_PLAN 称为 source of truth、不再通过其 checkbox 管理当前状态，验证无违规引用（grep `SCALING_PLAN` 逐条核对）
- [x] 7.2 检查所有指向 openspec/specs、openspec/changes、docs/scaling/archive 的相对链接有效，验证无 404 相对路径

## 8. OpenSpec validation 与文档验证

- [x] 8.1 运行 `openspec validate --change establish-scaling-openspec-baseline` 全部通过，验证无 spec 缺失或格式错误
- [x] 8.2 确认未修改任何应用代码：`git status --porcelain` 仅包含文档与 openspec/ 文件，验证 `infra/`、`agent/`、`session/`、`memory2/` 无改动
- [x] 8.3 在本 change 的 tasks/design 中记录闭环样例：branch/worktree（当前 main，无独立分支）、验证证据（`openspec validate` + 引用检查）、openspec 状态更新（tasks checkbox / `openspec status`），作为后续 Phase change 的模板样例

## 9. 用户审阅 gate

- [ ] 9.1 向用户展示 apply 结果（迁移矩阵、历史快照、tombstone、README、banner 改动、引用检查），用户确认后才可执行 `openspec sync-specs`，验证用户确认记录存在
- [ ] 9.2 用户确认后运行 `openspec sync-specs --change establish-scaling-openspec-baseline`，验证 `openspec/specs/scaling-governance/spec.md` 存在且含全部 10 条 Requirement
- [ ] 9.3 sync 后运行 `openspec validate` 通过、main specs 验证完成，才可执行 `openspec archive`，验证 archive 汇总记录了同步结果与证据
- [ ] 9.4 输出建议 commit message（描述 SCALING_PLAN 退役与 OpenSpec 治理基线，不加 Co-Authored-By、无 emoji），等用户审阅确认后再提交，验证未自动执行 commit

## 10. 本 change 闭环样例（作为后续 Phase change 模板）

- **branch/worktree**：当前 `main`，无独立分支/worktree（本 change 纯文档治理，直接在 main 工作树执行）。
- **验证证据**：`openspec validate establish-scaling-openspec-baseline` 通过（含 `--all`）；全仓 Markdown 相对链接检查（活跃文档无 404 相对路径）；`git diff --check` 无空白错误。
- **openspec 状态更新**：本文件 tasks checkbox 逐项勾选（`- [ ]` → `- [x]`），`openspec status --change establish-scaling-openspec-baseline` 反映完成进度。
- **生命周期**：apply（本阶段）→ 用户审阅 gate → sync-specs → archive。
