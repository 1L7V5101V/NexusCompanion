# Task-09 — Persona/Relationship tenant storage + optimizer concurrency（persona-relationship）

> 编号对应 PILOT_ROADMAP §5.9.10 第 9 项。状态标记复用 §8。

## 元数据

- **所属阶段**：主要里程碑 = P1
- **§5.9 引用**：§5.9.8（Persona 当前值更新与调试权限）、§5.7（人设/关系状态/多租户配置边界：5.7.1 配置分层、5.7.2 SELF.md 迁移、5.7.3 Prompt 组装）、§10 DECIDED（Persona/Relationship 存储语义、Persona 系列）
- **§6 出口条件引用**：P1 出口「首次进入完成一次性人设设置流程，提交后 tenant PersonaProfile/RelationshipState 可从 PostgreSQL 正确恢复且用户侧不能再次修改」
- **状态**：planned

## 目标

落地 PG `persona_templates` / `tenant_persona_profiles` / `tenant_relationship_states` 表；一次性人设设置流程（可选择管理员 Persona 或编辑自由文本，提交后保存 tenant 独立快照并**锁定用户侧编辑入口**）；PersonaProfile 提交后固定（current-state 模型，**不建产品级 revision 链**，异常恢复走 PostgreSQL backup/PITR）；RelationshipState 沿用单体 `SELF.md` 原地更新语义（tenant 串行 lane / maintenance lock + 单事务单写者）；Persona 审计记录；prompt 四层来源（RuntimeInvariant / PersonaProfile / RelationshipState / ChannelPolicy）；`config.toml` 只做实例默认/兼容/seed；调试权限：prompt source breakdown 仅 admin/debug 开放。

## 输入

- 上游 change 产出：C5（首次登录流程 principal，E5）、C1（tenant 派生，E6）、C3（tenant serial lane）
- roadmap 冻结决策：§5.7 全节（配置分层/self seed/生效时机）、§5.9.8、§10 DECIDED（Persona/Relationship 存储语义：PersonaProfile 固定、RelationshipState 租户单写者原地更新、无 revision 链、Consolidation 阈值保持当前默认）
- 现有代码锚点：`agent/persona.py`（Persona 进程级全局）、`agent/memory_pg.py`、`bootstrap/db/`（tenant 存储模式）
- 依赖前置：C5（E5）+ C1（E6）+ C3（tenant serial lane）

## 输出

- DB schema：`persona_templates` / `tenant_persona_profiles` / `tenant_relationship_states` 表 + 迁移
- 代码：onboarding 流程（一次性人设设置 + 提交后锁定）、optimizer 单写者、Persona 审计记录、prompt 组装分层（四来源）、prompt source breakdown（admin/debug）
- 测试/证据：onboarding 后修改入口缺失测试、跨 tenant 隔离测试、并发写测试、恢复测试、grep 无 revision 表、权限测试、生效时机测试、恢复 runbook

## 验收标准

- [ ] 提交后 PersonaProfile 用户侧不可二次修改 — 验证：onboarding 后修改入口缺失测试（UI + API 双断言）
- [ ] RelationshipState 随互动原地更新、per-tenant 隔离（主对话/Proactive/Drift 不读他人 tenant 人设） — 验证：跨 tenant 隔离测试
- [ ] 并发 optimizer 写不覆盖（串行 lane 内单写者 + 单事务） — 验证：并发写测试
- [ ] 重启后从 PG 恢复当前值 — 验证：恢复测试
- [ ] 不建产品级 revision 链/CAS（**无版本表**） — 验证：grep 无 revision/version 表 + schema 断言
- [ ] prompt source breakdown 只 admin/debug 开放；不展示模型隐藏思维链 — 验证：权限测试
- [ ] 异常恢复走 PostgreSQL backup/PITR，非产品内 revision 切换 — 验证：恢复 runbook 演练
- [ ] RelationshipState 更新从下一轮 turn 注入；进行中 turn 用原 prompt snapshot（§5.7.3 / §10 Persona 生效时机） — 验证：生效时机测试
- [ ] `config.toml` 只保留实例默认/单体兼容/migration seed，不承载 tenant 当前值 — 验证：配置边界断言

> 判定「真正完成」而非「执行过」：「用户侧不能再次修改」以修改入口缺失测试（而非文档声明）为准；「无 revision 链」以 grep/schema 断言为准。

## 独立性边界（不与其他任务重复）

- 本任务拥有：persona/relationship 表、onboarding 流程、optimizer 单写者、prompt 四层组装、prompt source breakdown
- 本任务不触碰：memory engine 选择/绑定（C14）、auth 端点（C5，只消费 principal）、canonical 表（C1，只消费 tenant 派生）
- 共享 seam 协议：消费 C5 auth principal 与 C1 tenant 派生；RelationshipState 在 C3 的 tenant serial lane 内更新；Persona 审计记录供 C5 admin audit / C12 观测复用

## 依赖

- **左依赖（必须先完成）**：C5（E5：首次登录流程）+ C1（E6：tenant 派生）
- **右依赖（本任务前置于）**：无独立下游 change
- **可并行**：C6 / C7 / C10 / C11 / C14

## 风险与需冻结决策

- §10 DECIDED「Persona/Relationship 存储语义」：current-state 模型已冻结（固定值 + 原地演化 + tenant 单写者）；无 revision 保留周期设计，恢复走 backup/PITR。
- §10 DECIDED「Consolidation 阈值与失败语义」：保持 `memory_window=40`、`keep_count=20`、guard threshold=30、失败立即阻断 turn；若要改变语义另开 change。
- 风险：Persona 进程级全局残留跨 tenant 污染 → 验收第 2 条隔离测试覆盖主对话/Proactive/Drift；「prompt 四层」与单体体验不一致 → 以当前单体 `identity`/`personality_rules`/`self_model` 自由文本语义为默认基线（§6 P0），不新增复杂人格参数模型。