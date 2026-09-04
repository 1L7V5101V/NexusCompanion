# Pilot 里程碑定义（P-1 → P4）

> 里程碑依据 `PILOT_ROADMAP.md §6`「分阶段路线」与 §5.9.10 依赖顺序。14 项 change（C1..C14）映射到七阶段，每个里程碑的「出口条件」直接引用 §6 对应小节；出口条件未满足则阶段不可关闭。
> 状态规则复用 §8：里程碑「完成」= 其所属 change 全部 `verified`（merge commit + 可复现证据）**且** §6 出口条件满足。

## 1. 阶段 → change 映射总表

| 里程碑                        | 阶段定位                 | 本阶段落地的 change                                                                                                                                                                                         | §6 出口条件引用（节选）                                                                                                                                                        |
| -------------------------- | -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **M-P-1** 编码前决策冻结          | 设计/ADR/spec，不写应用功能代码 | 全部 14 项各产出 design/spec（契约/fixture/状态机/表约束/错误码/回滚路径）；C12 先定义契约；C13 评测设计                                                                                                                                | §6 P-1 出口：5.9 表中不存在会阻塞首个 change 的「由实现决定」事项；每个 change 能说明输入/输出/状态/失败语义/DB rollout/测试证据；完成只代表设计冻结，**不标 `verified`**                                                    |
| **M-P0** Pilot 基础运行基线      | 最少可复现运行基线            | C3（admission+queue+overload 主体）、C8（lease coverage audit）、C13（召回基线）、C12（persistence/backup manifest 基线 + 日志脱敏基线）、C1（PG schema 基础 + identity resolver）                                                  | §6 P0 出口：Telegram Bot 连续运行 7 天；重启后已提交持久数据不丢；queue/task 丢失窗口 + P0.5 前置有记录；DB/配置/workspace 可恢复；同 canonical conversation 无序并发写入；maintenance 不挤压 interactive；FastAPI 可启动 |
| **M-P0.5** WebChat 最小可用闭环  | dev-only WebChat     | C1（canonical stream+sequence 收尾）、C2（durable inbox/outbox/delivery）、C4（WebChat protocol+前端）、C3（admission 基线兑现）、C7（ToolExecutionContext/TenantToolCatalog/ToolPolicy 最小接缝，不公网）                          | §6 P0.5 出口：本地/dev 打开 WebChat 收发消息+流式；刷新/断线重连不重复；顺序稳定；异常连接清理；协议测试通过；仍不视为公网客户端                                                                                         |
| **M-P1** 一次 Token 登录（公网门禁） | 受邀用户低摩擦登录 + 服务端身份派生  | C5（auth+provisioning+admin）、C6（attachment）、C7（普通 tenant 开放内置工具 allowlist）、C9（Persona/Relationship）、C10（Telegram binding+sync）、C14（memory engine selector）；**C2+C3 满足 P1GATE** 公网 delivery/recovery 承诺 | §6 P1 出口：一次 Token 登录；首次人设设置可完成且从 PG 恢复、用户侧不可再改；刷新/重开仍登录；越权全拒；重复兑换不产生多账号                                                                                              |
| **M-P2** 账号控制与长期试用         | 管理员低成本运营             | C7 收尾（资源/effect 隔离+负向测试）、C11（explicit schedules）、C8 收尾（per-task context+revocation gate）、UMCP（用户 MCP，C7 完成后独立 capability）、账号封禁取消传播、Dashboard 管理                                                       | §6 P2 出口：Dashboard 新增/停用 Persona 模板+切换用户+1 分钟内定位封禁；模板变更不影响已有 tenant；Dashboard 故障可用 CLI；封禁后新旧连接均不可用；误封可解除 suspended 重发 Token；revoked 不原地恢复                            |
| **M-P3** 稳定性与备份            | 「能跑」→「适合长期跑」         | C3 恢复演练、C11 恢复演练、C6 恢复演练、C12 backup manifest 演练 + 总控台指标聚合、Persona 当前值备份/PITR、delivery dead-letter 重投、provisioning retry、attachment 恢复                                                                 | §6 P3 出口：从备份恢复演练；总控台查看聚合运行状态/缓存命中率/Token 消耗+下钻；区分应用/DB/上游模型/账号滥用故障；长期运行无未解释数据丢失                                                                                      |
| **M-P4** 升级闸门              | 仅真实信号触发扩展            | 无 change（仅评估）                                                                                                                                                                                         | §6 P4：见「5. P4 触发信号清单」与升级顺序                                                                                                                                           |

