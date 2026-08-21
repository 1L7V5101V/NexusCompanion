# NexusCompanion 5000 用户扩展计划

> 架构复审日期：2026-08-21
> 文档定位：本文件是扩展工作的架构级 source of truth；任务级拆分、实验记录和迁移命令放在 `docs/tasks/`。
> 当前结论：PostgreSQL + pgvector 方向成立，但现有 Phase 1 分支只完成了“后端兼容接入”的一部分，尚未形成可安全承载多租户与多 Worker 的生产架构。

---

## 0. 架构复审结论

原计划正确识别了 SQLite、单进程消息总线、WebChat 身份链和横向扩展问题，但对当前代码状态和分布式投递语义存在几处关键误判。本次复审做以下修正：

1. **“5000 用户”不是一个可直接压测的指标**。必须拆成注册身份数、在线连接数、活跃 turn 数、消息到达率、LLM 并发、数据量六个维度。
2. **AgentLoop 和主 LLM 客户端已经是 async**，真正的瓶颈不是“改成异步”，而是：
   - `AgentLoop.run()` 创建任务后立即 `await`；
   - `_passive_runtime_lock` 覆盖完整 turn，导致全局 single-flight；
   - SQLite 与 Phase 1 分支中的同步 psycopg 调用仍会阻塞事件循环。
3. **Phase 1 分支尚未完成多租户接线**。PostgreSQL schema 有 `tenant_id`，但工厂和业务调用默认使用 `tenant_id="default"`；这只能证明双后端兼容，不能证明 5000 用户隔离。
4. **Redis Pub/Sub 不能承载最终回复的可靠投递**。流式 delta 可以是易失事件，最终消息必须经 PostgreSQL outbox 或等价持久化通道投递。
5. **“双写后可随时切回 SQLite”不成立**。没有反向同步时，一旦 PostgreSQL 成为主写，回切 SQLite 会丢失新写入。迁移必须明确主数据源和不可逆点。
6. **Redis 缓存不是存储迁移的必要前置**。先以 PostgreSQL 基线压测证明瓶颈，再按命中率和失效成本决定是否缓存，避免在迁移期同时引入一致性问题。
7. **10 Worker、3 Gateway、64 GB PostgreSQL 不是当前可验证结论**。实例数应由队列延迟、单 Worker 安全并发、LLM 配额和数据库连接预算计算，而不是按用户数静态拍板。
8. **WebChat 不是纯前端补完**。它需要先完成身份派生、会话归属、附件归属、重连补偿和多 Worker 出站语义，之后才能暴露公网入口。

### 0.1 已核实的实现状态

| 领域               | `main` 当前事实                                                                | 本地分支事实                                                                      | 架构判断                                    |
| ---------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------- | --------------------------------------- |
| Passive turn     | `agent/looping/core.py` 已使用 asyncio，但完整 turn 被 `_passive_runtime_lock` 串行化 | Phase 1 分支沿用相同模型                                                            | 当前每进程实质只有 1 个 passive turn 在执行          |
| LLM transport    | `agent/provider.py` 复用 `AsyncOpenAI`；Responses transport 每次请求新建并关闭 client  | 未在 Phase 1 改动                                                               | 不需要重写一套 HTTP 客户端；需要统一连接生命周期、配额与重试策略     |
| 流式输出             | 已有 `StreamDeltaReady` 和部分 channel 流式消费能力                                   | 未形成跨进程协议                                                                    | 应演进现有事件信封，而不是另起一套 `AsyncAgentLoop`      |
| MessageBus       | `bus/queue.py` 使用进程内、无界 `asyncio.Queue`；出站重试一次后可能彻底丢失                      | 未改动                                                                         | 只能用于单进程；横向扩展前必须补持久队列、幂等和 DLQ            |
| Session 顺序       | `SessionManager` 只有进程内按 session 的写锁                                        | 未改动                                                                         | 多 Worker 下不足以保证同 session turn 顺序        |
| 存储               | `main` 仍是 SQLite                                                           | `feature/scaling-phase1-storage` 已完成 M0-M4：配置、接口契约、两个 PG store、工厂与部分接线      | 是迁移基础，不是生产完成态                           |
| PG 连接            | 无                                                                          | 两个 PG store 各持有同步 psycopg 单连接与 `threading.RLock`；配置中的 `pool_size` 未成为运行时连接池 | 并发开启前必须修复，否则只是把 SQLite 全局锁换成单连接锁        |
| 多租户              | channel/session 有身份线索，但没有统一 `TenantContext`                                | store 默认为 `tenant_id="default"`，调用点未传真实 tenant                              | 当前不能宣称租户隔离已落地                           |
| 向量检索             | SQLite/sqlite-vec                                                          | 每 tenant LIST 分区 + 分区 HNSW 已有合成数据验证                                         | 保留方向，但需 5000 分区、真实 1024 维、运行时 DDL 的生产验证 |
| WebChat          | `chat_api.py` 已有未鉴权端点；`web_chat_channel.py` 缺失                             | `worktree-webchat-jwt-rebuild` 没有领先 `main` 的实现提交                            | 视为未开始，不得按分支名计入进度                        |
| Proactive / jobs | 生命周期和调度均为进程内运行态                                                            | 未增加分布式 claim/leader lease                                                   | 多副本会重复执行主动任务和插件 job                     |
| 可观测性             | 有结构化阶段耗时日志，但无统一指标导出与跨链路 trace                                              | 未改动                                                                         | 必须前移到 Phase 0，而不是收尾工作                   |

### 0.2 保留与替换的架构决策

**保留：**

- PostgreSQL 作为会话、记忆、主动任务和交付状态的主数据源。
- pgvector 继续作为 5000 用户阶段的向量检索实现。
- SQLite 作为本地单用户/开发模式 adapter 保留。
- 流式 delta 与最终消息采用不同可靠性等级。
- WebChat Gateway 与 LLM Worker 最终可独立扩缩容。

**替换：**

