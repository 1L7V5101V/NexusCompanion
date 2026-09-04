# NexusCompanion Pilot 任务计划集

> 本目录是 Pilot 14 项 change 的**任务计划集**（计划文档，非活跃 OpenSpec change）。
> 源路线图：`openspec/PILOT_ROADMAP.md`（1686 行，§1–§10）。编号、依赖与验收标准一律以路线图为准。
> 本目录**不**放入 `openspec/changes/` 活跃 change 目录：这些文件是任务计划，不触发 `openspec validate`/`openspec list` 的 change 生命周期。当某 task 真正开工时，在 `openspec/changes/<date>-<slug>/` 单独建 change 并引用对应 task-NN。

## 1. 目录定位

本目录回答「Pilot 一共要做什么、以什么顺序做、每件做到什么程度才算完成」。它把 `PILOT_ROADMAP.md §5.9.10` 已冻结的 14 项 capability/change 拆为 14 个独立可验收的子任务，并配套提供任务依赖图（`dag.md`）、里程碑定义（`milestones.md`）与分析报告（`analysis-report.md`）。

## 2. 文件导航

| 文件                                     | 定位                                                                   | 对应 §5.9.10 第 N 项 |
| -------------------------------------- | -------------------------------------------------------------------- | ---------------- |
| `README.md`                            | 本导航文件                                                                | —                |
| `task-01-canonical-identity.md`        | C1 canonical identity + PG conversation/message 基础                   | 1                |
| `task-02-durable-control-plane.md`     | C2 durable ingress/inbox + turn/work + outbox/delivery               | 2                |
| `task-03-admission-queue-recovery.md`  | C3 tenant admission + bounded queue + restart recovery               | 3                |
| `task-04-webchat-protocol-dev-loop.md` | C4 WebChat protocol + dev-only channel/Gateway/frontend              | 4                |
| `task-05-auth-provisioning-admin.md`   | C5 invitation auth + provisioning/readiness + admin/browser security | 5                |
| `task-06-attachment-media.md`          | C6 authenticated attachment/media lifecycle                          | 6                |
| `task-07-tool-context-isolation.md`    | C7 tenant tool context/resource/effect isolation                     | 7                |
| `task-08-runtimesnapshot-secrets.md`   | C8 RuntimeSnapshot lease + hook failure + tenant secret/revocation   | 8                |
| `task-09-persona-relationship.md`      | C9 Persona/Relationship tenant storage + optimizer concurrency       | 9                |
| `task-10-telegram-binding-sync.md`     | C10 Telegram binding + cross-channel synchronization                 | 10               |
| `task-11-explicit-schedules.md`        | C11 tenant-owned explicit schedules + recovery                       | 11               |
| `task-12-observability-backup.md`      | C12 observability/privacy/redaction + backup manifest                | 12               |
| `task-13-memory-retrieval-bm25.md`     | C13 memory retrieval BM25/hotness/RRF + 离线评测                         | 13               |
| `task-14-memory-engine-catalog.md`     | C14 memory engine plugin catalog/binding + WebChat selector          | 14               |
| `dag.md`                               | DAG 依赖图（Mermaid + 边表 + 拓扑批次）                                         | —                |
| `milestones.md`                        | 里程碑定义（P-1..P4 + §6 出口条件引用）                                           | —                |
| `analysis-report.md`                   | 分析报告（§8 大纲展开）                                                        | —                |

## 3. 编号约定

- `task-NN` 的 NN 严格对应 `PILOT_ROADMAP.md §5.9.10` 第 N 项（1..14），跨文档引用稳定，**不允许重排序或改号**。
- `C1`..`C14` 为对应 change 的缩写，与 `task-01`..`task-14` 一一对应。
- `P1GATE`（公网 delivery/recovery 门禁）与 `UMCP`（用户 MCP capability）是两个**派生节点**：非独立 change，仅用于 DAG 依赖表达，见 `dag.md`。

## 4. 状态规则

统一引用 `PILOT_ROADMAP.md §8`：

- `planned`：已确定但尚未实现；
- `in_progress`：已有 active OpenSpec change；
- `verified`：有合并 commit、可复现测试或 benchmark 证据；
- `blocked`：依赖或外部条件未满足；
- `deferred`：明确不属于当前 Pilot，等待升级闸门触发。

