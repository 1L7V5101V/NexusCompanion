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

- **当前阶段**（2026-10-02 刷新）：实现推进期。verified 合入：C1 canonical identity（+ account→N tenant 扩展）、C2 durable control plane、C3 admission/queue、C4 WebChat dev 协议闭环、C5 auth/provisioning、C8 RuntimeSnapshot/secrets、C13 记忆召回、C15 work queue 消费层、invite-code-tenant-registration（均归档）；C12 观测/隐私/备份 P-1 契约层 verified（贯穿型，change 保持 active，§8 伴随落地进行中）；webchat-auth-wiring 21/21 全部验收完成并归档（2026-09-27）；C7 工具隔离 `verified` 并归档（22/22，2026-09-28，spec `tenant-tool-isolation` 已 sync）。存储切换 `verified` 并归档（change `pg-durable-sot-cutover`，21/21 任务含生产激活，2026-10-02 归档）。**C6 attachment 已实现、归档并合入 main**（change `c6-attachment`，16/16 任务，全量回归 1835 passed/0 failed，2026-10-02 archive/sync：spec `attachment-media` 新建 9 requirements，merge `3b698fde`（历史改写后的哈希））——§5.9.15 门禁行与 P1/P3 相关行已回填。**C6 质量复盘发现两处由容器启动触发的静默数据销毁，已由 change `c6-attachment-hardening` 修复并归档**（spec `attachment-media` 增至 10 requirements：新增「reconciliation 作用域限定与在途写入保护」；全量回归 1849 passed/0 failed；证据 [evidence/c6-attachment-hardening](evidence/c6-attachment-hardening/)）：blob root 改租户命名空间 `workspace/tenants/<dirname>/attachments`、对账 known 集合覆盖全部状态（此前把上传终态 staged 判为孤儿删除）、孤儿宽限期、清理顺序与 delete 记录点、Pillow 异常面归一、media 流式。误提交的 `secrets/auth_pepper` 先取消跟踪（`45ad61a4`），随后用 `git filter-repo` 从本地历史整体抹除该路径（生产 pepper 为另一值，未受影响）。**谱系事实**：本地 main 与远端 main 自 2026-07-13 的一次 filter-repo 起为平行谱系（本地独有 425 commit / 远端独有 412），远端内容经核对是本地的严格子集；本次以 `--force-with-lease` 覆盖公开 main，因此 openspec 文档与 records 里 2026-07-13 之前写下的 commit 哈希引用全部失效，需按 `.git/filter-repo/commit-map` 重映射。生产未启用附件，无存量 blob 需搬迁。 尚未开工：C9 Persona、C10 Telegram binding、C11 schedule、C14 memory catalog。
- **current focus**：C12 §8 伴随落地（8.2 内容审计接线、8.4 retention 接线与总控台聚合、8.5 基线报告、8.6 P3 演练——8.1 记录点义务已全部落地含 C6、8.3 已随 C6 完成）；C9/C10/C11/C14 按依赖图排期（对应 §5.9 设计门禁收口先行）。
- **current blocker**：面向受邀用户的公网开放，**存储切换已实现**（2026-09-29，`pg-durable-sot-cutover` 18/18 任务 + 全量回归全绿，change 待归档）：WebChat 消息路径已满足 §5.9.6 durable source of truth；公网开放剩余前置 = 服务器部署 + 部署后 canary 实跑（模式同 webchat-auth-wiring）+ 运维放开决策（Tunnel/监控/runbook），均非代码阻塞。webchat-auth-wiring 运维边界已解除（2026-09-28，C7）：拿到 session 的主体不再拥有全实例工具能力——工具调用经 per-tenant `ToolExecutionContext`（账户/租户/会话/turn 服务端派生、模型不可覆盖）+ effect 闸门（process-exec/admin 对普通租户一律拒绝、external-write 无补偿拒）+ 白名单内目录 + 租户路径解析；C6/C9/C10/C11/C14 对应 5.9 设计门禁未收口前不开始实现（P-1 规则）。
- **next decision**：①C12 §8.2（内容查看端点 + AdminAccessAuditEvent 接线）/ §8.4（retention 接线 + 总控台聚合 API）指定承载 change；②下一 capability 排期：C9 Persona / C11 schedule（可先行，无重依赖）/ C10 Telegram binding / C14 memory catalog 按依赖图。
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