- 将“Phase 2 异步改造”替换为“并发准入、阻塞隔离和 LLM 容量治理”。
- 将“Redis Queue + Pub/Sub”替换为“持久 ingress + inbox 去重 + PG outbox + 易失流式 fanout”。
- 将“Redis 缓存是 Phase 1 必做”替换为“以指标驱动的可选优化”。
- 将“按固定机器数部署”替换为“按负载模型和容量公式扩容”。
- 将“简单双写迁移”替换为“有主从角色、对账和不可逆点的迁移状态机”。

---

## 1. 容量目标与 SLO

### 1.1 先定义“5000 用户”

扩展验收不得再只写“模拟 5000 用户并发”。Phase 0 结束前必须确定以下输入：

| 维度           | 定义                                                | 本计划暂定口径                               |
| ------------ | ------------------------------------------------- | ------------------------------------- |
| 注册身份数        | 可在系统中拥有独立数据的 principal/tenant 数                   | 5000                                  |
| 日活身份数        | 24 小时内至少发起一次 turn 的身份数                            | 待产品数据确认                               |
| 在线连接数        | 同时维持的 WebSocket 数                                 | Channel-only 场景接近 0；WebChat 压测上限 5000 |
| 活跃 turn 数    | 同时等待 LLM、工具或存储的 turn 数                            | 由 LLM 配额反推，不等于在线人数                    |
| ingress rate | 每秒进入系统的新消息数                                       | 持续值与突发值分别定义                           |
| 数据量          | session messages、memory items、attachments 的总量与增长率 | 按真实样本推算，不只计算 embedding 原始字节           |

必须明确选择下面至少一个验收画像：

- **画像 A：Channel-first**：5000 个注册身份，以 Telegram/QQ/Feishu 为主，不假设 5000 条常驻连接。
- **画像 B：WebChat-online**：除 5000 个注册身份外，还要求最高 5000 条 WebSocket 同时在线。

画像 B 只增加 Gateway 连接容量，不代表系统需要同时执行 5000 个 LLM turn。

### 1.2 服务目标

以下目标用于架构验收，具体数值可在 Phase 0 基线后调整，但不得删除指标维度：

| 指标      | 目标                                                               |
| ------- | ---------------------------------------------------------------- |
| 入站接收    | 已通过鉴权的消息在 P95 250 ms 内得到 accepted/queued 确认                      |
| 排队延迟    | 在设计负载内，P95 queue wait 不超过 2 s；超载时明确返回 busy/retry-after           |
| 首帧延迟    | P95 不超过“上游模型首 token 延迟 + 1 s 系统开销”                               |
| 最终消息可靠性 | 已持久化完成的回复不得静默丢失；投递失败进入可重试状态或 DLQ                                 |
| 会话顺序    | 同一 session 的 turn 按确定顺序执行，不出现历史覆盖或 seq 冲突                        |
| 隔离      | 所有读取、写入、搜索、附件访问都必须由服务端派生 tenant/principal 范围                     |
| 可恢复性    | PostgreSQL RPO 不高于 5 分钟、RTO 不高于 30 分钟；最终值按部署方案确认                 |
| 可观测性    | 任一 turn 可通过 `turn_id` 串起 ingress、queue、worker、LLM、store、delivery |

原计划中的“包含 LLM 调用 P95 小于 3 秒”删除。该目标无法脱离模型、上下文长度、工具链和供应商排队独立承诺。

---

## 2. 不可破坏的架构约束

### 2.1 身份与数据范围

每个请求必须携带由服务端生成或验证的上下文：

```text
TenantContext
  tenant_id       数据隔离域
  principal_id    已认证用户或机器人身份
  channel         telegram / qq / feishu / webchat / cli
  channel_user_id 平台侧不可伪造身份
  session_key     服务端派生的会话键
```

约束：

- WebChat 前端不得直接决定 `tenant_id`、`principal_id` 或最终 `session_key`。
- `tenant_id="default"` 只允许出现在显式 single-user 模式和测试中。
- store 不应为每个 tenant 新建独立网络连接；连接池属于进程级 `StorageRuntime`，tenant 范围属于请求级上下文。
- dashboard、检索、批量删除、undo、附件读取与主动任务都必须经过同一租户范围校验。

### 2.2 消息语义

统一标识：

| 标识            | 用途                            |
| ------------- | ----------------------------- |
| `inbound_id`  | channel 重试时去重，同一外部消息稳定不变      |
| `turn_id`     | 一次 agent 执行，贯穿日志、事件、LLM 和工具调用 |
| `message_id`  | 持久化会话消息标识                     |
| `delivery_id` | 一次最终出站交付及其重试状态                |
| `stream_seq`  | 单 turn 流式事件的单调序号              |

投递承诺：

- ingress 与最终 delivery 采用 **at-least-once + 幂等**，不宣称 exactly-once。
- 同一个 `inbound_id` 只能产生一个逻辑 turn；重复消费必须返回已有结果或安全跳过。
- 最终 assistant message 先持久化，再产生 outbox 记录；channel 发送成功后才标记 delivered。
- `StreamDeltaReady`/WebSocket token delta 可以丢失；重连后以数据库中的最终消息为准。

### 2.3 并发与背压

- 同 session 串行，不同 session 可并行。
- 每进程必须有 `max_inflight_turns`，每 provider/model 必须有独立并发、RPM、TPM 和预算限制。
- ingress queue、outbound queue、工具并发和上传大小都必须有界。
- 超载时拒绝或排队，不能无限创建 task、无限堆积 `asyncio.Queue` 或无限占用数据库连接。
- cancellation、timeout 和 retry 必须区分“未产生副作用”和“副作用可能已提交”。

### 2.4 主数据源与缓存

- PostgreSQL 是业务真相；Redis cache 可随时清空并从 PostgreSQL 重建。
- Redis Pub/Sub 只用于易失实时通知，不作为完成态或最终回复的唯一记录。
- 本地文件只允许服务单机开发模式；多 Worker 模式的附件和可下载产物必须进入共享 BlobStore。
- cache key 必须包含 tenant、数据版本和查询版本；写入后通过版本递增或显式失效避免跨租户/脏读。

### 2.5 多副本副作用

以下进程内能力在多副本前必须增加分布式 ownership：

