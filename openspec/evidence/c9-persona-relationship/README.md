# c9-persona-relationship 证据与边界

> change：`openspec/changes/c9-persona-relationship/`
> 承接 M-P1 最后的体验阻塞项：tenant 独立人设与关系状态 + 一次性 onboarding。
> 前置：C5（E5）/ C1（E6）/ C3（tenant serial lane）均已归档；基于
> `feature/p0-retention-wiring` tip（定向回归需含 tests/retention/）。

## 1. 落地内容（代码）

| 文件 | 变更 |
|---|---|
| `alembic/versions/e1f3a5c7b9d2_c9_persona_relationship.py` | **新**：persona_templates / tenant_persona_profiles（tenant_id PK、无 updated_at） / persona_audit_events；无 revision 表 |
| `bootstrap/db/models/persona.py` + `__init__.py` | **新** 三表 ORM |
| `bootstrap/db/repository/persona_repo.py` | **新** PersonaRepository：模板 CRUD/启停、onboarding 单事务提交（抢位 + memory_items self 种子 + 审计）、审计读取面；无修改快照方法 |
| `bootstrap/persona.py` | **新** resolve_persona_snapshot（per-turn 快照）+ PgRelationshipIO（单写者写缝：upsert + 审计同事务） |
| `agent/core/types.py` | PersonaSnapshot（frozen）+ ContextRequest.persona_snapshot |
| `agent/core/prompt_block.py` | TurnContext.persona_snapshot；Identity/SelfModel 块快照优先、单体回退；快照下禁 static 缓存（防跨租户泄漏） |
| `agent/context.py` + `agent/prompting/assembler.py` | 快照穿线（render → assemble → _build_system_prompt_result → TurnContext） |
| `agent/lifecycle/types.py` + `phases/prompt_render.py` | PromptRenderInput/Ctx.persona_snapshot 穿线 |
| `agent/core/passive_turn.py` | run_turn 在 tenant lane 内解析一次快照（异常 fail-open 回退单体语义）；bind_persona_resolver |
| `agent/looping/core.py` | bind_persona_resolver 委托 + prompt_breakdown 公开属性 |
| `proactive_v2/memory_optimizer.py` + `bootstrap/proactive.py` | relationship_io 注入 + tenant_id 穿线（PG seam / 单体文件二态） |
| `bootstrap/app.py` | PG durable 装配 resolver + relationship_io + chat server breakdown provider |
| `bootstrap/chat_api.py` | persona/status、persona/templates、persona/onboarding（201/409/413）端点；uploads/media/WS onboarding 门禁；无修改端点；source-breakdown（admin/debug） |
| `scripts/persona_admin.py` | **新** 模板管理 CLI（task 2.4 CLI 方案） |
| `frontend/chat/src/persona.ts` + `OnboardingPanel.tsx` + `App.tsx` | onboarding 流程 UI（status 404 = dev 不拦截；提交后无再进入入口） |
| `config.example.toml` | 无新增（`[agent.persona]` 语义不变：seed + 单体兼容） |

## 2. 验证证据

| 文件 | 内容 |
|---|---|
| `task-1.1-migration.md` | 迁移闭环 + 三表/无 revision 断言 + 实测前置事实（分区/vector 列/JSONB 绑定） |
| `task-2.1-endpoints.md` | 端点语义矩阵 + 门禁 + 前端 + 模板 CLI |
| `task-3.1-prompt.md` | 四层穿线链、缓存防线、单写者与生效时机、config 边界 |
| `task-4.2-recovery-runbook.md` | PITR 恢复 runbook（交付物） |
| `task-5.1-targeted.txt` | 定向回归（personas/retention/auth_provisioning/control_plane/observability_privacy） |
| `task-5.2-full-regression.txt` | 全量回归（对照 1902 基线） |
| `task-5.3-pyright.txt` | 改动文件 0 errors / project 38 基线 |
| `task-5.5-checklist.md` | checklist/task-09 回填说明 |

## 3. 已知边界（记录于 design，不属缺陷）

- RelationshipState 复用 `memory_items(memory_type='self')`（ADR-1，§5.7.1 表名可调
  条款）；embedding 列的 asyncpg 绑定限制 → 种子/写缝用显式列名原生 SQL。
- onboarding 以 provisioning 分区就绪为前置（真实时序天然满足）。
- breakdown 的 admin 可视化宿主为 Dashboard（既有 admin 门禁）；chat_api 侧
  dev/debug 面 + 认证模式普通用户 404。
- p0-retention-wiring 的三个 owner 数值仍为暂定默认（config 可改），与本 change 无关。