## 2. 每里程碑出口条件明细（引用 §6）

### M-P-1（编码前决策冻结）
- 完成 §5.9 全部设计门禁：canonical identity / auth / WS / admission / durable / persistence / provisioning / ToolExecutionContext / Persona / schedule / attachment / RuntimeSnapshot / observability / DB rollout（§6 P-1 bullet 1）。
- 按 §5.9.10 为 identity、control-plane、WebChat、auth/provisioning、attachment、tool/snapshot、Persona、Telegram、schedule、observability、retrieval 建立独立 change 边界、依赖图与验收命令（§6 P-1 bullet 2）。
- 固定协议 fixture、状态机、表约束、错误码和回滚路径（§6 P-1 bullet 3）。
- 未定的纯运行参数给配置项、默认值、压测后调整方式（§6 P-1 bullet 4）。
- **P-1 完成 ≠ `verified`**：不将任何目标能力标记为 verified（§6 P-1 出口末句）。

### M-P0（Pilot 基础运行基线）
- FastAPI/Uvicorn 应用层可启动；单进程 AgentLoop + `asyncio.Queue`；PostgreSQL + pgvector 为主数据源；Cloudflare Tunnel HTTPS/WSS（§6 P0）。
- 健康检查、结构化日志、基础错误告警 + 当前 persistence/backup manifest + 默认日志 secret/PII/路径脱敏（→ C12 基线）。
- 两类 work kind（interactive/maintenance）+ 五类 flow（passive/proactive/drift/consolidation/optimizer）基线；tenant-scoped admission；tenant 内不并发；不同 tenant 互不阻塞；进程内 queue 有保守容量与 overload 行为（→ C3）。
- default engine 召回基线（raw + dense/hotness + BM25/hotness + RRF + top-k），双 lane hotness 长期保留（→ C13）。
- 工具调用 tenant scope/effect 基线（普通 tenant 不开放 shell/全局 MCP/Peer Agent/插件管理）（→ C7 接缝）。
- 人设 prompt 四层来源收束（RuntimeInvariant/PersonaProfile/RelationshipState/ChannelPolicy）（→ C9 spec）。
- **RuntimeSnapshot lease coverage audit**（→ C8）：Passive/Proactive/Drift/maintenance/plugin job 都按 §5.9.16 绑定 snapshot。
- tenant plugin policy/catalog 最小执行接缝（→ C8）+ HyDE/rewrite/reranker 默认关闭 + 离线评测入口（→ C13）。

### M-P0.5（WebChat 最小可用闭环）
- WebChat 后端 channel adapter（WS 入站 → 内部消息；回复/流式/工具状态/终态 → 连接）（→ C4）。
- `turn_id`/`message_id`/`sequence`/`tenant_id` 贯穿 turn、tool、outbound 与重连补拉；WS 断线只影响显示（→ C4 + C2 durable）。
- Gateway HTTP/WS 路由、连接生命周期、心跳、断线清理、稳定 message_id/sequence 协议（→ C4）。
- 协议与错误协议覆盖 hello/send/delta/completed/error/replay（→ C4 fixture，§10 OPEN FOR P-1 SPEC）。
- WebChat 前端 bundle（消息列表/输入/发送状态/流式/连接/重连/错误）（→ C4）。
- 接通 durable ingress/inbox + canonical stream + outbox/delivery，三事务按 §5.9.11 收束（→ C2 + C1）。
- contract test：channel/Gateway/Cookie-Origin/重连/client_message_id/顺序/慢消费者/前端交互（→ C4）。
- ToolExecutionContext/TenantToolCatalog/ToolPolicy 最小接缝（→ C7），P1 认证与负向测试前不公网暴露 tenant-facing 工具。
- 所有入口收敛到 `account → tenant → canonical_conversation` 服务端映射（→ C1）。
- dev-only 门禁（本地或显式 dev mode 临时单用户身份）。

