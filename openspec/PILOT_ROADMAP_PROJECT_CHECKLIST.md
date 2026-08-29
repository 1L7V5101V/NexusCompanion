# PILOT_ROADMAP_PROJECT_CHECKLIST — Pilot 项目进度观测

> **定位**：本文件是完整项目的**进度观测 checklist**，只含 checklist + 一行简短描述 + 状态。
> 详细描述一律以链接指向既有细节文件（`specs/`、`changes/`、`records/`、`evidence/`、代码）。
> **不承载**完整 evidence、迁移命令、逐 commit 历史或行为规范定义。
>
> **Source of truth 优先级**：已验证代码与测试证据 > `openspec/specs` > `openspec/changes`（含 archive）> 历史材料；
> Program 目标与总体决策以 [`PILOT_ROADMAP.md`](./PILOT_ROADMAP.md) 为准；
> 状态定义与完成状态规则（§8）见 [`PILOT_ROADMAP.md`](./PILOT_ROADMAP.md)，本文件只引用不复制。
>
> 状态标记复用 roadmap §8 状态（planned/in_progress/verified/blocked/deferred）；
> 勾选框仅在 `verified`（满足 roadmap §8：merge commit + 可复现证据）时勾选。checklist 是派生观测面，
> 状态更新只允许在有证据时进行，与 change 的 tasks / `openspec status` 保持同步。

## 当前进度（来自 PILOT_ROADMAP §6）

- **当前阶段**：P-1 编码前决策冻结；现有 PostgreSQL + pgvector 存储基础、迁移和可观测性已有 verified 证据，但 Pilot 目标能力尚未实现。
- **current focus**：把 canonical identity、durable ingress/outbox/delivery、Auth/provisioning/browser security、WebSocket、bounded admission、persistence/backup ownership、ToolExecutionContext、RuntimeSnapshot/hooks/secrets、Persona、schedule、attachment、observability/privacy 和 schema/migration 固化为 ADR/design/spec。
- **current blocker**：在 5.9 对应设计门禁完成前，不开始 WebChat、认证/provisioning、Telegram binding、durable delivery、tenant tool/runtime snapshot、schedule/attachment 或相关 migration 的实现。
- **next decision**：按 [`PILOT_ROADMAP.md` §5.9.10](./PILOT_ROADMAP.md) 拆分首批 OpenSpec changes，并先完成 canonical identity/control-plane change。
- 详见 [`PILOT_ROADMAP.md` §5.9](./PILOT_ROADMAP.md)。

## GOV 治理与文档基线

- [x] **GOV 治理与文档基线** — 三类事实来源生效，SCALING_PLAN 退役；`verified`
  → [spec](specs/scaling-governance/spec.md) · [change archive](changes/archive/2026-08-23-establish-scaling-openspec-baseline/) · 证据：merge `e50732d6`
  - [x] **OpenSpec / Roadmap / 代码·测试·证据三类事实来源职责分离** → [scaling-governance spec](specs/scaling-governance/spec.md)
  - [x] **capability → change → evidence 追踪闭环与生命周期**（apply→review→sync-specs→archive） → [scaling-governance spec](specs/scaling-governance/spec.md)
  - [x] **SCALING_PLAN 退役为历史快照 + tombstone 导航** → [archive tombstone](archive/SCALING_PLAN.md)

## Phase 1 已完成（storage foundation，merge `0a83314d`）

- [x] **C1 Storage Foundation（PG + pgvector）** — SQLite/PG 双 adapter、tenant 贯穿、pool + bounded executor、provisioning 控制面；`verified`
  → [主记录](records/phase1-storage/phase1-storage.md) · [M4.5 硬化](records/phase1-storage/m4.5-architecture-hardening.md) · 证据：merge `0a83314d` + [5000 tenant 基准](evidence/phase1-storage/results/m4h4_partition_provisioning.json)
  - [x] **SQLite/PG 双 adapter 与共同 interface**（M4H-1） → [storage-interface 契约](records/phase1-storage/storage-interface.md) · [interfaces.py](../infra/storage/interfaces.py)
  - [x] **tenant 贯穿（TenantContext/TenantResolver + 各通道派生）**（M4H-2） → [m4h-2-tenant-wiring](records/phase1-storage/m4h-2-tenant-wiring.md) · [tenancy.py](../infra/storage/tenancy.py)
  - [x] **pool + bounded executor（event-loop 隔离）**（M4H-3） → [m4h-3-connection-isolation](records/phase1-storage/m4h-3-connection-isolation.md) · [runtime.py](../infra/storage/runtime.py)
  - [x] **provisioning 控制面（readiness gate + control worker + 幂等 DDL）**（M4H-4） → [m4h-4-partition-provisioning](records/phase1-storage/m4h-4-partition-provisioning.md) · [provisioning.py](../infra/storage/provisioning.py)
  - [x] **5000 tenant provisioning 基准** → [m4h-4-benchmark](records/phase1-storage/m4h-4-benchmark.md) · [原始结果](evidence/phase1-storage/results/m4h4_partition_provisioning.json)
  - [x] **merge readiness 与全量验证**（M4H-5） → [m4.5-architecture-hardening](records/phase1-storage/m4.5-architecture-hardening.md)

