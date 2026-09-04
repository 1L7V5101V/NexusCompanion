# PILOT_ROADMAP 分析报告（草稿）

> 本报告是对 `openspec/PILOT_ROADMAP.md`（1686 行，§1–§10）的任务拆分与执行规划分析，配套文件：14 个子任务 MD（`task-01..task-14`）、DAG 依赖图（`dag.md`）、里程碑定义（`milestones.md`）。所有「§」引用一律指向 `PILOT_ROADMAP.md`。
> 状态：**草稿**（analysis draft）。与路线图或下游 evidence 冲突时，以路线图 §5.9.10 规范依赖与验证代码/测试证据为准（openspec README 使用原则 2）。

---

## 1. 执行摘要

Pilot 的定位是**小范围、长期、可控的试点**：10–30 个受邀账号、5–15 个并发 WebChat 会话、单机/VPS、PostgreSQL + pgvector 为唯一长期主数据源（§1/§2）。当前 WebChat 不可用（只有 `bootstrap/chat_api.py` / `app.py` 骨架，缺 adapter/前端/auth），`core/telemetry/`（metrics/metrics_export/trace_store）已 verified 可复用，14 项 change 均 `planned`。§5.9.10 已将变更冻结为 **14 项相互可验收的 capability/change**，本报告将其拆为 14 个独立子任务（goal/inputs/outputs/acceptance_criteria），构建依赖 DAG 与七阶段里程碑，明确「所有子任务完成后如何组合成最终结果」。

## 2. 路线图目标（§1）

- **规模**：10–30 受邀账号；5–15 WebChat 会话；单机/VPS。
- **客户端**：WebChat 为正式客户端；Telegram Bot 双入口共享规范会话历史。
- **登录**：一次 Token 登录；可撤销会话；错误响应不泄露存在性。
- **身份**：`account_id → tenant_id → canonical_conversation_id` 服务端派生（§5.9.1/§5.9.2）。
- **能力**：memory engine 选择（default/rachael）、封禁即时生效、工具多租户隔离。
- **实现原则**：实现简单、可运营、不提前建设 5000 用户基础设施（§2 明确不做清单）。

## 3. 范围与运行边界（§2 + §3）

| 维度 | 冻结结论 |
| --- | --- |
| 目标用户 | 10–30 受邀账号 / 5–15 WebChat 会话 / 单机 VPS |
| 明确不做 | Kafka/NATS/Redis Streams、Nginx 必需、多 Gateway/Worker/K8s、独立向量库、5000 用户压测、自助注册/复杂 RBAC/独立管理后台/长期 JWT |
| 技术栈 | FastAPI/Uvicorn、进程内 `asyncio.Queue`、PostgreSQL + pgvector、Cloudflare Tunnel、阿里云 text-embedding-v3、jieba + ParadeDB pg_search、hotness + RRF |
| 运行时模型 | Pipeline / Stage-Module / Executor / Scheduler-Loop / In-process Worker / Horizontal Worker / Maintenance 分层（§3.1）；memory retrieval 与 tool call 属 stage/executor，**不统称 Worker** |
| 并发边界 | 租户内串行、租户间异步；一个 tenant 恰一个 canonical conversation；无全局 maintenance lock |
| 组件职责 | Gateway（FastAPI/Uvicorn）→ 入站接受 → tenant lane → Pipeline/Stage/Executor → durable 落库 → outbound delivery（§3 组件职责） |

## 4. 当前实现地图与阶段接缝（§3.3 + §3.4）

当前单体基线 → Pilot 解决路径的关键接缝（决定 change 拆分/协议/表结构/恢复语义）：