### M-P1（一次 Token 登录：公网门禁）
- `test_accounts`/`access_tokens`/`auth_sessions` + provisioning readiness；账号先 provisioning、ready 后签发 Token；普通/admin 分离 session + Cookie/CSRF/Origin/timeout/401-403（→ C5）。
- 邀请 Token 生成/hash/过期/一次性兑换/撤销（→ C5）。
- `POST /api/auth/exchange` + HttpOnly Cookie（→ C5）。
- HTTP/上传/媒体/WebSocket 统一认证；attachment 按 §5.9.15（→ C6）。
- 登录过期/退出/失效 401/403（→ C5）。
- WebChat memory engine selector（GET 允许目录 / PUT 选择 / 持久化 / 下个 work 生效）（→ C14）。
- `tenant_id` 服务端派生贯穿 Passive/Proactive/Drift/ToolExecutor/retrieval/consolidation/optimizer；engine 由 active binding 派生，不信任客户端提交（→ C5/C8/C14）。
- tenant plugin settings/catalog/KV 接入 PG canonical control-plane；active memory engine 持久化；hard revocation 副作用前即时生效（→ C8）。
- PersonaProfile/RelationshipState 绑定 tenant（→ C9）。
- 首次登录一次性人设设置流程 + 提交后锁定用户侧编辑（→ C9）。
- Telegram 私聊身份 ↔ WebChat 账号可信关联 + 幂等去重（→ C10）。
- **P1GATE**：C2 ∧ C3 满足公网 delivery/recovery 承诺（本里程碑关闭前必须确认通过）。

### M-P2（账号控制与长期试用）
- Dashboard 管理视图（active engine/允许目录/切换记录/engine 指标）、签发/Token 状态/会话查看/封禁/解封（→ C5 + C12）。
- 账号选择器 tenant 化复用（sessions/proactive/logs/metrics/memory/plugin 页）（→ C12 + Dashboard）。
- Persona 模板新增/停用/预览；模板变更只影响后续 onboarding，不覆盖已建 tenant 快照（→ C9）。
- CLI 保留 issue/list/revoke/expire 应急入口（→ C5）。
- 账号级撤销、Token 级撤销、原因记录（→ C5）。
- 封禁主动断开 WebSocket + lane 取消传播（→ C5/C3 + §6.1 D）。
- 基础限流、并发上限、消息大小限制、有界队列 overload（→ C3）。
- 按 engine_id 的 retrieval/consolidation/optimizer/backlog/封禁截断观测（→ C12）。
- explicit schedules 迁移为 tenant/account/conversation owned durable work（→ C11）。
- 用户 MCP 开放前置全过（→ C7/UMCP）。

### M-P3（稳定性与备份）
- PostgreSQL 自动备份与恢复演练（→ C12 backup + C9 PITR）。
- inbox/turn/tool/outbox/delivery/schedule/provisioning/consolidation/optimizer 恢复契约（→ C2/C3/C11 + §6.1 B）。
- PersonaProfile/RelationshipState 当前值备份 + PITR + optimizer 单写者验证（→ C9）。
- backup manifest 执行（PG/workspace/blob/配置/secret；legacy SQLite 独立项） + attachment reconciliation（→ C12 + C6）。
- 进程崩溃自动重启（→ C3/C2）。
- Dashboard 全局总控台（缓存命中/Token/请求量/错误率/P50/P95/在线数/backlog + 按时间/账号/tenant/channel/model 下钻）（→ C12）。
- 维护窗口与升级回滚步骤；过期 auth session/已撤销 Token 敏感字段/过期 debug content 清理；dead-letter 重投/schedule misfire/provisioning retry/attachment 恢复演练（→ C12/C11/C6/C5）。
- **出口**：一次从备份恢复演练通过；总控台可聚合与下钻；能区分应用/DB/上游模型/账号滥用；无未解释数据丢失。

## 3. 跨阶段 change 的分段验收

以下 change 跨多个阶段，验收标准**按阶段拆段**，每段独立 `verified`（对应 task 文件内已分段的「验收标准」）：

