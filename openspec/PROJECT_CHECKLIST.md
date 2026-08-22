# PROJECT_CHECKLIST — 完整项目进度观测

> **定位**：本文件是完整项目的**进度观测 checklist**，只含 checklist + 一行简短描述 + 状态。
> 详细描述一律以链接指向既有细节文件（`specs/`、`changes/`、`records/`、`evidence/`、代码）。
> **不承载**完整 evidence、迁移命令、逐 commit 历史或行为规范定义。
>
> **Source of truth 优先级**：已验证代码与测试证据 > `openspec/specs` > `openspec/changes`（含 archive）> 历史材料；
> Program 目标与总体决策以 [`SCALING_ROADMAP.md`](./SCALING_ROADMAP.md) 为准；
> 状态定义（§7）与完成状态规则（§8）见 [`SCALING_ROADMAP.md`](./SCALING_ROADMAP.md)，本文件只引用不复制。
>
> 状态标记复用 roadmap §7 六态（planned/proposed/in_progress/blocked/verified/retired）；
> 勾选框仅在 `verified`（满足 §8：merge commit + 可复现证据）时勾选。checklist 是派生观测面，
> 状态更新只允许在有证据时进行，与 change 的 tasks / `openspec status` 保持同步。

## 当前进度（来自 SCALING_ROADMAP §5）

- **当前阶段**：GOV 治理与文档基线 verified（merge `e50732d6`）；C1 Storage Foundation verified（merge `0a83314d`）。
- **current focus**：Phase 1B（C1B 迁移工具 + C1C 主数据源切换）在独立 branch/worktree 执行。
- **current blocker**：无 hard blocker。
- **next decision**：Phase 1B（M5/M6）启动范围与迁移状态机细化；是否创建 C0 的 OpenSpec change。
- 详见 [`SCALING_ROADMAP.md` §5](./SCALING_ROADMAP.md)。

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

## Phase 1B 进行中（M5/M6，独立 branch/worktree）

- [ ] **C1B 迁移工具（M5）** — 批量 COPY、断点续传、机器可读校验；`planned`
  - [ ] **批量 COPY / 批量 insert**（可配置 batch、进度） → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **断点续传与幂等重跑** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **机器可读校验（逐表行数/字段 hash/语义抽样）+ 迁移证据入库** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
- [ ] **C1C 主数据源切换（M6）** — S0-S4 状态机切 PostgreSQL primary；`planned`
  - [ ] **S0-S4 状态机**（SQLite primary → shadow import → PG shadow write/read verify → PG primary → SQLite retirement） → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **staging cutover + 对账** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **PITR 恢复与回滚演练** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)

## Phase 1C（M7 生产规模验证）

- [ ] **C1D 生产规模验证（M7）** — 真实 1024 维 embedding、5000 并发用户、autovacuum/REINDEX、PITR；`planned`
  - [ ] **真实 1024 维 embedding + 真实 tenant 分布 recall/延迟达标** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **5000 并发用户/tenant 运维基准**（planning latency、catalog、backup/restore） → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **autovacuum/REINDEX + EXPLAIN 分区裁剪验证** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **PITR / 连接池并发 / event-loop lag / 故障恢复** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)

## Phase 2 后续能力

- [ ] **C0 可观测性与负载工具** — turn_id 全链路追踪、指标导出、可重复负载工具；`proposed`
  - [ ] **turn_id 全链路追踪**（ingress→queue→worker→LLM→store→delivery） → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **指标导出与 dashboard** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **可重复负载工具**（负载脚本与数据入库） → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
- [ ] **C2 单进程并发与容量治理** — TurnAdmission + ModelGateway：同 session 串行、有界 inflight、限流预算；`planned`
  - [ ] **TurnAdmission**（同 session 串行、有界 inflight） → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **ModelGateway 限流预算**（per provider/model RPM/TPM） → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
- [ ] **C3 持久消息与水平 Worker** — ingress queue + inbox 去重 + transactional outbox + delivery DLQ + 多副本 lease；`planned`
  - [ ] **ingress queue + inbox 去重** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **transactional outbox + delivery DLQ** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **多副本 lease 与幂等/恢复测试** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
- [ ] **C4 WebChat 身份与 Gateway** — 服务端身份派生、鉴权端点、5000 WS 连接；`proposed`
  - [ ] **服务端身份派生 + 鉴权端点**（越权矩阵全 403/404） → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **5000 WS 连接压测** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
- [ ] **C5 生产部署与弹性** — 单实例 → 多 Worker → 多 Gateway → 容器编排；`planned`
  - [ ] **多 Worker / 多 Gateway 部署演进** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **N+1 故障下 SLO + runbook 演练** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
- [ ] **C6 指标驱动优化** — 仅指标证明必要时启用 Redis cache / read replica 等；`planned`
  - [ ] **瓶颈/基线测量 → Redis cache 或 read replica 按证据启用** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)
  - [ ] **收益/回滚记录** → outcome 见 [SCALING_ROADMAP §4](SCALING_ROADMAP.md)

> **扩展方式**：phase 进一步细化时，在本文件对应 phase 小节新增父条目或子条目，并链接到新建的
> OpenSpec change / spec / record / evidence；**不往 SCALING_ROADMAP.md 追加细化条目**（roadmap 只承载总体目标与决策）。