| 现状 | Pilot 目标 | 归属 change |
| --- | --- | --- |
| WebChat 骨架存在，缺 adapter/前端/auth | WebChat 正式客户端（P0.5 dev，P1 公网） | C4/C5 |
| MessageBus/Passive lane 无界进程内 | 有界 queue + overload + tenant admission | C3 |
| 无 durable Outbound delivery record | fork outbox/delivery 三事务状态机 | C2 |
| turn control 仍可落 SQLite turn_audit.db | PostgreSQL canonical turn/tool/work | C2 |
| 工具缺统一 tenant ownership/timeout/幂等/compensation | ToolExecutionContext + catalog + capability matrix | C7 |
| Persona 进程级全局 | PG tenant PersonaProfile/RelationshipState | C9 |
| schedule 全局 JSON | tenant-owned durable schedules | C11 |
| SQLite FTS5 BM25 / PG 仅 summary ILIKE | jieba + pg_search + hotness + RRF 基线 | C13 |
| tenant provisioning 单进程内存 | PG provisioning/readiness（P1） | C5 |
| `/tmp/nexus_uploads` 无 owner/metadata | immutable attachment_id + PG metadata | C6 |
| 插件无 tenant snapshot lease | RuntimeSnapshot lease + TenantRuntimePlan | C8 |
| 无统一观测 | §7.1 指标 + redaction + backup manifest | C12 |

## 5. 记忆召回链路（§4）

- 当前 `DefaultMemoryEngine`（§4.1）：intent 分流、dense/keyword/RRF、hotness 语义。
- 外层 `DefaultMemoryRetrievalPipeline`（§4.2）边界。
- Memory engine 插件与 WebChat 选择（§4.3）：default（初始）/rachael（可选）。
- `default` 引擎 Pilot 目标实现（§4.4）：**raw query + dense(semantic+hotness) + BM25(hotness) + RRF + top-k**；HyDE/query rewrite/reranker 默认关闭 + 消融矩阵 A/B/C/D（→ C13）。
- P0 出口要求：两条 lane 的 hotness 为长期保留默认组成；该 change 不阻塞 P0.5/P1 的 WebChat 与认证安全闭环（→ C13，与 C4/C5 并行）。

## 6. 一次 Token 登录与可撤销会话（§5）

- **用户体验**（§5.1）：管理员发一次性邀请 Token，用户兑换一次即建会话。
- **Token/session 边界**（§5.2/§5.9.3）：HttpOnly `__Host-` Cookie；digest-only 存储（HMAC-SHA-256 + pepper）；普通/admin 分离；CSRF + Origin；401/403 契约。
- **封禁与撤销**（§5.3/§6.1 D）：账号级撤销、Token 级撤销、原因记录；封禁截断 tenant work（lane 取消 + WebSocket 断开）；revoked 不原地恢复。
- **推荐端点**（§5.4）：`POST /api/auth/exchange` 等（→ C5）。
- **管理端**（§5.5）：复用 React Dashboard 为管理端 + CLI 应急；不另建独立管理后台（→ C5/C12）。
- **双入口同步**（§5.6）：PostgreSQL 统一消息流 + WebSocket 实时推送 + 游标补拉（→ C2/C4/C10）。
- **人设/关系/配置边界**（§5.7）：四层 prompt 来源（RuntimeInvariant / PersonaProfile / RelationshipState / ChannelPolicy）；PersonaProfile 提交后固定；RelationshipState 原地更新（→ C9）。
- **工具隔离**（§5.8）：四道边界 + ToolExecutionContext + 工具目录分层（Global/Tenant/Admin）+ 作用等级 + 用户 MCP + 路径管理 + 外部作用/确认/幂等/审计 + 状态联动 + 最小闸门（→ C7）。

## 7. 编码前冻结决策（§5.9）

14 项 change（§5.9.10）逐项对应 task-01..task-14；依赖顺序与 §5.9.10 依赖边界一致（详见 `dag.md`）。§5.9 其余小节为各 task 的冻结语义：