## Phase 0 前置 · C0 可观测性与负载工具（独立 change/worktree）

> change：[`c0-observability-load`](changes/archive/2026-08-23-c0-observability-load/)（已 archive `2026-08-23-c0-observability-load`；spec `observability-load` 已 sync 至 [spec](specs/observability-load/spec.md)）；与 Phase 1B 并行，为 C1B 基准与 C1D 验收提供可重复工具

- [x] **C0 可观测性与负载工具** — 可重复负载/基准工具、指标导出、turn_id 追踪（分段到当前 hop）；`verified`
  - [x] **可重复负载工具**（turn 级 harness，sqlite/postgres 双后端，结果 JSON 入库） → [load-harness 证据](evidence/c0/load-harness.md) · [results](evidence/c0/results/)
  - [x] **指标注册与导出**（JSON + Prometheus 文本，dashboard `/metrics` 消费） → [dashboard-metrics 证据](evidence/c0/dashboard-metrics.md)
  - [x] **turn_id 统一追踪表面**（当前 hop；C2/C3 端到端明确留后） → [trace-store 证据](evidence/c0/trace-store.md)

## Phase 1B 已完成（M5/M6，merge `1788de40`）

> change：[`phase1b-migration-cutover`](changes/archive/2026-08-26-phase1b-migration-cutover/)（已 archive `2026-08-26-phase1b-migration-cutover`；spec `storage-migration` 已 sync 至 [spec](specs/storage-migration/spec.md)）

- [x] **C1B 迁移工具（M5）** — 批量 COPY、断点续传、机器可读校验；`verified`
  - [x] **批量 COPY / 批量 insert**（可配置 batch、进度） → [导入证据](evidence/phase1b/results/phase1b-import-v1-import.json)（12,567 行，10.11s）
  - [x] **断点续传与幂等重跑** → [idem 证据](evidence/phase1b/results/idem-rerun-import.json) · [checkpoint.py](../scripts/migrate/checkpoint.py)
  - [x] **机器可读校验（逐表行数/字段 hash/语义抽样）+ 迁移证据入库** → [校验证据](evidence/phase1b/results/phase1b-verify-v1-verify.json)（四维全绿，exit_code=0）
- [x] **C1C 主数据源切换（M6）** — S0-S4 状态机切 PostgreSQL primary；`verified`
  - [x] **S0-S4 状态机**（SQLite primary → shadow import → PG shadow write/read verify → PG primary → SQLite retirement） → [state.py](../scripts/migrate/state.py) · [promote 证据](evidence/phase1b/results/phase1b-promote-v1-promote.json)
  - [x] **staging cutover + 对账** → [audit 证据](evidence/phase1b/results/phase1b-audit-v1-audit.json) · [promote 证据](evidence/phase1b/results/phase1b-promote-v1-promote.json)
  - [x] **PITR 恢复与回滚演练** → [PITR 证据](evidence/phase1b/results/phase1b-pitr-v1-pitr.json)（RPO≤5min/RTO≤30min runbook）
  - [x] **全量验证无回归**（pytest 1096 passed / 0 failed，pyright 基线一致） → [baseline](evidence/phase1b/baselines/baseline_feature_phase1b_migration.md)

## Pilot 阶段