- proactive tick；
- scheduler job；
- plugin job；
- consolidation/background memory job；
- 消息 delivery retry；
- tenant 分区 provisioning；
- plugin/runtime snapshot 发布。

每类任务必须采用 leader lease、数据库 claim 或 queue consumer group 中的一种，不得依赖“生产只会启动一个实例”的隐含假设。

---

## 3. 目标拓扑

```text
Channel / WebChat Client
          |
          v
Gateway / Channel Adapter
  auth + TenantResolver + ingress limit
          |
          v
Durable Ingress Stream
  consumer group + pending reclaim + DLQ
          |
          v
Turn Worker Pool
  TurnAdmission + AgentCore + ModelGateway + Tool runtime
          |
          +----------------------+
          |                      |
          v                      v
PostgreSQL                  Ephemeral Stream Fanout
sessions / memory / inbox   token/thinking/progress delta
outbox / delivery / jobs             |
          |                           v
          v                    WebSocket Gateway
Delivery Dispatcher
Telegram / QQ / Feishu / WebChat final event

Redis Cache：可选旁路，只缓存可重建数据。
BlobStore：多 Worker 时保存附件与生成文件。
```

### 3.1 持久 ingress

5000 用户阶段默认采用 Redis Streams consumer group；如已有托管消息系统，可提供 Kafka/NATS JetStream adapter，但不把替换 broker 作为当前前置工作。

接口必须隐藏 broker 细节：

```text
IngressQueue.publish(envelope)
IngressQueue.consume(consumer)
IngressQueue.ack(message)
IngressQueue.retry_or_dead_letter(message, error)
```

开发和测试使用 in-memory adapter，生产使用 durable adapter。队列 envelope 至少包含 `inbound_id`、`turn_id`、`TenantContext`、channel metadata、时间戳和 schema version。

### 3.2 inbox 与 session affinity

Worker 消费后先以 `inbound_id` 写入 inbox/claim 状态：

1. 新消息：取得处理权。
2. 已完成：返回已存在结果，不重复执行工具。
3. 处理中且 lease 未过期：暂不并发执行。
4. lease 过期：允许重新 claim，并通过幂等记录恢复。

同 session 的消息必须路由到同一逻辑分片，或在执行前取得数据库/Redis session lease。只使用进程内 `asyncio.Lock` 不足以支持多 Worker。

### 3.3 transactional outbox

完成 turn 时，在同一数据库事务中：

1. 写 assistant message；
2. 更新 turn 状态；
3. 插入 outbox delivery 记录。

Delivery Dispatcher 独立 claim outbox 记录并发送。发送 API 超时属于“结果未知”，重试必须使用 channel 支持的幂等键或本地 delivery 去重策略。超过阈值进入 DLQ，并在 dashboard 可见。

### 3.4 流式事件

沿用现有 `StreamDeltaReady`，补充稳定信封：

```text
turn_id, session_key, tenant_id, stream_seq, kind, delta, created_at
```

- `kind` 至少区分 `thinking`、`content`、`tool_status`、`done`、`error`。
- Gateway 丢失 delta 时不回放 token；`done` 后从 REST/数据库拉最终消息。
- Telegram/QQ/Feishu adapter 可聚合 delta 或节流 edit，不强迫所有 channel 逐 token 发送。

---

## 4. 关键 module 与 seam

本计划按 deep module 设计，避免让 broker、数据库和租户规则散落到业务调用点。

### 4.1 `TenantResolver`

**Interface：** 从可信 channel identity 或 WebChat credential 生成 `TenantContext`。
**隐藏实现：** JWT/session token、channel allowlist、账号映射、session_key 规则、吊销和审计。

### 4.2 `StorageRuntime`

**Interface：** 共享连接资源，并通过 `for_tenant(context)` 返回受限的 session/memory/job repository。
**Adapters：** SQLite single-user、PostgreSQL multi-tenant。
**禁止：** 在业务代码中使用 `MemoryStore2 | PostgresMemoryStore` 联合类型并逐步扩散 backend 判断。

Phase 1 可暂时保留现有 parity interface 作为迁移 adapter，但生产接线应收敛为：

```text
StorageRuntime
  for_tenant(TenantContext) -> TenantStorage
    sessions
    memory
    jobs
    inbox
    outbox
```

### 4.3 `TurnAdmission`

**Interface：** `run(envelope, handler)`。
**隐藏实现：** 全局 semaphore、per-session 顺序、per-tenant 配额、取消、超时、队列等待指标和 runtime snapshot lease。

它替换当前覆盖整条 passive path 的全局 `_passive_runtime_lock`，但仍需保留插件热重载的安全 quiesce 语义。

### 4.4 `ModelGateway`

**Interface：** 以 provider/model/profile 发起模型调用并返回统一 usage/error。
**隐藏实现：** AsyncOpenAI/client 生命周期、连接池、RPM/TPM/concurrency、`Retry-After`、指数退避、fallback、熔断、预算和 usage 计量。

不要在 AgentLoop、memory gate、query rewrite、proactive 各自实现一套限流器。

### 4.5 `DeliveryDispatcher`

**Interface：** claim、deliver、retry、dead-letter。
**隐藏实现：** channel adapter、发送幂等、退避、最终状态和告警。

当前 `MessageBus._send_outbound()` 中“重试一次后日志记录并丢失”的行为必须由这个 module 取代。

### 4.6 `RuntimeCoordinator`

**Interface：** 发布 runtime generation、取得 turn lease、选举 singleton job owner。
**隐藏实现：** 现有进程内 snapshot lease + 多副本版本协调。

多 Worker 初期可采用“部署版本不可热混用”的简单约束：滚动发布期间，envelope 记录 `runtime_version`，旧 Worker drain 后再切新版本。不要在第一版实现跨版本任意热切换。

---

## 5. 分阶段实施计划

### Phase 0：负载模型、可观测性与安全护栏（P0）

**状态：未完成，可立即与 Phase 1 并行。**

#### 工作项