> change：[`phase1b-migration-cutover`](changes/archive/2026-08-26-phase1b-migration-cutover/)（已 archive `2026-08-26-phase1b-migration-cutover`；spec `storage-migration` 已 sync 至 [spec](specs/storage-migration/spec.md)）。该阶段证明迁移工具能力，不代表 Pilot 会导入现有单体 SQLite；当前 Pilot 决策是旧数据原库保留。

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
  - [ ] **Canonical identity 与 Telegram Bot 私聊绑定**（`account → tenant → canonical conversation`、Telegram 用户与 Bot 私聊身份的一对一绑定、Pilot 从空历史开始；旧单体 SQLite 原库保留且不 fallback/双写） → outcome 见 [PILOT_ROADMAP §5.9.2](PILOT_ROADMAP.md)；canonical identity（C1）`verified`（commit `e124dbf8`，2026-09-05，change 归档 `changes/archive/2026-09-05-c1-canonical-identity/`，证据 [evidence/c1-canonical-identity](evidence/c1-canonical-identity/)），Telegram binding 仍 `planned`（C10）
  - [ ] **Auth/admin/browser security**（分离 Cookie、CSRF/Origin、timeout、401/403、原子兑换、`pilot-admin` bootstrap/rotate/revoke/disable/enable；recovery token 轮换默认保留有效 browser sessions，泄露时再显式 revoke） → outcome 见 [PILOT_ROADMAP §5.9.3](PILOT_ROADMAP.md)
  - [ ] **WebSocket protocol contract**（hello、client_message_id、sequence、durable terminal、slow consumer） → outcome 见 [PILOT_ROADMAP §5.9.4](PILOT_ROADMAP.md)
  - [ ] **Admission 与 overload policy**（tenant lane；interactive 128 / per-tenant 16 / maintenance 64 / WS 256-soft 192；LLM/embedding/MCP/process 30/4/8/2；LLM 429 退避与指标） → outcome 见 [PILOT_ROADMAP §5.9.5](PILOT_ROADMAP.md)；tenant-scoped admission + 有界队列 + 启动恢复扫描（C3 P0 段）`verified`（merge `e2408e5`，2026-09-20，change `c3-admission-queue-recovery` 全 15 项完成、证据 [evidence/c3-admission-queue-recovery](evidence/c3-admission-queue-recovery/)）；**LLM 429 退避与指标**、以及 C3 P3 恢复演练仍 `planned`（指标记录点依赖 C12 §8.1，当前无 owner change）
  - [ ] **PostgreSQL durable control plane 与 restart recovery**（turn/tool/work/outbound final、unknown/compensation） → outcome 见 [PILOT_ROADMAP §5.9.6](PILOT_ROADMAP.md)；durable control plane 表/三事务边界/delivery 状态机（C2）`verified`（commit `c2d40770`，2026-09-06，change 归档 `changes/archive/2026-09-06-c2-durable-control-plane/`，证据 [evidence/c2-durable-control-plane](evidence/c2-durable-control-plane/)）；restart recovery 扫描/compensation 调度归 C3/P3 仍 `planned`
  - [x] **ToolExecutionContext 三层注入边界与普通 tenant 首版工具白名单（精确 tool id）** → outcome 见 [PILOT_ROADMAP §5.9.7](PILOT_ROADMAP.md)；C7：frozen `ToolExecutionContext`（account/tenant/session/turn 服务端派生，fail-closed 空 tenant 拒绝）+ 语义反转（`TRUST_ARGUMENT_FIELDS` 剥离、`context.tool_kwargs()` 最高优先级、`set_context` 删除）+ 精确白名单/关闭清单（`agent/tools/catalog.py`，task 1.1 程序化清单对账收敛）+ schema 过滤（2026-09-28，evidence [evidence/c7-tool-isolation](evidence/c7-tool-isolation/)）
  - [ ] **Persona/Relationship 当前值语义**（Persona onboarding 后固定、RelationshipState 沿用单体原地更新、tenant 单写者、无产品级 revision/CAS、PITR 恢复与 debug 权限） → outcome 见 [PILOT_ROADMAP §5.9.8](PILOT_ROADMAP.md)
  - [ ] **数据模型、唯一约束和 DB rollout/rollback**（首次 Create → Verify → Enable，不导入 SQLite；后续 PostgreSQL schema evolution 才按 expand/backfill/cutover） → outcome 见 [PILOT_ROADMAP §5.9.9](PILOT_ROADMAP.md)
  - [ ] **独立 capability/change 依赖图**（identity/control-plane、WebChat、auth/provisioning、attachment、tool/snapshot、Persona、Telegram、schedule、observability、retrieval） → outcome 见 [PILOT_ROADMAP §5.9.10](PILOT_ROADMAP.md)
  - [ ] **Ingress acceptance、canonical message、outbox 与 delivery transaction** → outcome 见 [PILOT_ROADMAP §5.9.11](PILOT_ROADMAP.md)
  - [ ] **Persistence ownership 与 backup manifest** → outcome 见 [PILOT_ROADMAP §5.9.12](PILOT_ROADMAP.md)
  - [ ] **Account provisioning/readiness 生命周期与恢复** → outcome 见 [PILOT_ROADMAP §5.9.13](PILOT_ROADMAP.md)
  - [ ] **显式用户 schedule owner、misfire、幂等与恢复** → outcome 见 [PILOT_ROADMAP §5.9.14](PILOT_ROADMAP.md)
  - [x] **Attachment/media ownership、MIME/size、retention 与 backup** → outcome 见 [PILOT_ROADMAP §5.9.15](PILOT_ROADMAP.md)；C6 实现层 `verified`（change `c6-attachment`，feature/c6-attachment，2026-10-02，证据 [evidence/c6-attachment](evidence/c6-attachment/)）：immutable attachment_id + tenant namespace blob（`workspace/tenants/<dirname>/attachments`，与 C7 attachments_root 同树；加固 change `c6-attachment-hardening` 收口）+ PG metadata；MIME/扩展名 have 双一致 allowlist（图片 image/jpeg/png/webp/gif + 文本 UTF-8）+ 资源硬上限（20 MiB/16.8M 像素/64 MiB 解码内存/5s 超时/100 帧/200k 字符）fail-closed；保留 30d/临时 24h 幂等清理 + orphan/missing reconciliation（dry-run 演练形态）；backup manifest `tenant-workspace` 条目细化（C12 §8.3）；C12 §8.1 attachment 事件记录点。**加固（change `c6-attachment-hardening`，2026-10-02 归档）**：复盘修正 blob 根与对账 known 集合两处数据销毁，孤儿判定加在途宽限，新增可验收条款「reconciliation 作用域限定与在途写入保护」，证据 [evidence/c6-attachment-hardening](evidence/c6-attachment-hardening/)。注：备份恢复演练与 P3 每日粒度 reconciliation 仍 `planned`（归 P3）
  - [ ] **RuntimeSnapshot lease、hooks、tenant secrets 与 revocation** → outcome 见 [PILOT_ROADMAP §5.9.16](PILOT_ROADMAP.md)
  - [ ] **Observability/privacy/redaction 与 retention 默认值** → outcome 见 [PILOT_ROADMAP §5.9.17](PILOT_ROADMAP.md)；P-1 契约层（content-off gate + redaction、metrics label 白名单、retention 三档 30/180/7、backup manifest 模板+校验器、§7.1 事件 schema fixture、SLO DEFERRED 负向测试）`verified`（change `c12-observability-backup`，merge tip `83d54bc8`，2026-09-06，证据 [evidence/c12-observability-backup](evidence/c12-observability-backup/)）；§8.1（E10 指标记录点）已由 C15 首个落地（`background_work_items`，PR #7，2026-09-27）；总控台聚合（C2/C3 id 落地后）、config 接线与基线报告、恢复演练仍 `in_progress`（伴随落地协议，change 保持 active 不归档）
    - **2026-09-20 清点（伴随落地协议未执行）**：C2/C3/C4 的 change checklist 均未按 task-12「伴随落地协议」带「§7.1 指标字段 + redaction + backup manifest 条目」（三者 `tasks.md` 对 `§7.1|指标|redaction|manifest|AdminAccess` 均 0 命中），导致 C12 §8.1（E10 指标记录点）成为**无 owner 的滞留项**；§8.2–8.6 逐条 owner 已登记在 change `c12-observability-backup/tasks.md` §8。后续每个 Cxx change 必须在其 checklist 内带该条目，否则同一遗漏会复现。