- [ ] **P-1 编码前决策冻结** — 将影响协议、表结构、授权和恢复的选择固化为 ADR/design/spec；`planned`
  - [ ] **Canonical identity 与 Telegram Bot 私聊绑定**（`account → tenant → canonical conversation`、Telegram 用户与 Bot 私聊身份的一对一绑定、现有持久化数据/身份关系迁移、dry-run 生成逐旧 session 迁移去向/动作/冲突清单） → outcome 见 [PILOT_ROADMAP §5.9.2](PILOT_ROADMAP.md)
  - [ ] **Auth/admin/browser security**（分离 Cookie、CSRF/Origin、timeout、401/403、原子兑换、`pilot-admin` bootstrap/rotate/revoke/disable/enable；recovery token 轮换默认保留有效 browser sessions，泄露时再显式 revoke） → outcome 见 [PILOT_ROADMAP §5.9.3](PILOT_ROADMAP.md)
  - [ ] **WebSocket protocol contract**（hello、client_message_id、sequence、durable terminal、slow consumer） → outcome 见 [PILOT_ROADMAP §5.9.4](PILOT_ROADMAP.md)
  - [ ] **Admission 与 overload policy**（tenant lane；interactive 128 / per-tenant 16 / maintenance 64 / WS 256-soft 192；LLM/embedding/MCP/process 30/4/8/2；LLM 429 退避与指标） → outcome 见 [PILOT_ROADMAP §5.9.5](PILOT_ROADMAP.md)
  - [ ] **PostgreSQL durable control plane 与 restart recovery**（turn/tool/work/outbound final、unknown/compensation） → outcome 见 [PILOT_ROADMAP §5.9.6](PILOT_ROADMAP.md)
  - [ ] **ToolExecutionContext 三层注入边界与普通 tenant 首版工具白名单（精确 tool id）** → outcome 见 [PILOT_ROADMAP §5.9.7](PILOT_ROADMAP.md)
  - [ ] **Persona/Relationship 当前值语义**（Persona onboarding 后固定、RelationshipState 沿用单体原地更新、tenant 单写者、无产品级 revision/CAS、PITR 恢复与 debug 权限） → outcome 见 [PILOT_ROADMAP §5.9.8](PILOT_ROADMAP.md)
  - [ ] **数据模型、唯一约束和 migration/rollback**（expand → 500-row idempotent backfill → verify → cutover → 30-day compatibility → contract） → outcome 见 [PILOT_ROADMAP §5.9.9](PILOT_ROADMAP.md)
  - [ ] **独立 capability/change 依赖图**（identity/control-plane、WebChat、auth/provisioning、attachment、tool/snapshot、Persona、Telegram、schedule、observability、retrieval） → outcome 见 [PILOT_ROADMAP §5.9.10](PILOT_ROADMAP.md)
  - [ ] **Ingress acceptance、canonical message、outbox 与 delivery transaction** → outcome 见 [PILOT_ROADMAP §5.9.11](PILOT_ROADMAP.md)
  - [ ] **Persistence ownership 与 backup manifest** → outcome 见 [PILOT_ROADMAP §5.9.12](PILOT_ROADMAP.md)
  - [ ] **Account provisioning/readiness 生命周期与恢复** → outcome 见 [PILOT_ROADMAP §5.9.13](PILOT_ROADMAP.md)
  - [ ] **显式用户 schedule owner、misfire、幂等与恢复** → outcome 见 [PILOT_ROADMAP §5.9.14](PILOT_ROADMAP.md)
  - [ ] **Attachment/media ownership、MIME/size、retention 与 backup** → outcome 见 [PILOT_ROADMAP §5.9.15](PILOT_ROADMAP.md)
  - [ ] **RuntimeSnapshot lease、hooks、tenant secrets 与 revocation** → outcome 见 [PILOT_ROADMAP §5.9.16](PILOT_ROADMAP.md)
  - [ ] **Observability/privacy/redaction 与 retention 默认值** → outcome 见 [PILOT_ROADMAP §5.9.17](PILOT_ROADMAP.md)