- 确定画像 A/B 和持续/突发 ingress、LLM 并发、数据增长假设。
- 为 inbound、turn、LLM call、tool call、storage query、outbox delivery 统一生成并传播 `turn_id`。
- 导出 Prometheus/OpenTelemetry 等价指标；保留现有结构化耗时日志。
- 建立单机基线：turn throughput、event-loop lag、SQLite/PG query latency、内存和 LLM usage。
- 建立可重复负载工具，区分：
  - 不调用真实 LLM 的系统容量测试；
  - stub LLM 固定延迟测试；
  - 小流量真实 provider 验证。
- 给 `chat_api.py` 增加启动护栏：未完成鉴权时禁止监听公网地址。
- 记录 PostgreSQL 备份、PITR、恢复演练和 schema migration 流程。

#### Phase 0 Exit gate

- dashboard 能看到 queue depth/lag、inflight turns、event-loop lag、LLM latency/error、DB pool、delivery backlog。
- 任一失败 turn 可按 `turn_id` 定位到失败阶段。
- 负载脚本、数据集和结果进入仓库；不再用手工并发或主观体感验收。
- 画像 A/B 与目标值经确认并写回本文件。

---

### Phase 1：PostgreSQL + pgvector 存储基础（P0）

**状态：`feature/scaling-phase1-storage` 已完成 M0-M4；M4H-1/M4H-2 已完成，M4H-3 尚未开始，仍未达到 M4.5 merge/cutover gate。**
**旧分支：`feature/pg-migration` 已被该分支继承，视为 superseded。**

详细任务记录位于该分支中的 `docs/tasks/phase1-storage/phase1-storage.md`；合并前该文件不在 `main`。

#### 1.1 已完成的分支工作

- M0：依赖、Alembic、本地 PG、`[storage]`/`[cache]` 配置。
- M1：MemoryStore2 / SessionStore 接口契约。
- M2：PostgresMemoryStore、pgvector、tenant LIST 分区、HNSW、parity 基础。现有合成实验中，全局 HNSW 加 tenant filter 的小租户 recall 约为 0.180，按 tenant 分区后约为 0.990；该结果支持分区方向，但不替代真实生产基准。
- M3：PostgresSessionStore、`pg_trgm` 搜索与接口补齐。
- M4：工厂与主要构造点接线、undo 语义收口。
- M4H-1：storage Protocol/interface、factory/holder 收口和 SQLite/PG 共同契约测试。
- M4H-2：`TenantContext` / `TenantResolver` 全链路接线，以及 A/B tenant isolation、dashboard、undo 和 source_ref 越权测试。

这些工作证明了“SQLite 与 PostgreSQL 可以实现相近业务语义”，但 M4H-3 尚未开始；当前 adapter 的连接/阻塞模型仍未达到并发与生产 merge gate。

#### 1.2 merge 前必须补齐

##### A. 多租户运行时接线（M4H-2 已完成）

- `TenantContext` / `TenantResolver` 已从 inbound 贯穿到 session、memory、dashboard、proactive、undo/source_ref 和附件 metadata。
- 多用户路径的隐式 `tenant_id="default"` 已移除，并有 A/B tenant 越权测试。
- 后续连接池实现必须保持进程级共享；禁止按 tenant 创建长期 connection/store 单例。

##### B. 连接与事件循环模型（M4H-3 尚未开始）

当前 `PostgresMemoryStore` / `PostgresSessionStore` 使用同步 psycopg 单连接。M4H-3 必须先形成 ADR 级决策，再实现以下两条路线之一：

1. **推荐目标**：psycopg async/SQLAlchemy async + 进程级 pool；或
2. **过渡方案**：同步 pool + 有界 thread executor，将所有 DB 调用移出 event loop。

无论哪种方案，都必须满足：

- pool 配置真实生效；
- `worker_count × pool_max` 不超过数据库连接预算；
- 事务失败后连接可恢复，不留下 aborted transaction；
- shutdown 显式关闭 pool，不依赖 `__del__`；
- 不共享一个 cursor/connection 跨并发请求。

##### C. 分区生产验证

当前每 tenant 一个 LIST 分区，并在首次写入时动态 DDL。合成 recall 结果不足以证明生产可用，必须验证：

- 5000 分区下的 planning latency、catalog 大小、Alembic migration 时间、备份/恢复时间；
- 真实 1024 维 embedding、真实 tenant 大小分布下的 recall/P95；
- autovacuum、REINDEX、空 tenant 回收；
- 首次写入不在用户 hot path 执行 DDL，改为 tenant provisioning；
- 分区名使用稳定 hash/ID，避免简单字符替换导致命名碰撞；
- HNSW 参数和小分区 seq scan 的切换阈值。

若 5000 LIST 分区未通过 gate，再评估固定 hash bucket、冷热 tenant 分层或仅为大 tenant 建专属分区；当前 LIST partition 仅是 Phase 1 的 provisional 方案，不因已有实现而固化。

##### D. 接口与测试

- M4H-1 已使用 Protocol/明确 interface 收敛 `MemoryStore2 | PostgresMemoryStore` 联合类型。
- 使用 Protocol/明确 interface 收敛其余具体实现联合类型。
- 全量测试与 pyright 通过；不能只以局部 58 passed 作为 merge 依据。
- 保留 SQLite adapter 的契约测试与 PostgreSQL parity 测试。
- M7 单列 BM25/关键词搜索对等测试，明确允许的排序差异。

#### 1.3 当前数据平面边界

M4H-2 已完成 tenant 运行时接线，但当前 PostgreSQL adapter 仍不承接 `SessionManager` 的 turn control plane 持久化；该 SQLite-only 边界必须在 M6 cutover 前明确纳入范围，或记录双存储期间的一致性、恢复和回滚策略。不能将“PostgreSQL 成为 primary”笼统解释为所有 session/turn 数据已经迁移。

#### 1.4 迁移与切换

M5-M7 调整为：

- **M5：迁移工具与校验**：批量 COPY、断点续传、行数/哈希/抽样语义校验。
- **M6：主数据源切换**：按第 8 节迁移状态机执行，不采用无主次的简单双写。
- **M7：生产基准与对等**：真实向量、BM25、5000 分区、连接池、故障恢复。
- **Cache：条件任务**：只有 PG 基线显示重复读是瓶颈且可定义可靠失效时才启用。

#### Phase 1 Exit gate

