# Task-01 — canonical identity + PG conversation/message 基础（canonical-identity）

> 编号对应 PILOT_ROADMAP §5.9.10 第 1 项。状态标记复用 §8：`planned / in_progress / verified / blocked / deferred`。

## 元数据

- **所属阶段**：主要里程碑 = P0.5；P-1 产出 design/ADR，P0 落 PG schema 基础，P0.5 收尾 canonical stream + sequence
- **§5.9 引用**：§5.9.1（identity/session 硬冲突）、§5.9.2（canonical identity + Telegram binding 语义）、§5.9.9（数据模型/约束/首次启用）、§10 DECIDED（旧单体数据边界、DB rollout/rollback）
- **§6 出口条件引用**：P-1 出口（设计冻结，不标 verified）；P0.5 出口「canonical conversation/message stream 与 0-based per-conversation sequence」「统一收敛到 account→tenant→canonical conversation」
- **状态**：verified
  - 2026-09-05：完成并置 `verified`（§8：commit `e124dbf8`/`7d5e1e38` + 可复现证据 + design review 确认）。OpenSpec change `2026-09-05-c1-canonical-identity`（15/15 任务完成，已归档至 `openspec/changes/archive/`）；交付设计冻结 ADR-1..7、Alembic migration `e2b4d6f8a0c2`、identity resolver + sequence repository、契约 fixture、30 项测试；证据齐 `openspec/evidence/c1-canonical-identity/`（migration/并发 sequence/负向/grep 无 SQLite fallback/rollback drill 9 项 PASS）。

## 目标

冻结并落地服务端派生的 `account_id → tenant_id → canonical_conversation_id` 映射；在 PostgreSQL 建立 `test_accounts` / `canonical_conversations` / `canonical_messages` 基础表与唯一约束；per-conversation 0-based `BIGINT` sequence 由 PG 事务原子分配；遵守 §10 DECIDED：旧单体 SQLite 数据**不导入、不 fallback、不双写、不反向同步**。

## 输入

- 上游 change 产出：无（本任务是锚定根，无前置 change；P-1 产出 design/ADR 供自身与下游消费）
- roadmap 冻结决策：§5.9.2（canonical identity + Telegram binding 语义、binding 为不可信输入）、§5.9.9（数据模型/首次启用/schema evolution）、§10 DECIDED（旧单体数据边界 = 空历史开始；DB rollout = Create→Verify→Enable）
- 现有代码锚点：`alembic/versions/`、`bus/events.py`（identity/session 事件）、`infra/storage/tenancy.py`（tenant 派生）、`session/manager.py`（session 管理）、`bootstrap/db/models/session.py`、`bootstrap/db/repository/session_repo.py`
- 依赖前置：无

## 输出

- 设计/ADR：`openspec/records/` 或对应 change 的 design/ADR，内容覆盖 canonical identity 派生链、表约束、DDL、Create→Verify→Enable rollback 语义、不导入 SQLite 声明
- DB schema：Pilot 首次启用 Alembic migration（`test_accounts` / `canonical_conversations` / `canonical_messages` 表 + 索引 + 约束 + seed）
- 代码：身份映射 resolver（channel binding → account/tenant/canonical conversation）；per-conversation sequence 原子分配 repository
- 测试/证据：并发 sequence 分配测试、负向测试（无 binding 拒绝、不落 DEFAULT_TENANT）、无 SQLite fallback grep；`openspec/evidence/` 复现证据
- 契约 fixture：canonical identity 派生链 fixture（供 C2/C4/C5/C9/C10/C14 复用）

## 验收标准

