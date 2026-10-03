# C9 Persona/Relationship — design

> 对应 `openspec/changes/c9-persona-relationship/`；ADR-1..ADR-7 为实现约束，偏离需先改本 design。
> 上游事实来源：PILOT_ROADMAP §5.7（全节）、§5.9.8、§10 DECIDED（Persona 系列）、
> task 契约 `openspec/openspec-tasks-bundle/task-09-persona-relationship.md`。

## Context

**现状事实（已核实，非推测）**

| 事实 | 位置 |
| --- | --- |
| Persona 是进程级模块全局（`NEXUS_IDENTITY`/`PERSONALITY_RULES`），`apply_persona_config` 启动时从 config 覆盖 | `agent/persona.py:18-46` |
| 身份块 prompt 来自静态构建（进程全局/工作区文件） | `agent/core/prompt_block.py:75-78`（`IdentityPromptBlock` priority 10） |
| RelationshipState（SELF.md 语义）在 PG 模式**已按 tenant 隔离**落 PG：`memory_items(memory_type='self')` 当前值 upsert | `agent/memory_pg.py:135-139`（`PgMemoryMarkdownStore` 构造即绑 tenant_id，`:190-215` 读写均带 tenant 过滤） |
| prompt 的 self 块经 `ctx.memory.read_self()` 读取（PG 模式即 tenant 当前值） | `agent/core/prompt_block.py:122-133`（`SelfModelPromptBlock` priority 30） |
| 单体 SQLite 模式 self 是文件 `memory_dir/SELF.md` | `agent/memory.py:151-156` |
| persona 全局消费者 | `agent/persona.py`、`agent/core/drift_turn.py` |
| onboarding 承接点：登录后用户面 API/WS 已有统一认证门禁 | `bootstrap/chat_api.py`（webchat-auth-wiring 已归档） |
| tenant 串行 lane / maintenance lock | C3（`c3-admission-queue-recovery`，已归档 P0 段） |
| 配置节 | `[agent.persona]`（`PersonaConfig`：identity/personality_rules/self_model 三块自由文本） |

**已具备的底座（本 change 不重复建设）**：PG 模式下 RelationshipState 的
tenant 隔离存储与读写路径已存在（上表第 3、4 行）——缺的是 PersonaProfile 的
tenant 存储、onboarding 流程与锁定、prompt 身份层的 tenant 化、单写者收束与审计。

## Goals / Non-Goals

**Goals:**
- onboarding：首次登录一次性人设设置（模板或自由文本）→ tenant 快照 → 锁定。
- PersonaProfile/RelationshipState 全部以 PG 当前值为准，重启可恢复。
- prompt 四层来源可识别，breakdown 仅 admin/debug。
- Persona 写入有审计记录；并发写由 tenant 串行 lane 收束。
- 单体/SQLite dev 模式行为完全不变（回退现有文件 + 进程全局语义）。

**Non-Goals:**
- 不建设 revision/version/history 表、CAS、产品内回滚（恢复 = PG backup/PITR）。
- 不新增人格参数模型（不做滑杆/好感度/关系状态机）。
- 不动 memory engine 选择（C14）、auth 端点（C5 只消费）、canonical 表（C1 只消费）。
- 不做 Persona 模板的多语言/富媒体管理界面（模板 = 三块自由文本的 CRUD + 启停）。

## Decisions

### ADR-1 RelationshipState 存储复用现有 tenant-scoped content seam，不建第二张状态表

**结论**：RelationshipState（当前可演化值）继续以 `memory_items(memory_type='self')`
（`PgMemoryMarkdownStore` 的 tenant 过滤读写）为唯一 PG 当前值存储；本 change 不新建
`tenant_relationship_states` 表。

**理由**：roadmap §5.7.1 明示「具体表名可以在实现 change 中调整，但两类 tenant 状态都以
PostgreSQL 当前记录为准」。现有 seam 已满足全部契约语义（tenant 隔离、当前值、optimizer
写路径已接通、prompt 读侧已接通）；另建一张表会制造同一值的第二个规范源，正是 §5.7.1
拒绝的「争夺最终解释权」形态。task-09 冻结 DDL 中的 `tenant_relationship_states` 依此条款
调整为「复用现 seam」——本 design 即该调整的记录。