- PostgreSQL 成为 staging 主数据源，SQLite parity 测试持续通过。
- tenant 由请求上下文派生，越权测试覆盖所有存储入口。
- DB 调用不阻塞 event loop，连接池有预算和指标。
- 数据导入、对账、PITR 恢复和回滚演练完成。
- 向量 recall、搜索语义、5000 tenant 分区运维均达到记录的阈值。

---

### Phase 2：单进程并发、阻塞隔离与 LLM 容量治理（P0）

**状态：未开始；必须在 Phase 1 的 store interface 稳定后完成。**

#### 2.1 TurnAdmission

- 将 `AgentLoop.run()` 改为消费后提交到有界 task set，而不是创建后立即等待。
- 用 per-session admission 保证同 session 串行，不同 session 并行。
- 用全局 semaphore 限制每进程 inflight turn。
- `_active_tasks` 与 `_active_turn_states` 改以 `turn_id` 为主键，并维护 session 到 turn 的索引，避免并发时覆盖。
- 将 `_passive_runtime_lock` 的职责拆成：
  - runtime snapshot lease；
  - reload quiesce gate；
  - turn 并发 admission。
- 明确中断、续跑、spawn completion、proactive 与 passive 的优先级和互斥规则。

#### 2.2 阻塞操作清单

逐项确认是否会阻塞 event loop：

- PostgreSQL/SQLite 调用；
- markdown 文件读写与 consolidation commit；
- embedding 请求；
- MCP subprocess I/O；
- 插件同步 hook；
- 图片/附件处理；
- dashboard 大查询。

同步实现要么迁移为 async adapter，要么进入有界 executor，并暴露 queue time 指标。

#### 2.3 ModelGateway

- 复用长生命周期 async client；修复 Responses transport 每请求新建 client 的连接复用问题。
- 限制维度至少包含 provider、account/key、model、request concurrency、RPM、TPM。
- 读取 `Retry-After`；重试使用 exponential backoff + jitter。
- 对 401、quota、context、safety、timeout、5xx 分别处理，避免盲目重试。
- 加入 provider fallback 与 circuit breaker，但工具副作用发生后不得自动重放整 turn。
- 记录 prompt/completion/cache token 与估算成本，支持 tenant 预算和全局熔断。

#### 2.4 流式协议定型

- 在现有 `StreamDeltaReady` 上增加 `turn_id`、`stream_seq`、稳定 `kind`。
- 最终 `OutboundMessage` 与流式 delta 解耦。
- channel adapter 定义聚合、节流 edit、最大消息长度和 done/error 行为。
- 不在本阶段引入跨进程 broker，但协议必须可序列化、版本化。

#### Phase 2 Exit gate

- 单进程可并发处理不同 session，且同 session 顺序测试稳定。
- event-loop lag 在目标负载下不因 DB/文件调用显著抖动。
- 限流后不出现 provider 429 风暴，queue wait 与拒绝行为可观测。
- cancellation、timeout、plugin reload 和中断回归测试通过。
- 在 stub LLM 固定延迟下，吞吐随 `max_inflight_turns` 增长到预期瓶颈，而不是始终 single-flight。

---

### Phase 3：持久消息、幂等与水平 Worker（P0）

**状态：未开始；依赖 Phase 1 与 Phase 2。**

#### 3.1 IngressQueue + inbox

- 实现 in-memory 与 Redis Streams 两个 adapter。
- Gateway 入队前完成 auth、tenant 派生、payload 限制和 per-principal rate limit。
- Worker 使用 consumer group，支持 pending reclaim、visibility timeout、retry count 和 DLQ。
- PostgreSQL inbox 以 `inbound_id` 唯一约束实现逻辑去重。
- envelope 带 schema version；滚动升级期间允许兼容前一版本。

#### 3.2 session 顺序

优先采用按 `session_key` 一致性 hash 到固定逻辑 shard；worker 扩缩容期间再配合短 lease 防止双执行。若直接使用共享 consumer group，则必须先取得 session lease。

不要用全局分布式锁串行所有用户，也不要只依赖“同一消息通常落在同一 Worker”。

#### 3.3 Outbox + delivery

- 在 PG 建立 turn、outbox、delivery attempt 状态。
- final message 与 outbox 同事务提交。
- delivery worker 独立扩缩容，按 channel 限流。
- 失败进入 retry/DLQ，dashboard 支持查看和人工重放。
- Redis Pub/Sub 只分发实时 delta 或“有新最终消息”的提示。

#### 3.4 多副本运行时

- proactive、scheduler、plugin jobs 使用 leader lease 或数据库 claim。
- consolidation/background memory job 具备幂等 source_ref 和可恢复 claim。
- plugin/runtime snapshot 以 deployment/runtime version 固定；旧 Worker drain 后退出。
- MCP server 进程数量纳入 Worker 容量；重型 MCP 可拆成远程共享服务，但不提前全部微服务化。

#### 3.5 共享文件

- 引入 `BlobStore` interface：local adapter 用于开发，共享对象存储 adapter 用于多 Worker。
- 数据库只保存 object key、tenant、owner、mime、size、hash、retention 等 metadata。
- 下载使用授权后的短期 URL 或受控流式代理，禁止接受客户端绝对路径。

#### Phase 3 Exit gate

- 2 个以上 Worker 下，同 session 顺序、inbound 去重、工具副作用幂等测试通过。
- 随机 kill Worker 后 pending 消息被 reclaim，不丢 final reply。
- Redis Pub/Sub 中断不影响最终消息恢复。
- proactive/job 在多副本下每个调度窗口只执行一次或按幂等规则安全重复。
- outbox backlog、oldest age、DLQ 和 delivery success rate 可观测。

---

### Phase 4：WebChat 身份、安全与 Gateway（P0，仅启用 WebChat 时）

**状态：未开始。`worktree-webchat-jwt-rebuild` 当前没有可计入的领先实现。**

#### 4.1 身份与授权