- [x] 设计文档覆盖 §5.9.2 全部冻结语义（含「旧单体数据不迁入」、binding 不可信输入、canonical sequence 0-based 事务原子分配） — 验证：design review + §5.9.2 逐条对照 + `openspec/records/` 存档 → change design.md ADR-1..7（§5.9.2 逐条对照见其 §1 表），随 change 归档存档；design review 经用户确认（2026-09-05）
- [x] `alembic upgrade head` 在空 PG 创建全部实体且唯一约束符合 §5.9.9（`test_accounts.tenant_id` 唯一、`canonical_conversations.tenant_id` 唯一、`(conversation_id, sequence)` 唯一） — 验证：migration 测试在空 PG 上执行 → `pytest-migration.txt`（migration `e2b4d6f8a0c2`）
- [x] 并发 sequence 分配测试：多并发插入下 `(conversation_id, sequence)` 唯一、连续、0-based、跨 conversation 独立（A=0,1,2；B=0,1,2） — 验证：并发测试 + 行级断言 → `pytest-concurrency.txt`（2 会话 × 20 并发）
- [x] 无 binding 请求被拒绝、不落 `DEFAULT_TENANT` — 验证：负向测试失败即拒绝 → `pytest-identity.txt`（未知/空 principal/`default` tenant 拒绝、失败零写入）
- [x] 无 SQLite fallback/双写路径 — 验证：grep Pilot tenant 路径无「PG 查询失败回退 SQLite」代码 → `grep-no-sqlite-fallback.txt` + 静态契约测试（`pytest-static-contract.txt`）
- [x] Create→Verify→Enable 演练可回滚（关闭入口 + 停 provisioning，保留 PG 数据） — 验证：rollback drill 记录 → `rollback-drill.md` + `rollback_drill.py`（9 项 PASS）
- [x] 本 task 不触碰 inbox/outbox/turn/delivery 表（C2 边界）与 admission lane（C3 边界） — 验证：PR diff 范围检查 → `scope-check.txt`（commit `e124dbf8` diff 仅新增文件 + env.py/alembic_util 小改）

> 判定「真正完成」而非「执行过」：上述每条均需产生**可复现证据**（测试报告 / grep 输出 / rollback 演练记录），且与 `openspec/evidence/` 中脚本对应。→ 证据齐 `openspec/evidence/c1-canonical-identity/`；全量回归 1166 passed（1 个既有环境性失败，干净 HEAD 复现）+ pyright 零新增错误。

## 独立性边界（不与其他任务重复）

- 本任务拥有：`test_accounts` / `canonical_conversations` / `canonical_messages` 表、identity resolver、per-conversation sequence 分配 repository
- 本任务不触碰：inbox/dedupe/turn/tool/work/outbox/delivery 表（C2）、admission lane / queue / overload（C3）、auth 端点（C5）、Persona/Relationship 表（C9）、Telegram binding 表（C10）、attachment 表（C6）
- 共享 seam 协议：`tenant_id_for_channel()` 期间为 C3 的临时 admission key（E9 弱对齐）；canonical mapping 落地后切换（§3.4 实现地图行）；不导入/不 fallback/不双写 SQLite（§5.9.2/§5.9.9）

## 依赖

- **左依赖（必须先完成）**：无（锚定根，roadmap「先完成 canonical identity/control-plane change」）
- **右依赖（本任务前置于）**：C2（canonical 表 + sequence）、C4（canonical message/sequence）、C5（account→tenant 映射）、C10（canonical message stream）、C9（tenant 派生，E6）、C14（tenant identity）
- **可并行**：C3（弱耦合 E9）、C12（仅契约）、C13（独立）

## 风险与需冻结决策

- §10 OPEN FOR P-1 SPEC「Exact schema/DDL」：精确表名/列/index/constraint 名在 P-1 的 design/spec 产出，本 task 验收以空 PG `alembic upgrade head` 为依据。
- §10 DECIDED「DB rollout/rollback」：首次启用=Create→Verify→Enable，后续 evolution 才用 Expand→backfill→verify→cutover；rollback=关闭入口 + forward-fix/PITR，不反向同步 SQLite。
- §10 DECIDED「旧单体数据边界」：任何「PG 查询失败回退 SQLite」或双写路径均违反验收，须在实现期用 grep 防御。