| Change | 分段 | 本段所属里程碑 | 阶段验收要点 |
| --- | --- | --- | --- |
| C3 | C3-P0 段 | M-P0 | admission + bounded queue + overload + 串行 lane + lane owner 释放 |
| C3 | C3-P3 段 | M-P3 | 启动恢复扫描逐条解释 + `unknown`/`compensation_required` 语义演练 |
| C7 | C7-P0.5 段 | M-P0.5 | ToolExecutionContext/TenantToolCatalog/ToolPolicy 最小接缝（不公网） |
| C7 | C7-P1 段 | M-P1 | 普通 tenant 开放内置工具 allowlist + 越权负向 |
| C7 | C7-P2 段 | M-P2 | capability matrix + tool_call_id audit + §5.8.8 负向（UMCP 前置） |
| C8 | C8-P0 段 | M-P0 | lease coverage audit（全入口 + 旧 snapshot 不绕过 revocation） |
| C8 | C8-P2 段 | M-P2 | per-task context + hook failure gate + revocation gate 收尾 |
| C12 | C12-P-1 段 | M-P-1 | 采集规范/事件 schema/backup manifest 契约定义 |
| C12 | C12-P0 段 | M-P0 | persistence/backup manifest 基线 + 日志脱敏基线 |
| C12 | C12-P3 段 | M-P3 | 恢复演练 + 总控台聚合 |

> 里程碑「完成」判定：跨阶段 change 只需**本段所需部分** verified；C3/C7/C8/C12 的整体 `verified` 以最后一段完成为准。

## 4. P1GATE 检查点定义

- **性质**：派生检查点，**不是独立 change**（§5.9.10 D2：2 和 3 是公网 delivery/recovery 承诺的前置）。误当独立任务会导致进度误判。
- **通过条件**：C2（durable ingress/outbox/delivery 收敛为 PostgreSQL source of truth）+ C3（tenant admission + restart recovery 契约）同时满足。
- **位置**：M-P1 关闭前必须确认；开放公网 WebChat（带 C5 认证）的前提。
- **记录方式**：在 `PILOT_ROADMAP_PROJECT_CHECKLIST.md` / 里程碑评审中作为显式 gate 条目，不占 change 编号。

## 5. P4 触发信号清单（§6 P4）

以下任一**真实信号**出现，才考虑离开 Pilot：

1. 并发 WebSocket 长期超过单机可接受范围；
2. `asyncio.Queue` backlog 持续增长，或真实运行数据表明等待/恢复/丢失-重复风险影响体验；
3. interactive/maintenance backlog 或 tool/retrieval latency 持续超过 Pilot 可接受范围；
4. background maintenance 长期挤压 interactive，或任务丢失/重复副作用/恢复窗口不可接受；
5. 重启恢复窗口或任务丢失风险不可接受；
6. 需要多 Gateway/Worker 同时处理任务；
7. 用户规模明显超过 30 个受邀账号，或出现多个运营管理员。

**升级顺序固定为**（§6 P4）：① 先补齐指标和压测证据 → ② 保留 FastAPI 应用层认证和账号模型 → ③ 队列适配 Redis Streams consumer group → ④ 再评估 transactional outbox/DLQ/多 Worker → ⑤ 最后评估 Nginx/多 Gateway/容器编排。

## 6. 里程碑完成状态跟踪表

| 里程碑 | 关联 change（任务文件） | 状态（§8） | 出口条件满足？ |
| --- | --- | --- | --- |
| M-P-1 | task-01..task-14 的 design/spec 产出 | planned | 否 |
| M-P0 | task-01 / task-03 / task-08 / task-12 / task-13 | planned | 否 |
| M-P0.5 | task-01 / task-02 / task-03 / task-04 / task-07 | planned | 否 |
| M-P1 | task-05 / task-06 / task-07 / task-09 / task-10 / task-14 + P1GATE | planned | 否 |
| M-P2 | task-07 / task-08 / task-11 + UMCP | planned | 否 |
| M-P3 | task-03 / task-06 / task-09 / task-11 / task-12 | planned | 否 |
| M-P4 | 无 change（仅评估） | deferred | — |