# C9 Persona/Relationship — 实施任务

> 对应 `openspec/changes/c9-persona-relationship/`；design.md ADR-1..ADR-7 为实现约束，偏离需先改 design。
> 分支：`feature/c9-persona-relationship`（worktree `D:/1/wt-c9`，基于 main tip；venv junction 共享主仓库 .venv）。
> 证据统一落 `openspec/evidence/c9-persona-relationship/`（每条任务给出命令与结果）。
> 环境：本地 PG 5433（便携实例，`127.0.0.1`）；集成测试 `NEXUS_TEST_PG_URL` +
> `NEXUS_REQUIRE_PG=1` 走独立 scratch 库（模式同 `tests/retention/conftest.py`）。
> 前置：C5/C1/C3 已归档 ✅；p0-retention-wiring 已归档（2026-10-04）。

## 1. 迁移与模型（ADR-2/ADR-7）

- [ ] 1.1 新增 alembic revision（expand-only，`down_revision` 取实现时 `alembic heads` 实测值）：
  `persona_templates`（name UNIQUE + 三块 TEXT + enabled）、
  `tenant_persona_profiles`（tenant_id UNIQUE + source CHECK ∈ ('template','custom') +
  template_id NULLABLE FK + 三块 TEXT NOT NULL + created_at，**无 updated_at**）、
  `persona_audit_events`（tenant_id + actor/action CHECK + turn_id NULLABLE +
  detail JSONB + created_at，索引 (tenant_id, created_at)）。**不建**任何
  revision/version/history 表。验证：`upgrade → downgrade -1 → upgrade head` 闭环
  实跑 5433；`information_schema` 断言三表存在且无 revision 类表。证据：
  `task-1.1-migration.md`
- [ ] 1.2 模型新增 `bootstrap/db/models/persona.py`：三表 ORM（列与迁移逐字一致）；
  `bootstrap/db/repository/persona_repo.py`：`PersonaRepository`（模板
  CRUD+启停、tenant profile 原子提交 `ON CONFLICT(tenant_id) DO NOTHING`、审计追加、
  读取面按 tenant 过滤）。验证：repo 单元用例（真 PG）：模板变更不覆盖已提交
  快照；并发双提交只成功一份。证据：`task-1.2-repository.md`

## 2. Onboarding 流程与锁定（ADR-3）

- [ ] 2.1 `bootstrap/chat_api.py` 新增端点：`GET /api/persona/status`（当前 tenant
  是否已完成 onboarding + 模板目录摘要）、`GET /api/persona/templates`（启用模板
  列表，仅 onboarding 未完成时可见正文）、`POST /api/persona/onboarding`
  （source=template/custom；原子提交 profile + 初始 RelationshipState
  （`memory_items` self 种子）+ 审计行，单事务）。用户面统一认证门禁复用
  `_require_user_session`。验证：API 用例——未认证 401；模板与自由文本双路径
  201；重复提交 409 语义。证据：`task-2.1-endpoints.md`
- [ ] 2.2 onboarding 门禁：PG 多租户模式下，无 profile 行的 tenant 调用户面
  会话写路径（sessions/uploads/media/ws 消息提交）返回 403 + 机器可读码
  `persona_onboarding_required`；admin 面、认证面、dev/SQLite 路径不受影响。
  验证：门禁矩阵用例（PG/dev × 用户/admin × 各端点）。证据：`task-2.2-gate.md`
- [ ] 2.3 修改入口缺失：不实现任何 profile 修改端点（PATCH/PUT/DELETE 一律 404）；
  `frontend/chat/` onboarding 页（模板选择/自由文本 + 提交后跳聊天），已 onboarding
  的 UI 不渲染任何编辑入口。验证：负向 API 用例（PATCH/PUT/DELETE → 404）+
  前端 grep 无 profile 编辑调用。证据：`task-2.3-locked.md`
- [ ] 2.4 管理员模板管理（CLI 或最小 admin 端点，二选一记录于 design 偏差注记）：
  create/disable/list；修改/停用不影响已建 tenant 快照。验证：用例断言
  disable 后已 onboarding tenant 的 prompt 注入不变。证据：并入 `task-2.1-endpoints.md`