- 登录/签发 credential 后由 `TenantResolver` 生成 principal 与 tenant。
- WebSocket 握手、REST sessions/messages、upload、media 全部要求认证。
- 所有资源查询同时校验 tenant 与 owner/session scope。
- token 支持过期、轮换、吊销；日志不得记录原始 token。
- `host != 127.0.0.1` 且 auth 未启用时启动失败。
- 明确 CORS、CSRF、CSP、WebSocket Origin 和反向代理信任头策略。

#### 4.2 Gateway 行为

- Gateway 只持有连接态，不执行 AgentCore 或直接访问内部 store 细节。
- inbound 先限流再入 durable queue。
- streaming delta 经易失 fanout；final message 通过 REST/持久状态补齐。
- 客户端携带 last seen message/stream sequence，重连后拉取缺失最终消息。
- 慢客户端有单连接发送缓冲上限；超过阈值丢弃 delta 或断开，不能拖垮 Worker。

#### 4.3 连接容量

- 5000 WebSocket 压测单独进行，不与 5000 LLM turn 混为一个测试。
- 验证 fd、内存、ping/pong、反代 idle timeout、滚动升级 drain、重连风暴。
- `max_connections`、每连接缓冲和 heartbeat 使用独立 WebChat 配置，不复用 IPC server 的限制。
- Gateway 实例数由每实例实测安全连接数与 N+1 冗余计算。

#### 4.4 前端

- 支持 queued/running/tool/done/error 状态，不只显示 token。
- 以 `turn_id` 去重，避免重连后重复插入消息。
- 上传前后均做 size/mime 校验；展示层不信任模型生成 HTML。
- 管理端与普通用户端权限分离，不复用无范围 dashboard endpoint。

#### Phase 4 Exit gate

- A/B 用户越权矩阵全部返回 403/404，且无 timing/path 泄漏。
- 5000 WS 连接测试通过，断开和滚动发布后可自动恢复。
- 10 分钟 Pub/Sub 故障或 Gateway 重启不丢最终消息。
- slow consumer、重连风暴、上传滥用和 token 吊销测试通过。

---

### Phase 5：生产部署、弹性与故障恢复（P1）

**状态：未开始；不把 Kubernetes 作为前置条件。**

#### 5.1 部署演进

1. **单实例 + PostgreSQL**：验证存储迁移和单进程并发。
2. **单 Gateway + 多 Worker**：验证 durable queue、session 顺序和 outbox。
3. **多 Gateway + 多 Worker**：仅画像 B 或可用性目标需要时启用。
4. **容器编排**：当滚动发布、自动伸缩和故障迁移的运维收益高于复杂度时，再选择 Kubernetes/ECS 等平台。

#### 5.2 资源隔离

生产环境不建议让一个 Redis 实例同时无配额地承担 cache、durable stream 和 Pub/Sub：

- 开发环境可以共用；
- 生产至少使用独立 logical deployment/资源配额区分 queue 与 cache；
- cache 淘汰不得影响 stream pending 数据；
- 为 stream 设置持久化、备份和内存上限策略。

PostgreSQL 前可使用 PgBouncer，但 transaction pooling 与 session-level 特性必须验证。总连接预算：

```text
sum(worker_replicas × worker_pool_max)
+ gateway_pool
+ migration/admin reserve
<= database_max_connections × 70%
```

原计划的“10 Worker × 每 Worker 50 连接”会直接形成 500 个应用连接，不得作为默认值。

#### 5.3 自动伸缩信号

Worker 扩缩容优先依据：

- ingress queue oldest age；
- ready message count；
- inflight turns；
- provider concurrency utilization；
- event-loop lag；
- DB pool wait。

Gateway 扩缩容依据 active connections、send buffer、event-loop lag 和 reconnect rate。CPU 只能作为辅助指标。

#### 5.4 故障演练

- Worker kill / network partition / Redis restart；
- PostgreSQL failover 与 PITR 恢复；
- provider 429、5xx、长时间无 token；
- channel API 超时且实际可能已发送；
- Gateway 滚动升级与重连风暴；
- plugin reload 失败；
- queue poison message 与 DLQ replay。

#### Phase 5 Exit gate

- 在 N+1 故障下满足已定义 SLO。
- 部署、迁移、回滚和 DLQ replay 有 runbook，并至少演练一次。
- 容量提升可通过增加 Worker/Gateway 实例获得，且不被数据库连接或 provider 配额反向拖垮。

---

### Phase 6：指标驱动的优化（P2，持续）

以下项目只有在指标证明必要时实施：

- Redis session/profile/search cache；
- embedding dedup/cache；
- read replica；
- memory cold archive；
- 更复杂的 pgvector 分层索引；
- 独立向量数据库；
- Kafka/NATS 等更重 broker；
- 多区域 active-active。

“已有技术选型”不是实施理由。每个优化必须记录瓶颈、基线、预期收益、回滚方式和实际结果。

---

## 6. 阶段依赖与并行规则

| 工作                        | 可立即开始       | 依赖                            | 说明                                |
| ------------------------- | ----------- | ----------------------------- | --------------------------------- |
| Phase 0 指标与负载工具           | 是           | 无                             | 所有后续 gate 的基础                     |
| Phase 1 M5-M7             | 否           | M4.5 Exit gate                | M4.5 通过后依次执行 M5 迁移工具、M6 主数据源切换、M7 生产基准 |
| TenantResolver 设计与越权测试    | 是           | 身份模型确认                        | 与存储分支协调 interface，避免再次默认 tenant   |
| WebChat 前端静态交互            | 可部分开始       | 稳定事件状态模型                      | 不得把未定的身份/session_key 写死           |
| Phase 2 TurnAdmission     | 设计可开始，接线后合并 | Phase 1 store interface 稳定    | 会修改 `agent/looping/core.py` 和调用路径 |
| Phase 3 durable messaging | 协议设计可开始     | Phase 2 event envelope        | worker 化依赖并发与幂等语义                 |
| Phase 4 公网 WebChat        | 否           | Phase 3 final delivery + auth | 未满足前只允许本地开发                       |
| Phase 5 多副本生产             | 否           | Phase 3                       | 先证明两 Worker 正确，再扩数量               |

### 本地分支整合规则