- §5.9.1 硬冲突表（identity/session/WS replay/tool context 四类）→ C1/C4/C7
- §5.9.2 canonical identity + Telegram binding → C1/C10
- §5.9.3 auth/admin/browser security → C5
- §5.9.4 WS 协议/游标/慢消费者 → C4
- §5.9.5 admission/overload（128/16/64/1/256-soft192-1MiB；30/4/8/2）→ C3
- §5.9.6 durable/recovery → C2/C3
- §5.9.7 ToolExecutionContext 注入边界 → C7
- §5.9.8 Persona 当前值更新与调试权限 → C9
- §5.9.9 数据模型/首次启用/schema evolution（Create→Verify→Enable；唯一约束）→ C1/C2/C5/C6/C10/C11/C14
- §5.9.10 change 拆分与开工顺序（14 项 + 依赖）→ 本报告 §11
- §5.9.11 ingress/canonical/outbox/delivery 三事务 → C2
- §5.9.12 persistence/backup manifest → C1/C12
- §5.9.13 provisioning/readiness → C5
- §5.9.14 schedule（tenant-owned；`(job_id, scheduled_for)` 幂等；5min grace）→ C11
- §5.9.15 attachment（immutable id / MIME / 20MiB / 24h cleanup / reconciliation）→ C6
- §5.9.16 RuntimeSnapshot/hooks/secrets/revocation（installed/active/binding 三态；绑定策略表；engine slot）→ C8/C14
- §5.9.17 observability/privacy（结构元数据默认；redaction；label 规则；retention 30/180）→ C12

## 8. 分阶段路线与出口条件（§6 + §6.1）

七阶段 P-1 → P0 → P0.5 → P1 → P2 → P3 → P4 的目标、产出与出口条件详见 `milestones.md`。§6.1 运行时可靠性细化：

- **A. Tenant-scoped serial lane**：lane key = 服务端派生 `tenant_id`；interactive 优先；无全局 maintenance lock；lane owner 统一释放（→ C3）。
- **B. Durable recovery/replay**：inbound/outbound/maintenance/active turn/schedule/tick/provisioning/attachment 恢复契约 + `unknown` / `compensation_required` 区分 + spinner 字段（→ C2/C3/C11/C6）。
- **C. Tool capability inventory**：每类工具一行 matrix（identity/ownership/side effect/timeout/cancellation/retry/idempotency/outcome query/compensation/terminal mapping）+ 2026-08-28 盘点基线（→ C7）。
- **D. Account-ban cancellation and timeout fallback**：认证检查前置、lane cancel、协作式取消、timeout 兜底、`unknown` 不盲重试（→ C5/C3）。

## 9. 验收指标（§7）

- **观测字段**：work_kind / flow / stage / backlog / latency / skip / failure / timeout / tenant_id / session_key（§7.1）。
- **必采集数据**：任务生命周期事件（入队/开始/结束/取消/异常）、按维度聚合（kind/flow/stage/tenant/channel/model）、数据保留与验收规则（→ C12）。
- **验收项表**（§7）：首次登录 / 持久登录 / 撤销生效 / 隔离 / 双入口在线 / Telegram 同步 / 重启恢复 / 队列安全 / 运行时顺序 / 用户切换 / 全局总控 / 运维。
- **SLO 策略**：先采集后设（§5.9.17）；阻断性问题（丢任务/跨 tenant 污染/重复副作用/无主任务）不等 SLO 直接阻断里程碑。

## 10. 状态与决策规则（§8 + §9）

- 状态：`planned / in_progress / verified / blocked / deferred`；仅证据驱动更新（§8）。
- 决策摘要（§9，节选）：管理模式＝管理员发一次性 Token；登录状态＝HttpOnly Cookie（非长期 JWT/LocalStorage）；Gateway＝FastAPI/Uvicorn（非 Nginx 必需）；队列＝进程内 `asyncio.Queue`；DB＝PostgreSQL + pgvector；公网＝Cloudflare Tunnel；管理方式＝复用 Dashboard + CLI；双入口并存共享规范会话；消息顺序与幂等＝per-conversation BIGINT sequence + 双键去重；WS 恢复＝final durable + delta 非 durable；浏览器安全＝`__Host-` 分离 + CSRF + Origin；Queue overload＝interactive 拒绝 / maintenance 合并；Provisioning＝ready 前不发 Token；Explicit schedule＝tenant-owned + `(job_id, scheduled_for)`；Attachment＝immutable id + tenant namespace；RuntimeSnapshot＝lease + per-task plan；Observability＝默认结构化元数据；Tool context＝immutable per-call；Telegram 身份＝管理员预绑定/一次性码；Persona＝current-value 模型（固定 + 原地演化）；Runtime 术语＝Pipeline/Stage/Executor 不统称 Worker；运行时负载＝interactive/maintenance 区分。