状态**只允许证据驱动更新**（§8）：只有「merge commit + 可复现证据」才置 `verified`；多阶段 change（C3/C7/C8/C12）按段验收，每段独立 `verified`。P-1 设计冻结**不**标记任何能力为 `verified`（§6 P-1 出口：「P-1 完成只代表设计冻结，不标记任何目标能力为 verified」）。

每个 task 文件内的「验收标准」是证据清单；仅当全部 checkbox 对应的证据真实产生且可复现时，该 task 才能视为完成。

## 5. 依赖与执行顺序

- 依赖图见 `dag.md`（Mermaid + 规范边 D1–D7 + 派生边 E1–E10 + 拓扑批次 0–3）。
- §5.9.10 依赖边界**不可跨越合并**：可以合并相邻小 change，但不得跨越「1 是 2/4/5/10 前置、2+3 是公网前置、5 是 6/7/10/11 前置、7 是用户 MCP 前置」这些边界形成“大一统 migration”。
- 并行说明：同拓扑批次内可并行；批间有依赖。`C12` 以虚线贯穿（先定义契约、伴随各 change 落地）；`C13` 独立质量能力，不阻塞 C4/C5。

## 6. 组合规则：所有子任务完成后如何组合成最终结果

组合 = 沿 DAG 边的**产出接力**，而非事后拼接。每个子任务的「输出」是下游子任务的「输入」。接力契约（详见 `analysis-report.md` §15 与 `milestones.md`）：

| 组合点 | 上游产出 | 下游消费 |
| --- | --- | --- |
| identity seam | C1 的 account→tenant→canonical conversation resolver + canonical 表 | C2/C4/C5/C10/C9/C14 |
| durable seam | C2 的 inbox/outbox/delivery 事务 + 幂等键 | C4、C10、P1GATE |
| admission seam | C3 的 tenant lane + 有界队列 + overload | C4、P1GATE |
| auth seam | C5 的 principal/session/provisioning | C6/C7/C9/C10/C11/C14 |
| runtime seam | C8 的 TenantRuntimePlan/PluginInvocationContext | C7、C14 |
| retrieval seam | C13 的 default 召回基线 | （可选）C14 的 default engine 实现 |
| observability seam | C12 的采集规范/redaction/backup manifest | 各 Cxx 落地时同步实现其字段 |

整体组装目标：

1. **M-P0.5 组装**：C1+C2+C3+C4 → dev-only WebChat 最小可用闭环（收发消息、断线重连不重复、durable 三事务收束、tenant admission 生效）。
2. **M-P1 组装**：再叠加 C5+C6+C7(allowlist)+C9+C10+C14 + C2∧C3(P1GATE) → 公网可用 Pilot 客户端（一次 Token 登录、首次人设设置、Telegram 绑定同步、memory engine selector、attachment 认证、工具 allowlist、公网 delivery/recovery 承诺）。
3. **M-P2 组装**：叠加账号封禁取消传播、Dashboard 管理、C11、C7 负向测试、C8 revocation gate、UMCP → 管理员可低成本运营十几个测试用户。
4. **M-P3 组装**：叠加恢复演练、backup manifest 演练、总控台聚合、retention → 「适合长期跑」。
5. **最终验收**：对照 `PILOT_ROADMAP.md §7` 验收项表与 §6 各阶段出口条件逐项验证。

集成回归基线：每个 change 完成后跑 `pytest` + `pyright` 对齐 main 基线；`openspec validate` 通过 → `openspec status` 确认 → 仅在 evidence 齐全时更新 `PILOT_ROADMAP_PROJECT_CHECKLIST.md` 与 `PILOT_ROADMAP.md` 状态（§8 规则）。

## 7. 从计划转 change（开工协议）

当任一 task 决定开工时：

1. 在 `openspec/changes/<YYYY-MM-DD>-<slug>/` 建 `proposal.md` + `tasks.md` + `design.md`（OpenSpec change 结构，参考 `changes/archive/2026-08-23-c0-observability-load/`）。
2. change 文件头部引用对应 task-NN，并列出本 task 的「输入」中已冻结的决策（不重复分析路线图）。
3. 复刻本 task 的「验收标准」为 change 的 checkbox 清单；每完成一项，在 `openspec/evidence/` 留下可复现脚本与原始结果。
4. change 完成且 evidence 齐全后按 §8 更新状态，再进入 archive。