**备选（不选）**：新建 `tenant_relationship_states` 并迁移 `memory_items.self` 数据：
多一次 expand 迁移 + 数据搬迁 + 双读兼容期，换来的只是表名与 DDL 文字一致，无行为差异。

### ADR-2 新表：`persona_templates` + `tenant_persona_profiles` + `persona_audit_events`（expand-only）

| 表 | 关键列 | 语义 |
| --- | --- | --- |
| `persona_templates` | id, name UNIQUE, identity/personality_rules/self_model 三块 TEXT, enabled BOOL, created_by, created_at, updated_at | 管理员可选 Persona；修改/停用只影响后续 onboarding（spec Scenario） |
| `tenant_persona_profiles` | tenant_id UNIQUE（一 tenant 一份）, source ∈ ('template','custom'), template_id NULLABLE FK, identity/personality_rules/self_model 三块 TEXT NOT NULL, created_at | onboarding 提交后的**固定快照**；无 updated_at 语义（不可变行，除 P1GATE 修复外不改） |
| `persona_audit_events` | id, tenant_id, actor ∈ ('user','optimizer','admin'), action ∈ ('onboarding_submit','relationship_update','template_create','template_disable'…), turn_id NULLABLE, detail JSONB（不复制正文全文）, created_at | Persona 写入审计；供 admin 下钻与 C12 观测复用 |

**理由**：PersonaProfile 是 onboarding 一次性提交的不可变快照（UNIQUE(tenant_id) 即
"提交后固定"的 schema 表达）；审计需要独立生命周期（C5 admin_audit_events 的
actor/target 语义不同，ADR 同 p0-retention-wiring ADR-6——不把两个未定承载方耦合成
一次表语义变更）。

### ADR-3 onboarding 流程：登录后 fail-closed 门禁 + 提交即锁定

**结论**：PG 多租户模式下，已完成登录但无 `tenant_persona_profiles` 行的 tenant，
用户面（`/api/chat/*` 会话读写、`/ws` 收发）返回 onboarding-required 语义（403 +
机器可读码），前端跳 onboarding 页；`POST /api/persona/onboarding` 原子校验
"该 tenant 无 profile 行"（`INSERT ... ON CONFLICT(tenant_id) DO NOTHING` 语义）并
写 profile + 初始 RelationshipState + 审计行（单事务）。提交成功后门禁自然放行；
用户侧**不存在**任何 profile 修改端点（404 而非 403——不泄露能力存在性），前端
也不渲染修改入口。

**理由**：API + UI 双重缺失是 task-09 验收第 1 条的判定方式（"以修改入口缺失测试
为准"）；原子性防双开窗口并发提交产生两份快照。

### ADR-4 prompt 四层组装：tenant 解析 + 单体兼容回退

**结论**：组装顺序固定为 RuntimeInvariant → PersonaProfile → RelationshipState →
ChannelPolicy。实现上：现有 `IdentityPromptBlock`/行为规则块在**PG 多租户模式**下
改为按 `WorkContext.tenant_id` 解析 `tenant_persona_profiles`（identity → 身份块、
personality_rules → 行为规则块；命中模板或自由文本一视同仁）；`SelfModelPromptBlock`
不动（PG 模式经 `ctx.memory.read_self()` 已是 tenant 当前值）。**解析不到 tenant
profile（onboarding 未完成）或 SQLite/dev 模式时，回退现有进程全局/`config.toml`
语义——dev 路径行为逐字不变。**

**理由**：§5.7.3「以当前单体实际语义为基线，不额外引入复杂人格参数」+ task-09
风险条目「prompt 四层与单体体验不一致」；turn 组装时一次性解析成不可变块，
天然满足「进行中 turn 用原 snapshot」（ADR-5 生效时机）。

### ADR-5 单写者与生效时机：optimizer 写路径收束进 tenant 串行 lane