- [ ] **P0 Pilot 基础运行基线** — 单机 FastAPI/Uvicorn、PostgreSQL + pgvector、有界进程内队列、HTTPS/WSS 入口；`planned`
  - [ ] **单机长期运行与重启恢复基线** → outcome 见 [PILOT_ROADMAP §6](PILOT_ROADMAP.md)
  - [ ] **当前 persistence map、backup manifest、健康检查与基础指标** → outcome 见 [PILOT_ROADMAP §3.4](PILOT_ROADMAP.md) 与 [§5.9.12](PILOT_ROADMAP.md)
  - [ ] **RuntimeSnapshot 全入口 lease coverage audit** → outcome 见 [PILOT_ROADMAP §5.9.16](PILOT_ROADMAP.md)
  - [ ] **结构化日志 redaction 与默认 content-off 基线** → outcome 见 [PILOT_ROADMAP §5.9.17](PILOT_ROADMAP.md)
  - [ ] **tenant-scoped admission + interactive/maintenance overload** → outcome 见 [PILOT_ROADMAP §5.9.5](PILOT_ROADMAP.md)
  - [ ] **工具 scope/effect 基线**（普通 tenant 关闭宿主机 shell/全局能力） → outcome 见 [PILOT_ROADMAP §5.8](PILOT_ROADMAP.md)
  - [ ] **记忆召回改造保持独立 change**（BM25/hotness/RRF，不阻塞安全 WebChat/Auth 闭环） → outcome 见 [PILOT_ROADMAP §5.9.10](PILOT_ROADMAP.md)
- [ ] **P0.5 WebChat 最小可用闭环** — 仅 local/dev identity，完成 channel、Gateway、协议和前端；`planned`
  - [ ] **canonical conversation/message stream 与 0-based per-conversation sequence** → outcome 见 [PILOT_ROADMAP §5.9.2](PILOT_ROADMAP.md)
  - [ ] **WebSocket hello/send/delta/completed/error/replay 协议** → outcome 见 [PILOT_ROADMAP §5.9.4](PILOT_ROADMAP.md)
  - [ ] **client_message_id 幂等、重连补拉和慢消费者测试** → outcome 见 [PILOT_ROADMAP §5.9.4](PILOT_ROADMAP.md)
  - [ ] **durable inbox/acceptance + final/outbox + delivery ack 状态机** → outcome 见 [PILOT_ROADMAP §5.9.11](PILOT_ROADMAP.md)
  - [ ] **模型生成完成与 channel `sent`/`failed` 语义分离** → outcome 见 [PILOT_ROADMAP §5.9.11](PILOT_ROADMAP.md)
  - [ ] **dev-only 暴露门禁**（P1 前不得公网 tenant-facing） → outcome 见 [PILOT_ROADMAP §6](PILOT_ROADMAP.md)
- [ ] **P1 一次 Token 登录** — 一次性邀请 Token 兑换可撤销 HttpOnly 登录 Cookie；`planned`
  - [ ] **test_accounts / access_tokens / auth_sessions 数据模型与 digest 约束** → outcome 见 [PILOT_ROADMAP §5.9.3](PILOT_ROADMAP.md)
  - [ ] **账号 provisioning → ready → active 后才签发 Token** → outcome 见 [PILOT_ROADMAP §5.9.13](PILOT_ROADMAP.md)
  - [ ] **普通/admin 分离认证、CSRF/Origin 和 Cookie timeout** → outcome 见 [PILOT_ROADMAP §5.9.3](PILOT_ROADMAP.md)
  - [ ] **HTTP、上传、媒体和 WebSocket 统一认证** → outcome 见 [PILOT_ROADMAP §5](PILOT_ROADMAP.md)
  - [ ] **immutable attachment_id + tenant ownership + MIME/size/cleanup** → outcome 见 [PILOT_ROADMAP §5.9.15](PILOT_ROADMAP.md)
  - [ ] **服务端 principal/tenant 派生与越权测试** → outcome 见 [PILOT_ROADMAP §5.9.1](PILOT_ROADMAP.md)
  - [ ] **PersonaProfile / RelationshipState PostgreSQL 当前值存储、tenant 串行更新与最小审计** → outcome 见 [PILOT_ROADMAP §5.9.8](PILOT_ROADMAP.md)
  - [ ] **Telegram Bot 用户私聊身份绑定 + cross-channel 去重/同步** → outcome 见 [PILOT_ROADMAP §5.9.2](PILOT_ROADMAP.md)
  - [ ] **PostgreSQL inbox/turn/tool/work/outbox/delivery/schedule/provisioning durable source of truth**（公网前移除 Pilot 多规范源） → outcome 见 [PILOT_ROADMAP §5.9.6](PILOT_ROADMAP.md)