- `feature/scaling-phase1-storage` 是唯一有效的 Phase 1 工作分支。
- `feature/pg-migration` 不再独立推进，避免两套 PG 实现漂移。
- `worktree-webchat-jwt-rebuild` 当前不含领先 `main` 的功能提交；若重启工作，应从最新 `main` 新建明确 scope 的分支。
- Phase 1 合并前先同步最新 `main`，运行全量测试、pyright 和 migration/parity 测试，确认没有测试覆盖回退。
- [`architecture_comparison.md`](./architecture_comparison.md) 与 [`migration_checklist.md`](./migration_checklist.md) 仍包含旧的“同步 AgentLoop/重建异步客户端”等表述；在它们完成同步前，以本文件和已验证代码为准。
- 不在 SCALING_PLAN 中用“分支存在”代表“Phase 已完成”；完成状态以 exit gate 和可复现证据为准。

---

## 7. 容量与成本模型

### 7.1 Worker 数量

```text
worker_count = ceil(target_active_turns / measured_safe_turns_per_worker) × headroom
```

其中 `measured_safe_turns_per_worker` 必须在 stub LLM 与真实 provider 两种测试中给出。通常 provider 配额会先于 CPU 成为瓶颈。

### 7.2 PostgreSQL

容量至少包含：

- message/session 行与索引；
- 记忆正文和 metadata；
- embedding 原始数据；
- HNSW 索引、WAL、临时空间和 vacuum headroom；
- inbox/outbox/delivery/job 状态；
- 备份与 PITR 存储。

`5000 × 1000 × 1024 × 4 bytes` 只得到约 20 GB 向量原始值，不等于实际数据库内存需求，也不代表所有向量必须常驻内存。实际规划使用 `pg_total_relation_size`、索引大小、buffer hit 和真实查询延迟。

### 7.3 LLM 成本

月成本必须包含：

```text
input_tokens × input_price
+ output_tokens × output_price
+ embedding_tokens × embedding_price
+ retry/fallback overhead
+ background/proactive/memory consolidation calls
```

基础设施成本通常不是 AI companion 的唯一主要成本。原计划固定的 `$1900/月` 删除，改为按真实 token/turn 分布形成 low/base/high 三档预算，并设置 tenant/global budget alarm。

### 7.4 WebSocket

单连接 20-50 KB、单机数千连接等数字只能作为初始假设。实际容量由框架、TLS、反代缓冲、订阅状态和慢客户端行为决定，必须通过 heap/RSS/fd/event-loop lag 实测。

---

## 8. 迁移状态机与回滚

### S0：SQLite primary

- 生产只写 SQLite。
- PG schema、导入和 parity 在 staging 验证。
- 可无损回滚到当前版本。

### S1：Snapshot import + shadow validation

- 维护窗口或一致性快照导入 PG。
- 对比行数、主键集合、抽样内容、向量维度、搜索结果和 session seq。
- 生产仍以 SQLite 为 primary。

### S2：PostgreSQL primary，保留审计窗口

- 新写入只以 PG 成功为准。
- 如需要保留 SQLite shadow，必须明确它只是审计副本，并记录 shadow 写失败；不得把两个库都当 primary。
- 读取可做少量 shadow compare，但响应只取 PG。
- 回滚应用版本时仍连接 PG；不能直接切回旧 SQLite 数据。

### S3：PostgreSQL stable

- 完成至少一个业务周期的对账、备份恢复和性能观察。
- 停止 shadow 流程，SQLite 归档为只读快照。

### S4：旧实现清理

- 只有在 S3 稳定且本地 single-user adapter 的保留策略明确后，才删除不再需要的迁移代码。
- SQLite adapter 是否保留由产品模式决定，不与生产主数据源切换绑定。

### 回滚原则

- schema migration 必须优先 forward-fix；破坏性 migration 采用 expand/contract。
- S0/S1 可回到 SQLite primary。
- S2 之后若无 PG -> SQLite 反向同步，禁止声称可无损回切 SQLite。
- 队列、outbox 和 delivery schema 必须支持旧 Worker drain，避免滚动发布时消息版本不兼容。

---

## 9. 可观测性清单

### 核心指标

- `ingress_received_total`、`ingress_rejected_total`、`ingress_duplicate_total`
- `ingress_queue_depth`、`ingress_oldest_age_seconds`、`queue_retry_total`、`dlq_total`
- `turn_inflight`、`turn_queue_wait_seconds`、`turn_duration_seconds`、`turn_cancelled_total`
- `event_loop_lag_seconds`、executor queue depth
- `llm_requests_total`、`llm_latency_seconds`、`llm_ttfb_seconds`、429/5xx/timeout、tokens/cost
- `db_pool_in_use`、`db_pool_wait_seconds`、query latency、transaction rollback
- vector recall benchmark、HNSW/seq scan plan ratio、partition planning latency
- `outbox_pending`、`outbox_oldest_age_seconds`、`delivery_attempt_total`、`delivery_success_total`
- `ws_connections`、send buffer、reconnect rate、slow consumer disconnect
- proactive/job claim、duplicate execution、lease expiry

### 日志字段

每条关键日志至少包含：

```text
turn_id, inbound_id, tenant_id, principal_id, session_key,
worker_id, runtime_version, provider, model, attempt, duration_ms
```

敏感字段、token、完整 prompt 和用户附件路径不得默认进入日志。

### 告警

- oldest queue age 超过 SLO；
- final outbox backlog 持续增长；
- tenant isolation violation；
- provider 429/5xx 突增；
- DB pool wait 或连接数接近预算；
- event-loop lag 持续超阈值；
- proactive/job duplicate claim；
- backup/PITR 失败或超过 RPO。

---

## 10. 风险登记

