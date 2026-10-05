# C9 Persona/Relationship — proposal

## Why

Pilot 的第一个真实用户进入 WebChat 时，会话人格来自**进程级全局变量**
（`agent/persona.py` 的 `NEXUS_IDENTITY`/`PERSONALITY_RULES` 模块全局 +
`config.toml [agent.persona]`），关系状态来自单体 `workspace/memory/SELF.md`
（`agent/memory.py` 的 `read_self()/write_self()` 文件读写）。这意味着：所有
tenant 共享同一份人设与关系状态（跨租户污染，§5.9.1 硬冲突）；M-P1 出口的
「首次进入完成一次性人设设置流程，提交后 tenant PersonaProfile/RelationshipState
可从 PostgreSQL 正确恢复且用户侧不能再次修改」不成立。C9 是公网开放前
M-P1 关闭的最后一个体验阻塞项（C10/C14 可并行后置）。

## What Changes

- 新建 capability **`persona-relationship`**：把「tenant 独立人设与关系状态、
  一次性 onboarding 后锁定、RelationshipState 单写者原地演化、无 revision 链」
  写成可验收契约。
- **DB schema（expand-only 迁移）**：`persona_templates`（管理员可选 Persona 模板）、
  `tenant_persona_profiles`（tenant 一次性提交后的固定快照）、
  `tenant_relationship_states`（tenant 当前可演化值，current-state 模型）。
  **不建** revision/version/CAS 表（grep + schema 断言验收）。
- **一次性 onboarding 流程**：首次登录进入人设设置——从管理员模板选择或编辑
  完整自由文本（沿用单体 `identity`/`personality_rules`/`self_model` 自由文本
  语义，不新增人格参数模型）；提交后保存 tenant 独立快照并**锁定用户侧编辑入口**
  （API + UI 双断言）；管理员模板的新增/停用只影响后续 onboarding，不静默覆盖
  已建 tenant。
- **RelationshipState 演化**：沿用单体 `read_self() → optimizer 计算 →
  write_self()` 原地覆盖语义，但落 PG 且在 C3 tenant 串行 lane / maintenance
  lock 内单事务单写者；更新成功后从该 tenant **下一轮 turn** 注入新 block，
  进行中 turn 用原 prompt snapshot（§5.7.3 生效时机）。
- **Persona 审计记录**：每次 Persona/Relationship 写入记录来源、触发 turn、
  时间（供 C5 admin audit / C12 观测复用）。
- **Prompt 四层组装**：RuntimeInvariant（代码维护）→ PersonaProfile（tenant 固定）
  → RelationshipState（tenant 演化）→ ChannelPolicy（channel adapter），prompt
  中来源可识别；`prompt source breakdown` 仅 admin/debug 开放，不展示模型隐藏
  思维链。
- **config.toml 边界**：只保留实例默认 persona（单体兼容）与 migration seed，
  不再承载 tenant 当前值；进程级 persona 全局在多租户 PG 路径上停用（dev/SQLite
  路径行为不变）。

## Capabilities

### New Capabilities

- `persona-relationship`: tenant 独立 PersonaProfile/RelationshipState 的 PG 存储、
  一次性 onboarding 与锁定、RelationshipState 单写者演化、prompt 四层组装与
  source breakdown、Persona 审计、无 revision 链约束。

### Modified Capabilities

（无——C5/C1/C3 的 spec 语义只被消费不修改；prompt 分层对本 change 之前的
行为是纯新增能力。）

## Impact

- **代码**：`bootstrap/db/models/`（新模型 + 迁移）、`bootstrap/db/repository/`
  （新 PersonaRepository）、`agent/persona.py`（进程全局 → tenant 解析接缝）、
  `agent/core/prompt_block.py`（四层组装 + source breakdown）、
  `bootstrap/memory.py` / optimizer 写路径（SELF.md → PG 单写者，tenant lane 内）、
  `bootstrap/chat_api.py`（onboarding 端点 + 登录后 onboarding 门禁）、
  `frontend/chat/`（onboarding 页 + 修改入口缺失）。
- **依赖前置**：C5（E5 登录 principal）✅ 已归档、C1（E6 tenant 派生）✅ 已归档、
  C3（tenant serial lane）✅ 已归档（P0 段）。
- **风险**：单体兼容路径（SQLite/dev mode）必须保持行为不变——四层组装在
  dev 路径回退到现有 `config.toml [agent.persona]` + `SELF.md` 文件语义。