## 3. Prompt 四层与单写者（ADR-4/ADR-5）

- [ ] 3.1 `agent/core/prompt_block.py`：身份块与行为规则块在 PG 多租户模式下按
  `WorkContext.tenant_id` 解析 `tenant_persona_profiles`（identity → 身份、
  personality_rules → 行为规则）；`SelfModelPromptBlock` 不动；onboarding 未完成
  或 SQLite/dev 模式回退进程全局/`config.toml` 语义（逐字不变）。验证：
  PG 用例——已 onboarding tenant 注入其 profile 内容；未 onboarding/dev 回退
  旧行为；breakdown 四层 label 齐全。证据：`task-3.1-prompt.md`
- [ ] 3.2 跨 tenant 隔离负向：tenant A/B 双 onboarding，A 的主对话/Proactive/
  Drift 组装结果与日志不含 B 的任何 persona 内容（进程全局残留负向测试）。
  验证：三入口隔离用例。证据：`task-3.2-isolation.md`
- [ ] 3.3 单写者收束：PG 模式 optimizer `write_self` 调用方全部置于 tenant 串行
  lane（maintenance lock）内执行，状态更新单事务 + 同事务审计行
  （`relationship_update`，detail 不复制全文）。验证：并发两笔更新串行收束且
  审计行数=2；进行中 turn 用组装时 snapshot、下一轮注入新值（生效时机用例）。
  证据：`task-3.3-single-writer.md`
- [ ] 3.4 source breakdown 权限：breakdown 仅 admin/debug 端点返回（复用 C5 admin
  session 门禁），普通用户 404；内容只有来源 metadata，无隐藏推理。验证：
  权限矩阵用例。证据：并入 `task-3.1-prompt.md`

## 4. 配置边界与恢复（ADR-7）

- [ ] 4.1 `config.toml [agent.persona]` 在 PG 多租户模式仅作 onboarding 自由文本
  编辑器初始 seed 与单体兼容默认，不参与运行时 tenant 值解析（代码路径断言）。
  验证：修改 config 重启后已 onboarding tenant 快照不变的用例；恢复测试
  （重启后当前值从 PG 逐字恢复）。证据：`task-4.1-config-boundary.md`
- [ ] 4.2 恢复 runbook 片段（交付物）：PersonaProfile/RelationshipState 异常恢复 =
  PG backup/PITR，无产品内回滚入口；写入 `openspec/evidence/c9-persona-relationship/`。
  证据：`task-4.2-recovery-runbook.md`

## 5. 测试闸门与状态回填

- [ ] 5.1 定向回归：`NEXUS_REQUIRE_PG=1 pytest -q -W error tests/persona/ tests/retention/
  tests/auth_provisioning/ tests/control_plane/ tests/observability_privacy/`。
  验证：全绿且无 skip。证据：`task-5.1-targeted.txt`
- [ ] 5.2 全量回归：`NEXUS_REQUIRE_PG=1 pytest -q -W error tests/`。验证：对照
  p0-retention-wiring 后基线 **1902 passed / 0 failed** 不新增失败。证据：
  `task-5.2-full-regression.txt`（附非绿采样定性）
- [ ] 5.3 pyright：改动文件 `--level error` 0 errors；project 全局对照 **38 基线**
  不新增（分批跑）。证据：`task-5.3-pyright.txt`
- [ ] 5.4 无 revision 链终验：grep 无 revision/version/history 表 + 
  `information_schema` schema 断言（合并 1.1 证据）。证据：并入 `task-1.1-migration.md`
- [ ] 5.5 状态回填（仅在 5.1–5.4 有证据后）：`PILOT_ROADMAP_PROJECT_CHECKLIST.md`
  M-P1 相关行（§5.7/§5.9.8 门禁行、current focus/next decision）、
  `openspec/openspec-tasks-bundle/task-09-persona-relationship.md` 验收标准逐条
  勾选并补证据链接。证据：`task-5.5-checklist.md`
- [ ] 5.6 `openspec validate c9-persona-relationship` 通过；status 四件套 done。
  证据：并入 `task-5.1-targeted.txt` 末尾