| 风险                          | 等级  | 当前证据                              | 缓解                                                   |
| --------------------------- | --- | --------------------------------- | ---------------------------------------------------- |
| 所有 PG 数据落入 `default` tenant | P0  | Phase 1 工厂与调用点未传真实 tenant         | TenantResolver + request-scoped TenantStorage + 越权测试 |
| 开启并发后同步 PG 阻塞 event loop    | P0  | PG store 使用同步单连接和 RLock           | async pool 或有界 executor；并发前完成                        |
| 全局 passive lock 导致吞吐为 1     | P0  | `_passive_runtime_lock` 覆盖完整 turn | TurnAdmission + snapshot lease 拆责                    |
| 多 Worker 重复执行同 session/tool | P0  | 只有进程内 queue/lock/state            | inbox、session affinity/lease、工具幂等                    |
| final reply 静默丢失            | P0  | 当前出站重试一次后只记日志                     | transactional outbox + delivery DLQ                  |
| WebChat 越权与任意文件读取           | P0  | endpoint 无 auth，media 只做路径范围检查    | 服务端身份派生、owner scope、BlobStore                        |
| 多副本重复 proactive/job         | P0  | 调度状态为进程内                          | leader lease/DB claim                                |
| 5000 LIST 分区运维退化            | P1  | 只完成合成 recall 验证                   | 5000 分区基准；必要时改分区策略                                   |
| 首次请求动态 CREATE PARTITION     | P1  | store 在首次写入确保分区                   | provisioning control plane + 幂等 DDL worker           |
| PG 连接爆炸                     | P1  | 原计划按 Worker 配 50 连接               | 全局连接预算 + PgBouncer/小 pool                            |
| Redis cache 引入脏读            | P1  | 原计划计划缓存 session/profile/search    | 指标驱动启用、版本化 key、可清空                                   |
| 本地附件在多 Worker 不可见           | P1  | chat media 基于本地路径                 | BlobStore adapter + metadata authorization           |
| LLM 费用先于计算资源失控              | P1  | 无统一 token budget/成本 gate          | ModelGateway usage、tenant/global budget              |
| plugin 版本在滚动发布中不一致          | P1  | snapshot lease 仅进程内               | runtime_version + drain 策略                           |

---

## 11. 上线验收总清单

### 架构与数据

- [ ] 已选择画像 A 或 B，并记录持续/突发负载。
- [ ] `TenantContext` 贯穿所有读写路径，无多用户默认 tenant。
- [ ] 同 session 顺序、多 session 并发通过测试。
- [ ] inbox、outbox、delivery、DLQ schema 和运维入口存在。
- [ ] PostgreSQL migration、PITR 和 restore 演练通过。
- [ ] 真实 1024 维向量与 5000 tenant 分区基准通过。
- [ ] 总数据库连接数满足预算，连接等待可观测。

### 可靠性

- [ ] kill Worker 后消息可 reclaim，工具副作用不重复。
- [ ] Redis Pub/Sub 故障不丢最终回复。
- [ ] channel 超时/重试不会无界重复发送。
- [ ] proactive、scheduler、plugin job 多副本不重复执行。
- [ ] queue poison message 可进入 DLQ 并安全 replay。
- [ ] 滚动发布可 drain 旧 runtime version。

### 性能

- [ ] ingress accepted latency 达标。
- [ ] queue wait、TTFB、turn duration 按 provider/model 分层达标。
- [ ] event-loop lag、executor queue、DB pool wait 在目标内。
- [ ] Worker 扩容能降低 queue age，而不是把瓶颈转移到 PG/provider。
- [ ] WebChat 场景下 5000 WS 连接、慢客户端和重连风暴通过测试。

### 安全

- [ ] WebSocket 和所有 REST endpoint 要求认证。
- [ ] A token 无法访问 B 的 session/message/memory/attachment。
- [ ] session_key、tenant_id、object key 均不能由客户端伪造越权。
- [ ] 上传大小、mime、内容处理和下载授权均有测试。
- [ ] secrets、token、prompt 和用户文件路径不进入普通日志。
- [ ] 公网监听但 auth 关闭时启动失败。

### 成本与运维

- [ ] token/embedding/background call 成本有 low/base/high 预算。
- [ ] tenant/global budget alarm 和 provider quota alarm 生效。
- [ ] queue、DB、Redis、BlobStore、备份都有容量和保留策略。
- [ ] runbook 覆盖 provider 故障、数据库恢复、DLQ、部署回滚。

---

## 12. 长期演进触发条件

### 独立向量数据库

只有出现以下证据时再评估 Qdrant/Milvus/Weaviate：

- pgvector 在目标 recall 下无法达到查询 SLO；
- HNSW 索引和业务 OLTP 争用导致 PG 无法经济扩展；
- 分区、备份、重建或跨区域复制成为主要运维瓶颈；
- 需要 PG 难以提供的检索功能，并有清晰一致性方案。

### 更重的消息系统

只有 Redis Streams 在 pending 数、消费吞吐、保留、跨区域或审计方面达到可测瓶颈时，再迁移 Kafka/NATS JetStream。由于 `IngressQueue` 是 seam，broker 更换不应改 AgentCore。

### Kubernetes

只有在需要频繁滚动发布、自动伸缩、多可用区调度和统一运维平台时启用。5000 注册用户本身不是必须上 Kubernetes 的理由。

### 多区域

多区域 active-active 会引入 session 顺序、tenant home region、数据复制、对象存储和模型供应商路由问题。应先采用 active-passive/灾备，再基于明确 RTO/RPO 和地域合规需求设计。

---

## 13. 下一步建议

按风险和当前分支状态，推荐紧接着执行：

1. M4H-2 已完成；在 `feature/scaling-phase1-storage` 中先完成 **M4H-3 连接池/阻塞模型 ADR 与验证**，再完成 M4H-4 分区 provisioning 和 M4H-5 全量 merge gate。
2. 在独立低冲突分支完成 **Phase 0 metrics + load harness**，为 Phase 1 M7 和 Phase 2 提供统一证据。
3. 用小型设计文档定型 **TurnAdmission、IngressQueue envelope、inbox/outbox schema**，再开始 Phase 2/3 编码。
4. 暂停把 Redis cache 和公网 WebChat 当作当前关键路径；前者等待 PG 基准，后者等待身份与最终投递语义。
5. Phase 1 合并后，先完成“单实例 PostgreSQL + 有界并发”，再进入多 Worker；不要一次同时切存储、并发、队列和 WebChat。

这一路线把每一步都限制在可测、可回滚的 seam 上，避免把 5000 用户扩展变成一次不可验证的大爆炸式重写。