- [ ] **P2 账号控制与长期试用** — Dashboard/CLI 发放、查询、过期、撤销、封禁和 tenant 下钻；`planned`
  - [ ] **账号 `suspended`/`revoked` 状态 + active WebSocket/tenant lane/tool 取消传播** → outcome 见 [PILOT_ROADMAP §5.3](PILOT_ROADMAP.md)
  - [ ] **按冻结默认容量实现有界队列、单账号限流、消息大小限制、overload/replay 和审计字段** → outcome 见 [PILOT_ROADMAP §5.9.5](PILOT_ROADMAP.md)
  - [ ] **ToolExecutionContext / TenantToolCatalog / ToolPolicy** → outcome 见 [PILOT_ROADMAP §5.8](PILOT_ROADMAP.md)
  - [ ] **工具资源与副作用隔离**（path resolver、target binding、owner、幂等、typed outcome） → outcome 见 [PILOT_ROADMAP §5.8.5](PILOT_ROADMAP.md)
  - [ ] **跨租户工具负向测试和并发交错测试** → outcome 见 [PILOT_ROADMAP §5.8.8](PILOT_ROADMAP.md)
  - [ ] **显式用户 schedule tenant ownership + server-resolved delivery binding** → outcome 见 [PILOT_ROADMAP §5.9.14](PILOT_ROADMAP.md)
  - [ ] **RuntimeSnapshot/per-task tenant context + hook failure/revocation gate** → outcome 见 [PILOT_ROADMAP §5.9.16](PILOT_ROADMAP.md)
  - [ ] **用户 MCP tenant namespace**（不随 Token 登录自动开放；独立 binding/runtime/catalog/secret/audit 与负向测试完成后再单独开放） → outcome 见 [PILOT_ROADMAP §5.8.4](PILOT_ROADMAP.md)
- [ ] **P3 稳定性与备份** — 恢复演练、崩溃重启、运行指标和维护 runbook；`planned`
  - [ ] **PostgreSQL / workspace / attachment 备份恢复演练** → outcome 见 [PILOT_ROADMAP §6](PILOT_ROADMAP.md)
  - [ ] **canonical migration cutover、30-day compatibility、pre/post-cutover rollback 与 forward-fix/PITR 演练** → outcome 见 [PILOT_ROADMAP §5.9.9](PILOT_ROADMAP.md)
  - [ ] **admin recovery token 丢失、疑似泄露和数据库恢复 runbook 演练** → outcome 见 [PILOT_ROADMAP §5.9.3](PILOT_ROADMAP.md)
  - [ ] **启动恢复扫描、unknown outcome query 与 compensation 演练** → outcome 见 [PILOT_ROADMAP §5.9.6](PILOT_ROADMAP.md)
  - [ ] **outbox delivery retry/dead-letter/重投与 provider ack 恢复演练** → outcome 见 [PILOT_ROADMAP §5.9.11](PILOT_ROADMAP.md)
  - [ ] **provisioning pending/failed 启动恢复与 admin retry 演练** → outcome 见 [PILOT_ROADMAP §5.9.13](PILOT_ROADMAP.md)
  - [ ] **schedule misfire/restart/idempotency/DST 演练** → outcome 见 [PILOT_ROADMAP §5.9.14](PILOT_ROADMAP.md)
  - [ ] **attachment blob backup、orphan cleanup 与 missing reconciliation** → outcome 见 [PILOT_ROADMAP §5.9.15](PILOT_ROADMAP.md)
  - [ ] **认证失败、在线连接、队列 backlog、LLM/tool/retrieval/delivery 指标** → outcome 见 [PILOT_ROADMAP §7](PILOT_ROADMAP.md)
  - [ ] **日志/审计 retention、redaction 与 admin content-access audit** → outcome 见 [PILOT_ROADMAP §5.9.17](PILOT_ROADMAP.md)
  - [ ] **Persona/Relationship 当前值的备份/PITR 恢复、tenant 单写者与隐私删除演练** → outcome 见 [PILOT_ROADMAP §5.9.8](PILOT_ROADMAP.md)
- [ ] **P4 升级闸门** — 仅在真实负载超过单机边界时引入 Redis Streams 和多副本；`deferred`
  - [ ] **以 backlog、并发连接、重复副作用和恢复窗口证据触发扩展评估** → outcome 见 [PILOT_ROADMAP §6](PILOT_ROADMAP.md)

> **扩展方式**：Pilot 阶段进一步细化时，在本文件对应阶段新增父条目或子条目，并链接到新建的
> OpenSpec change / spec / record / evidence；路线图只承载总体目标与决策，不追加 implementation task。