## 11. 任务依赖 DAG 与执行顺序（对应 `dag.md`）

- 节点：C1..C14 + 派生节点 P1GATE（C2∧C3 公网门禁）、UMCP（用户 MCP）。
- §5.9.10 规范边 D1–D7：`1→{2,4,5,10}`、`2+3→P1GATE`、`5→{6,7,10,11}`、`7→UMCP`、`12` 虚线贯穿、`13` 独立不阻塞 4/5、`14` 依赖 C1+C4+C5+C8 且不依赖 C13。
- 派生边 E1–E10（理由见 §6）：C2→C4、C3→C4、C4→C5、C8→C7、C5→C9、C1→C9、C2→C10、C4→C10、C1→C3（弱）、C1/2/3→C12（伴随）。
- 拓扑批次：**批次 0**（并行根）C1/C3/C12/C13 → **批次 1** C2/C4/C5（+C8 早启）→ **批次 2** C6/C7/C9/C10/C11/C14 → **批次 3** UMCP + 恢复演练（C3/C6/C11/C12 收尾）。
- 并行/串行：同批并行、批间有依赖；C13 与 C4/C5 无依赖边；C12 不阻塞任何主线。
- Mermaid 图见 `dag.md` §5。

## 12. 里程碑定义（对应 `milestones.md`）

14 项 → P-1/P0/P0.5/P1/P2/P3/P4 映射总表 + 每里程碑 §6 出口条件引用 + 跨阶段 change（C3/C7/C8/C12）分段验收 + P1GATE 检查点 + P4 触发信号。详见 `milestones.md`。要点：

- M-P-1：全部 14 项只产设计/spec，**不标 verified**。
- M-P0：C3 核心 + C8 lease audit + C13 召回基线 + C12 基线 + C1 schema。
- M-P0.5：C1 收尾 + C2 + C4 + C3 兑现 + C7 最小接缝 → dev-only WebChat 闭环。
- M-P1：C5+C6+C7(allowlist)+C9+C10+C14 + P1GATE → 公网可用 Pilot。
- M-P2：C7 收尾 + C11 + C8 收尾 + UMCP + 账号控制/Dashboard。
- M-P3：恢复演练 + backup manifest 演练 + 总控台聚合 + retention。
- M-P4：无 change，仅评估（升级闸门信号见 §13）。

## 13. 升级触发条件（§6 P4）

七类真实信号（WS 并发超单机 / backlog 持续增长 / latency 持续超标 / maintenance 挤压 / 恢复窗口不可接受 / 多 Gateway 需求 / 账号超 30）触发扩展评估；升级顺序固定：指标+压测 → 保留 FastAPI 认证/账号模型 → Redis Streams consumer group → transactional outbox/DLQ/多 Worker → Nginx/多 Gateway/容器编排（§6 P4）。

## 14. 风险与待决策项（§10）

**四类待决策分级**（阶段计划不得把 DECIDED 降级为「由实现自行选择」）：

| 级别 | 主题 | 待办 |
| --- | --- | --- |
| DECIDED | 旧单体数据边界 / DB rollout / Admin bootstrap / Ingress-outbox-delivery / Persistence ownership / Provisioning lifecycle / Tenant admission / Consolidation 阈值 / Persona 语义 / Explicit schedule / RuntimeSnapshot / 工具取消 / 副作用契约 / Queue 容量初始值 | 各 task 的 design/spec 直接落地（如 C1 的「不导入 SQLite」、C3 的容量表、C8 的插件可信判断） |
| PROPOSED DEFAULT | Attachment policy（20MiB/24h/allowlist）、Schedule misfire（5min）、日志与审计保留（30/180 天） | P-1 接受或修改后固化测试（C6/C11/C12） |
| OPEN FOR P-1 SPEC | Exact schema/DDL、WebSocket frame/error schema、Digest/encryption/key rotation | C1/C4/C5/C8 开工前提交可执行 DDL、protocol fixture、算法与 runbook |
| DEFERRED BY EVIDENCE | SLO/容量阈值、Queue backend/多副本、Retrieval 增强开关 | 基线报告后设（C12/C3/C13） |