- [ ] **P0 Pilot 基础运行基线** — 单机 FastAPI/Uvicorn、PostgreSQL + pgvector、有界进程内队列、HTTPS/WSS 入口；`planned`
  - [ ] **单机长期运行与重启恢复基线** → outcome 见 [PILOT_ROADMAP §6](PILOT_ROADMAP.md)
  - [ ] **当前 persistence map、backup manifest、健康检查与基础指标** → outcome 见 [PILOT_ROADMAP §3.4](PILOT_ROADMAP.md) 与 [§5.9.12](PILOT_ROADMAP.md)；backup manifest 模板+校验器契约已交付（C12：`tests/fixtures/backup_manifest_template.json` + `core/backup/manifest.py`），待接线运行与基线产出
  - [x] **RuntimeSnapshot 全入口 lease coverage audit** → outcome 见 [PILOT_ROADMAP §5.9.16](PILOT_ROADMAP.md)；C8 P0 段（全入口 work-start lease + revocation recheck fail-closed + 热更新不切 snapshot）`verified`（change `c8-runtimesnapshot-secrets`，merge commit `4a8a1f6f`（PR #6），2026-09-27，证据 [evidence/c8-runtimesnapshot-secrets](evidence/c8-runtimesnapshot-secrets/)；revocation 真实账号源接线归 C5）
  - [ ] **结构化日志 redaction 与默认 content-off 基线** → outcome 见 [PILOT_ROADMAP §5.9.17](PILOT_ROADMAP.md)；redaction/ContentCaptureGate/metrics label 白名单/retention sweep 契约原语已 verified（C12），config 接线与进程内定时执行归 C12 §8.4
  - [ ] **tenant-scoped admission + interactive/maintenance overload** → outcome 见 [PILOT_ROADMAP §5.9.5](PILOT_ROADMAP.md)
  - [x] **工具 scope/effect 基线**（普通 tenant 关闭宿主机 shell/全局能力） → outcome 见 [PILOT_ROADMAP §5.8](PILOT_ROADMAP.md)；C7 effect 闸门（2026-09-28）：process-exec/admin 对普通租户一律拒绝、external-write 无补偿登记即拒、错误码 `tool_denied_effect`；shell/spawn/spawn_manage/task_stop/load_skill/mcp_add 等关闭面 + 每工具 effect 映射表（[effects-mapping](evidence/c7-tool-isolation/effect-mapping.md)）
  - [x] **记忆召回改造保持独立 change**（BM25/hotness/RRF，不阻塞安全 WebChat/Auth 闭环） → outcome 见 [PILOT_ROADMAP §5.9.10](PILOT_ROADMAP.md)；`verified`（change `2026-09-07-c13-memory-retrieval-bm25`，PR #5 merge `5b140fe`，证据 [evidence/c13-memory-retrieval-bm25](evidence/c13-memory-retrieval-bm25/)：A 基线可复现评测 + 消融矩阵 + 回归/pyright 对齐基线）