**结论**：PG 模式下 `write_self()` 的全部调用方（MemoryOptimizer 及后续任何维护
路径）必须在 C3 tenant 串行 lane（maintenance lock）内执行；状态更新单事务完成，
同事务追加 `persona_audit_events`（action='relationship_update'，detail 只记来源/
触发 turn/字符数摘要，**不复制正文全文**）。更新成功后从下一轮 turn 生效——prompt
块在 turn 组装时解析一次即快照，无需额外失效机制。

**理由**：§5.7.2「同一 tenant 的更新必须经过 tenant 串行 lane / maintenance lock，
并在一个数据库事务内完成」；「既然写入者被约束为单写者，不额外引入 CAS」。

### ADR-6 prompt source breakdown：admin/debug 专用

**结论**：breakdown 清单（四层来源的类别、block label、字符预算等 metadata）由
admin/debug 专用接口返回；普通用户请求一律 404 语义；不展示模型隐藏推理。

**理由**：§5.7.3 + task-09 验收第 6 条（权限测试）；复用 C5 的 admin session 门禁，
不新建权限模型。

### ADR-7 无 revision 链：grep + schema 断言为验收

**结论**：不建 persona/relationship 的 revision/version/history 表；`config.toml
[agent.persona]` 在 PG 多租户模式仅作为**新 tenant onboarding 自由文本编辑器的
初始 seed** 与单体兼容默认，不参与运行时 tenant 值解析。

**理由**：§10 DECIDED「Persona/Relationship 存储语义」「Persona 当前值与异常恢复」；
验收以 grep 无 revision 表 + schema 断言为准（task-09「真正完成」判定条款）。

## Risks / Trade-offs

- **复用 seam 的耦合**：RelationshipState 挂在 `memory_items` 上，语义与"记忆条目"
  不同（它是 markdown 内容块）。缓解：`memory_type='self'` 已是该表的既有契约值，
  读写收口在 `PgMemoryMarkdownStore` 单点；若后续独立成表，只动该类内部。
- **onboarding 门禁的误伤面**：门禁只拦"无 profile 行的 tenant"的用户面写路径，
  admin 面、认证面不受影响；dev/SQLite 路径完全绕过门禁（行为不变）。
- **prompt 行为漂移**：身份块从进程全局改为 tenant 解析后，已 onboarding tenant 的
  prompt 与单体不同是**预期行为**（这正是本 change 的目的）；未 onboarding /
  dev 路径必须逐字不变——验收含回退语义测试。
- **进程级全局残留**：`agent/persona.py` 全局在 PG 多租户模式不再被 prompt 消费，
  但模块保留（dev 兼容 + seed 加载）；验收第 2 条跨 tenant 隔离测试覆盖主对话/
  Proactive/Drift 三入口。

## Rollback

- 运行期回退：onboarding 门禁与 tenant prompt 解析由存储后端与 profile 行存在性
  自然决定，`backend = sqlite` 即回到单体语义；PG 模式下无 profile 行的 tenant
  等价于门禁前状态（仅新增 onboarding 引导）。
- Schema 回退：三张新表 expand-only，downgrade 直接 DROP；无数据迁移依赖。

## 测试策略

- **onboarding/锁定**：提交原子性（并发双提交只成功一份）、提交后修改端点 404 +
  前端无入口、模板变更不覆盖快照、onboarding-required 门禁放行/拦截。
- **隔离**：跨 tenant 主对话/Proactive/Drift prompt 组装只含本 tenant 人设
  （进程全局残留负向测试）。
- **单写者与生效时机**：lane 内两笔更新串行收束、审计行同事务、进行中 turn 用旧
  snapshot、下一轮注入新值。
- **恢复**：重启后从 PG 恢复当前值；config 修改不影响已 onboarding tenant。
- **权限**：breakdown 普通用户 404 / admin 可见。
- **无 revision**：grep 断言无 revision/version/history 表 + information_schema 断言。
- **单体兼容**：SQLite/dev 模式 prompt 与门禁行为与现在逐字一致（回归守卫）。
- **回归**：`NEXUS_REQUIRE_PG=1 pytest -q -W error tests/` 全量不新增失败；
  改动文件 pyright 0 errors，project 全局对照 38 基线不新增。