**重启恢复建议表**（§10）：inbound→持久化 inbox；Passive lane→inbox/turn 表重建；interactive turn→收束为 interrupted/cancelled/unknown + 先 query outcome；final outbound→不重新生成只重投 pending/attempting；WS delta→丢弃补拉；consolidation→按 last_consolidated 幂等重跑；schedule→从 PG 恢复 `(job_id, scheduled_for)` 去重；provisioning→恢复 pending/failed retry；attachment→校验 blob + orphan 清理；proactive/optimizer tick→重算下次不补发。

**主要风险与缓解**：跨阶段 change 验收分散→分段验收；C12 贯穿→每个 Cxx 落地协议含指标/redaction/backup 条目；C13↔C14 解耦→D7 不强依赖 + 依赖范围检查；旧 SQLite 误入 fallback→grep 防御 + legacy 独立备份；P1GATE 误判→显式检查点标注；E9 弱边→切换对齐点集成测试；`openspec validate` 误识别 tasks/→tasks/ 不放 .openspec.yaml/proposal.md。

## 15. 组合与验收（「所有子任务完成后如何组合成最终结果」）

**组合 = 沿 DAG 边的产出接力**（每个子任务输出是下游输入），非事后拼接：

| 组合点 | 上游产出 | 下游消费 |
| --- | --- | --- |
| identity seam | C1 resolver + canonical 表 | C2/C4/C5/C10/C9/C14 |
| durable seam | C2 三事务 + 幂等键 | C4/C10/P1GATE |
| admission seam | C3 tenant lane + bounded queue | C4/P1GATE |
| auth seam | C5 principal/session/provisioning | C6/C7/C9/C10/C11/C14 |
| runtime seam | C8 TenantRuntimePlan/PluginInvocationContext | C7/C14 |
| retrieval seam | C13 召回基线 | （可选）C14 default engine 实现 |
| observability seam | C12 规范/redaction/manifest | 各 Cxx 落地同步实现 |

组装路径：

1. **M-P0.5 组装** = C1 + C2 + C3 + C4（+ C7 最小接缝）→ dev-only WebChat 最小可用闭环。
2. **M-P1 组装** = M-P0.5 + C5 + C6 + C7(allowlist) + C9 + C10 + C14 + P1GATE → 公网可用 Pilot 客户端。
3. **M-P2 组装** = + 封禁取消传播 + Dashboard 管理 + C11 + C7 负向测试 + C8 revocation gate + UMCP → 管理员可运营十几用户。
4. **M-P3 组装** = + 恢复演练 + backup manifest 演练 + 总控台聚合 + retention → 适合长期跑。
5. **最终验收** = 对照 §7 验收项表（12 项）与 §6 各阶段出口条件逐项验证；阻断性问题不等 SLO 直接阻断。

**集成与回归基线**：每个 change 完成后 `pytest` + `pyright` 对齐 main；`openspec validate` → `openspec status` → sync-specs → archive；evidence 齐全才更新 `PILOT_ROADMAP_PROJECT_CHECKLIST.md` / `PILOT_ROADMAP.md` 状态（§8）。

---

## 附：本报告与配套文件的关系

| 文件 | 内容 | 给谁用 |
| --- | --- | --- |
| `task-01..14` | 14 项 goal/input/output/acceptance_criteria + 阶段 + §5.9.10 依赖 | 各 change 的实现 Agent（开工 → 转 `openspec/changes/`） |
| `dag.md` | Mermaid DAG + 规范/派生边 + 拓扑批次 | 排期与并行调度 |
| `milestones.md` | 七里程碑 + §6 出口条件 + 分段验收 + P1GATE/P4 | 里程碑评审与回归门禁 |
| `README.md` | 导航 + 状态规则 + 组合规则 + 转 change 协议 | 所有读者 |

> 本报告为分析草稿；下游实现与验收以各 task 文件与路线图原始语义为准。当发现本报告与 task 文件/路线图冲突时，以 `dag.md` 与 `milestones.md` 中附 § 引用的条目为优先仲裁依据，必要时回查 `PILOT_ROADMAP.md` 原文。