- [ ] **P0.5 WebChat 最小可用闭环** — dev-only 闭环已按 task-04 验收标准收口（change `2026-09-20-c4-webchat-protocol-dev-loop`，证据 [evidence/c4-webchat-protocol-dev-loop](evidence/c4-webchat-protocol-dev-loop/)）；dev 闭环 `verified`，公网项仍 `planned`（C5 认证后端已于 `0753628` 合并；WebChat **通道层身份派生与握手凭据门禁已由 change `2026-09-21-webchat-auth-wiring` 接通（21/21 全部验收完成）**：真实 PG e2e（本地，`real-pg-e2e.txt`）与部署后服务器实跑（生产容器内 canary 全链，`deploy-e2e.txt`）均 E2E PASS，2026-09-27，evidence [evidence/webchat-auth-wiring](evidence/webchat-auth-wiring/)；change 已归档 `changes/archive/2026-09-21-webchat-auth-wiring/`（验收闭环 commit `57a0c80e`），spec `auth-provisioning`/`webchat-protocol-dev-loop` 已 sync）
  - [x] **dev WebSocket hello/send/delta/tool/turn 终态/error/replay 帧协议**（`infra/channels/web_chat_protocol.py` + 共享 fixture `tests/fixtures/chat_protocol_frames.json`；精确字段/版本/错误码/close code 已由 C4 change 冻结并前后端双向执行：后端 49 项 + 前端 31 项）
  - [x] **client_message_id 幂等、重连补拉和慢消费者测试**（`tests/test_web_chat_channel.py`；C4 补真实入口 e2e：收发/流式/重连无重复/`replay_required`→REST 重建/断线不取消 turn/空闲回收，共 6 项；进程内重放 buffer 为 dev v0，PG durable sequence 未实现）
  - [x] **canonical conversation/message stream 与 0-based per-conversation sequence** → outcome 见 [PILOT_ROADMAP §5.9.2](PILOT_ROADMAP.md)；`verified`（change `pg-durable-sot-cutover`，2026-09-29）：WebChat 全链路接入 canonical 流——T1 接受事务（dedupe+canonical user message+inbox+queued turn+重放帧同事务，提交后才 ack）、T2 完成事务（final assistant message+turn 终态+outbox intent 同事务）、durable 重放帧表（重启存续 seq/补拉）、REST 重建以 canonical 为权威（tenant 由 session 派生）；evidence [evidence/pg-durable-sot-cutover](evidence/pg-durable-sot-cutover/task-7.1-pg-e2e.txt)
  - [ ] **durable inbox/acceptance + final/outbox + delivery ack 状态机** → outcome 见 [PILOT_ROADMAP §5.9.11](PILOT_ROADMAP.md)；durable control plane 表/三事务边界/delivery 状态机（C2）`verified`（commit `c2d40770`，2026-09-06，change 归档 `changes/archive/2026-09-06-c2-durable-control-plane/`，证据 [evidence/c2-durable-control-plane](evidence/c2-durable-control-plane/)）
  - [ ] **模型生成完成与 channel `sent`/`failed` 语义分离** → outcome 见 [PILOT_ROADMAP §5.9.11](PILOT_ROADMAP.md)（`turn.failed` 帧已可用；delivery ack 状态机已由 C2 落地：`sent` 仅由 ack 推进、独立 attempt/provider receipt/dead_letter/redrive）；durable control plane 表/三事务边界/delivery 状态机（C2）`verified`（commit `c2d40770`，2026-09-06，change 归档 `changes/archive/2026-09-06-c2-durable-control-plane/`，证据 [evidence/c2-durable-control-plane](evidence/c2-durable-control-plane/)）
  - [x] **dev-only 暴露门禁**（C4 三层：`[channels.chat] enabled=false` 默认关闭 ∧ 非 dev 启用即 fail-fast / 非回环 host 需显式 `allow_public_bind` ∧ 运行期回环中间件 HTTP 403 / WS 1008；P1 前不得公网 tenant-facing）
- [ ] **P1 一次 Token 登录** — 一次性邀请 Token 兑换可撤销 HttpOnly 登录 Cookie；后端 `verified`（C5 合并 `0753628`，PR #2），用户可见登录 `planned`（WebChat 通道接线缺失，见下）
  - [x] **test_accounts / access_tokens / auth_sessions 数据模型与 digest 约束** → outcome 见 [PILOT_ROADMAP §5.9.3](PILOT_ROADMAP.md)；C5 落地（migration `b7e2f9a4c1d8`：`access_tokens`/`auth_sessions`/`admin_credentials`/`admin_audit_events`/`tenant_provisioning_jobs`，CHECK 约束强制 64-hex digest），证据 [evidence/c5-auth-provisioning-admin](evidence/c5-auth-provisioning-admin/)
  - [x] **账号 provisioning → ready → active 后才签发 Token** → outcome 见 [PILOT_ROADMAP §5.9.13](PILOT_ROADMAP.md)；C5 落地（provisioning 状态机 + readiness gate，ready 前不发 Token），证据同上
  - [x] **普通/admin 分离认证、CSRF/Origin 和 Cookie timeout** → outcome 见 [PILOT_ROADMAP §5.9.3](PILOT_ROADMAP.md)；C5 落地（`__Host-nexus_session` vs `__Host-nexus_admin`、session-bound CSRF、Origin/Referer 校验、idle 7d/absolute 30d 与 admin 30min/12h），证据同上
  - [ ] **HTTP、上传、媒体和 WebSocket 统一认证** → outcome 见 [PILOT_ROADMAP §5](PILOT_ROADMAP.md)；**已完成 HTTP + WebSocket 两面**：`/api/auth/*` + `/api/admin/*`（C5），`/ws` 握手凭据校验 + **用户面 REST 凭据门禁**（`bootstrap/chat_api.py` 的 `_require_user_session`，覆盖 `/api/chat/sessions*`、`/uploads`、`/media`），均由 change `2026-09-21-webchat-auth-wiring` 收口；**剩余「上传/媒体」的租户归属与生命周期归 C6**
  - [x] **immutable attachment_id + tenant ownership + MIME/size/cleanup** → outcome 见 [PILOT_ROADMAP §5.9.15](PILOT_ROADMAP.md)；C6 `verified`（change `c6-attachment`，2026-10-02）：uploads/media 端点 BREAKING 契约（attachment_id 寻址、ownership 404 语义、旧 path 参数 400、dev/SQLite 503 fail-closed）；内容校验 12 项负向矩阵 + 资源硬上限；清理（24h staging / 30d 引用）与 orphan/missing reconciliation 接线；**加固后** blob 真正落在租户命名空间且对账不再删除在库附件、畸形图片返回 415 而非 500、`max_decode_bytes` 生效、media 流式（change `c6-attachment-hardening`）
  - [x] **服务端 principal/tenant 派生与越权测试** → outcome 见 [PILOT_ROADMAP §5.9.1](PILOT_ROADMAP.md)；C1 规范身份链已 `verified`；**WebChat 入口的 principal→tenant 派生已接线并测试**（change `2026-09-21-webchat-auth-wiring`：`resolve_webchat_identity` 经 C1 解析 `account_id → tenant_id → canonical conversation_id`，fail-closed 不回落 `DEFAULT_TENANT`；`tests/auth_provisioning/test_webchat_identity.py` 覆盖跨账号隔离与 fail-closed），证据 [evidence/webchat-auth-wiring](evidence/webchat-auth-wiring/)
  - [ ] **PersonaProfile / RelationshipState PostgreSQL 当前值存储、tenant 串行更新与最小审计** → outcome 见 [PILOT_ROADMAP §5.9.8](PILOT_ROADMAP.md)
  - [ ] **Telegram Bot 用户私聊身份绑定 + cross-channel 去重/同步** → outcome 见 [PILOT_ROADMAP §5.9.2](PILOT_ROADMAP.md)
  - [x] **邀请码租户注册 + 邮箱密码登录**（invite-code-tenant-registration change）— 一次性租户邀请码（签发不要求预存账号，携带租户名）+ 邮箱/密码自助注册 + 登录；argon2 密码哈希；`POST /api/auth/register` / `POST /api/auth/login` / admin `POST /api/admin/tenant-invites`；注册即消费邀请码 + provisioning 收束 + 会话；存量 exchange 路径兼容。migration `c9d7e3a5f2b1`（test_accounts.email/password_digest、access_tokens.account_id nullable + tenant_name、账号状态枚举 + failed）。测试：82/82（tests/auth_provisioning）全绿 + 前端 `npm run build:chat`。change 未归档时以 `openspec/changes/invite-code-tenant-registration/` 为准
  - [ ] **PostgreSQL inbox/turn/tool/work/outbox/delivery/schedule/provisioning durable source of truth**（公网前移除 Pilot 多规范源） → outcome 见 [PILOT_ROADMAP §5.9.6](PILOT_ROADMAP.md)；**WebChat 链路已落地**（change `pg-durable-sot-cutover`，2026-09-29，18/18 任务，全量回归 1768 passed/0 failed，evidence [evidence/pg-durable-sot-cutover](evidence/pg-durable-sot-cutover/)）：inbox/turn/tool_call/outbox/delivery 接入 C2 控制面三事务边界 + durable 重放帧 + delivery worker（C15 work item 侧早已 durable）；**schedule durable 归 C11 仍 planned**（不阻塞首批受邀用户 WebChat 闭环），本条目待 C11 落地后整体勾选
- [ ] **P2 账号控制与长期试用** — Dashboard/CLI 发放、查询、过期、撤销、封禁和 tenant 下钻；`planned`
  - [x] **账号 `suspended`/`revoked` 状态 + active WebSocket/tenant lane/tool 取消传播** → outcome 见 [PILOT_ROADMAP §5.3](PILOT_ROADMAP.md)；C7 工具侧（2026-09-28）：封禁事件 → 租户级取消注册表取消执行中工具调用（结构化取消结果 `tool_cancelled_account_status`，其他租户不受影响，evidence [task-6.1](evidence/c7-tool-isolation/task-6.1-tool-cancellation.txt)）+ pre-tool revocation recheck 拒新（`TenantToolGateHook`，C8 接缝 fail-closed）；WS 断开传播沿用既有账号断开路径（webchat-auth-wiring/C5 侧）
  - [ ] **按冻结默认容量实现有界队列、单账号限流、消息大小限制、overload/replay 和审计字段** → outcome 见 [PILOT_ROADMAP §5.9.5](PILOT_ROADMAP.md)
  - [x] **ToolExecutionContext / TenantToolCatalog / ToolPolicy** → outcome 见 [PILOT_ROADMAP §5.8](PILOT_ROADMAP.md)；C7（2026-09-28）：frozen context + 租户目录（pre-hook 重查 `tool_denied_account_status`/`tool_denied_tenant_scope` 错误码冻结）+ effect 执行面（`tool_denied_effect`）+ 审计（`tool_audit_events`）；binding/capability 重查归 C14
  - [x] **工具资源与副作用隔离**（path resolver、target binding、owner、幂等、typed outcome） → outcome 见 [PILOT_ROADMAP §5.8.5](PILOT_ROADMAP.md)；C7（2026-09-28）：`TenantPathResolver` 逃逸面矩阵（绝对/`..`/symlink）+ 五文件工具统一接线；`message_push` 服务端绑定目标（`push_target_not_allowed`）；后台任务 owner（scheduler/task_output/task_stop 租户隔离）；typed outcome（`ToolEffect` 七级 + `ToolResult.tool_call_id`，无补偿 external-write 默认拒 = 幂等/补偿登记面）
  - [x] **跨租户工具负向测试和并发交错测试** → outcome 见 [PILOT_ROADMAP §5.8.8](PILOT_ROADMAP.md)；C7（2026-09-28）：共享 registry 双租户 24 轮并发交错零泄漏 + 持久化目录互不可达（真实 C5 provisioning 双账号，`tests/c7/test_tool_isolation_pg.py`，3/3 重跑稳定，evidence [task-8.2](evidence/c7-tool-isolation/task-8.2-interleave-pg.txt)）；§5.8.8 十二条闸门 12/12 对账有落点（[task-8.1 对照表](evidence/c7-tool-isolation/task-8.1-gate-reconciliation.md)）
  - [ ] **显式用户 schedule tenant ownership + server-resolved delivery binding** → outcome 见 [PILOT_ROADMAP §5.9.14](PILOT_ROADMAP.md)
  - [x] **RuntimeSnapshot/per-task tenant context + hook failure/revocation gate** → outcome 见 [PILOT_ROADMAP §5.9.16](PILOT_ROADMAP.md)；C8 P2 段（TenantRuntimePlan/Resolver/PluginInvocationContext 接缝 + contribution 元数据 + hook failure 分层 + secret 静态加密/rotation + 发布失败回退 + dormant 安装）`verified`（change `c8-runtimesnapshot-secrets`，merge commit `4a8a1f6f`（PR #6），2026-09-27，证据 [evidence/c8-runtimesnapshot-secrets](evidence/c8-runtimesnapshot-secrets/)；TenantToolCatalog 消费归 C7、tenant plugin catalog/engine slot 归 C14、tenant binding durable 化归 C14/C5）
  - [ ] **用户 MCP tenant namespace**（不随 Token 登录自动开放；独立 binding/runtime/catalog/secret/audit 与负向测试完成后再单独开放） → outcome 见 [PILOT_ROADMAP §5.8.4](PILOT_ROADMAP.md)
- [ ] **P3 稳定性与备份** — 恢复演练、崩溃重启、运行指标和维护 runbook；`planned`
  - [ ] **PostgreSQL / workspace / attachment 备份恢复演练，并独立保存 legacy SQLite/workspace** → outcome 见 [PILOT_ROADMAP §6](PILOT_ROADMAP.md)
  - [ ] **Pilot 空白 PostgreSQL 首次启用/关闭入口回滚，以及后续 PG schema evolution、forward-fix/PITR 演练；不演练 SQLite 历史导入** → outcome 见 [PILOT_ROADMAP §5.9.9](PILOT_ROADMAP.md)
  - [ ] **admin recovery token 丢失、疑似泄露和数据库恢复 runbook 演练** → outcome 见 [PILOT_ROADMAP §5.9.3](PILOT_ROADMAP.md)
  - [ ] **启动恢复扫描、unknown outcome query 与 compensation 演练** → outcome 见 [PILOT_ROADMAP §5.9.6](PILOT_ROADMAP.md)
  - [ ] **outbox delivery retry/dead-letter/重投与 provider ack 恢复演练** → outcome 见 [PILOT_ROADMAP §5.9.11](PILOT_ROADMAP.md)
  - [ ] **provisioning pending/failed 启动恢复与 admin retry 演练** → outcome 见 [PILOT_ROADMAP §5.9.13](PILOT_ROADMAP.md)
  - [ ] **schedule misfire/restart/idempotency/DST 演练** → outcome 见 [PILOT_ROADMAP §5.9.14](PILOT_ROADMAP.md)
  - [ ] **attachment blob backup、orphan cleanup 与 missing reconciliation** → outcome 见 [PILOT_ROADMAP §5.9.15](PILOT_ROADMAP.md)；orphan cleanup 与 missing reconciliation 已实现（C6，`AttachmentLifecycleRuntime`：启动对账 + 周期 + 手动 dry-run，evidence [task-5](evidence/c6-attachment/task-5-lifecycle-runtime.md)）；**加固后**对账按租户 blob 根作用域执行、周期轮覆盖全部有附件记录的租户（此前生产默认 `tenant_ids=()` 使周期轮空转），dry-run 零变更有契约用例（evidence [task-2](evidence/c6-attachment-hardening/task-2-data-loss-repro.md)）；**backup restore 演练与恢复 drill 归 P3 未完**（C12 §8.6 承载）
  - [ ] **认证失败、在线连接、队列 backlog、LLM/tool/retrieval/delivery 指标** → outcome 见 [PILOT_ROADMAP §7](PILOT_ROADMAP.md)
  - [ ] **日志/审计 retention、redaction 与 admin content-access audit** → outcome 见 [PILOT_ROADMAP §5.9.17](PILOT_ROADMAP.md)
  - [ ] **Persona/Relationship 当前值的备份/PITR 恢复、tenant 单写者与隐私删除演练** → outcome 见 [PILOT_ROADMAP §5.9.8](PILOT_ROADMAP.md)
- [ ] **P4 升级闸门** — 仅在真实负载超过单机边界时引入 Redis Streams 和多副本；`deferred`
  - [ ] **以 backlog、并发连接、重复副作用和恢复窗口证据触发扩展评估** → outcome 见 [PILOT_ROADMAP §6](PILOT_ROADMAP.md)

> **扩展方式**：Pilot 阶段进一步细化时，在本文件对应阶段新增父条目或子条目，并链接到新建的
> OpenSpec change / spec / record / evidence；路线图只承载总体目标与决策，不追加 implementation task。
