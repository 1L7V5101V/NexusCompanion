# NexusCompanion 小范围长期测试路线图（Pilot Roadmap）

> **定位**：本文件是面向小范围、长期、可控试用的 program-level roadmap。目标不是立即把系统扩展到 5000 用户，而是先让少量受邀用户能够稳定使用 WebChat，并支持“输入一次邀请 Token，之后自动保持登录状态；出现异常时可立即封禁账号”。
>
> 本文件承载目标、范围、技术决策、阶段出口和升级触发条件；不承载具体实现 task、逐 commit 记录或行为规范的完整 source of truth。认证行为的最终契约应在后续 OpenSpec change/spec 中落地，当前代码行为仍以代码和测试为准。
>
> 上次审阅：2026-09-03。
>
> **当前实现状态（截至 2026-09-03）**：WebChat 已具备 P0.5 dev 模式最小闭环（commit `4e40e510`）。`bootstrap/chat_api.py` 与 `infra/channels/web_chat_channel.py` 提供本地 WebSocket 通道：hello 握手、`client_message_id` 幂等、进程内重放 buffer、慢消费者有界队列降级、`turn.completed`/`turn.failed` 终态帧；前端为 `frontend/chat/`（assistant-ui + 独立 connection/store 分层，构建到 `static/chat`），经 `[channels.chat]`（默认 disabled、127.0.0.1:6322）启用。协议契约与共享 fixture 见 `infra/channels/web_chat_protocol.py` 与 `tests/fixtures/chat_protocol_frames.json`。**尚未具备** Pilot 面向受邀用户的能力：认证/邀请 Token/tenant-bound principal、CSRF/Origin、durable ingress/outbox/delivery 状态机（5.9.11）、PG canonical control plane、跨端 Telegram 同步与 memory engine selector 均未实现；当前 WebChat 仅限本地 dev 单用户（`chat:local`，DEFAULT_TENANT），不得暴露公网。当前可用的另一对话入口仍是 Telegram Bot 通道。记忆运行时已支持按配置加载多个 engine（`default` 与 `rachael`）；但 `engine = "default,rachael"` 多引擎并存会因 `recall_memory` 重复注册在启动时崩溃（已知缺陷，需修复 `agent/tools/meta/register.py` 的注册逻辑），当前只能配置单引擎。

## 1. North Star

在一台个人电脑或单台 VPS 上运行一个可长期维护的 NexusCompanion Pilot：

- 面向约 **10–30 个受邀测试账号**；
- 目标同时在线规模约 **5–15 个 WebChat 会话**，并保留 Telegram Bot 通道接入；
- 目标定位上，WebChat 是 Pilot 的正式客户端；实现后，同一测试账号可以同时通过 WebChat 和 Telegram Bot 访问同一规范会话；
- WebChat 实现后，用户首次打开时输入一次管理员发放的邀请 Token；
- 服务端将把邀请 Token 兑换为可撤销的登录会话，并通过 HttpOnly Cookie 保存登录状态；
- 实现后，后续 HTTP 请求和 WebSocket 连接自动使用该登录状态，不再反复输入邀请 Token；
- 实现后，WebChat 将能读取已绑定 Telegram 身份的历史对话，并实时接收 Telegram Bot 通道收到的新消息；
- 目标能力包括：管理员可以按账号或单个 Token 查看、过期、撤销和封禁；
- 实现后，每个租户可以在 WebChat 中从管理员允许的 memory engine 插件中选择当前使用的引擎；首版提供 `default` 和 `rachael`，默认选择 `default`，切换在下一次 work 开始时生效；
- 实现后，封禁将拒绝新的请求，并主动断开该账号已有的 WebSocket 连接；
- 保持实现简单：单进程、单 Gateway、进程内队列、PostgreSQL + pgvector。

这条路线优先验证产品体验、稳定性和账号控制能力；只有当真实使用数据证明单机架构不够时，才进入下一阶段扩展。

## 2. 目标用户规模与运行边界

| 维度                          |                       Pilot 目标 | 说明                                                                  |
| --------------------------- | -----------------------------: | ------------------------------------------------------------------- |
| invited accounts            |                          10–30 | 由管理员手动发放，不开放自助注册                                                    |
| concurrent WebChat sessions |                           5–15 | 主要验证长期在线、重连和会话隔离                                                    |
| active LLM turns            |      30（全局可配置初始上限；maintenance 单独统计） | 表示全系统最多同时有 30 个正在执行并调用 LLM 的 interactive turn，不是“最多 30 个用户”；每个 tenant 内仍保持串行 |
| deployment                  |                  单台个人电脑或单台 VPS | 先不引入容器编排和多副本                                                        |
| queue                       |            进程内 `asyncio.Queue` | 为后续替换 Redis Streams 保留抽象边界                                          |
| public entry                | Cloudflare Tunnel 或同类 HTTPS 隧道 | 负责公网入口，不负责应用级身份认证                                                   |
| primary data                |          PostgreSQL + pgvector | Pilot 目标是统一管理会话、消息、记忆、Embedding、账号和 control-plane 元数据；当前实现仍存在 SQLite、JSON、Markdown 与本地文件分散存储 |

### 明确不做

当前 Pilot 不引入以下组件或能力：

- Kafka、NATS、Redis Streams；
- Nginx 作为必需组件；
- 多 Gateway、多可独立部署的水平 Worker、Kubernetes 或云端自动扩缩容；
- 独立向量数据库；
- 面向 5000 用户的并发压测和生产 SLO 承诺；
- 自助注册、复杂 RBAC、另起炉灶建设一套独立管理后台和长期 JWT 体系；管理端复用现有 Dashboard，并只增补 Pilot 所需账号管理页面；多租户 Pilot 暂不开放用户侧 manual consolidation。

> 如果未来需要持久队列或可独立部署的水平 Worker，优先把现有队列适配为 **Redis Streams consumer group**；但这不是 Pilot 的前置条件。

## 3. Pilot 技术栈

```text
受邀用户浏览器
      │ HTTPS / WSS
      ▼
Cloudflare Tunnel（公网入口，可选）
      │
      ▼
FastAPI / Uvicorn Web Gateway（应用层）
      ├── WebChat HTTP API + WebSocket
      ├── 现有 React Dashboard + Pilot 管理面板
      ├── 一次性邀请 Token 兑换
      ├── HttpOnly 登录 Cookie 校验
      ├── 账号/会话撤销与 WebSocket 断开
      └── 进程内 asyncio.Queue
              │
              ▼
       In-process Turn Worker（`PassiveMessageWorker`） ◄── Telegram Bot 通道
              │                         （保持在线并写入统一会话）
              ▼
 PostgreSQL + pgvector（统一账号、会话与消息主数据源）
```

### 组件职责

| 组件                           | Pilot 中的职责                                             | 是否必需                                                                     |
| ---------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------ |
| FastAPI/Uvicorn              | WebChat API、WebSocket、认证端点、Dashboard API，以及 WebChat/Telegram Bot 的统一会话入口 | 是                                                                        |
| 现有 React Dashboard          | 将当前单用户视图 tenant 化，支持管理员切换用户；增加账号管理面板和跨用户指标总控制台 | 是；当前已有 tenant 参数基础，但安全用户切换与全局总控属于计划增量 |
| Telegram Bot 通道              | 保持 Bot 通道可用；接收 Bot 消息并写入统一规范会话              | 是，不能因 WebChat 上线而移除                                                      |
| Cloudflare Tunnel            | 提供公网 HTTPS/WSS 和隐藏家庭网络入口                               | 面向外部用户时建议                                                                |
| PostgreSQL                   | 账号、登录会话、tenant、统一 session/message、消息游标与 delivery 状态    | 是，长期测试建议使用                                                               |
| pgvector                     | 稠密向量存储、ANN 检索与 tenant 过滤                               | 使用语义记忆时是                                                                 |
| memory engine plugins        | 按统一 `MemoryPlugin` / `MemoryEngine` 契约提供记忆写入、召回、管理和诊断能力；首版包含 `default` 与 `rachael` | 是；两个引擎都由平台安装和维护，具体租户通过 WebChat 选择允许的一个                           |
| 阿里云 `text-embedding-v3`      | 生成查询与记忆条目的稠密向量                                         | 是；当前 `Embedder` 已按此模型配置                                                  |
| jieba + ParadeDB `pg_search` | Pilot 目标中的中文分词与 BM25 稀疏检索                              | 是；当前代码尚未接入，见记忆召回链路                                                       |
| hotness score                | 在 RRF 融合后对检索分数做乘性热度增强                    | 是；dense/keyword lane 内都不再预混热度（`hotness_alpha=0`），RRF 后按 `fused = rrf × (1 + β×hotness)` 增强，β 默认 `0.05`，半衰期默认 `14` 天 |
| RRF + top-k                  | 合并 dense/keyword 候选并截断结果                               | 是；当前已实现，但使用固定 `RRF_K=60` 与 keyword 权重 `0.5`                              |
| HyDE-style hypothesis        | 根据 query 生成假设文本，再参与记忆召回                                | 当前 `answer` intent 已有；目标改为默认关闭，后续做消融实验                                   |
| query rewrite                | 在检索前改写 query                                           | 否；可以先实现开关，但 Pilot 默认关闭，后续做消融实验                                           |
| reranker                     | 对 RRF 候选做最终重排                                          | 否；当前 `DefaultMemoryEngine` 未实现，后续保留开关并做消融                                |
| `asyncio.Queue`              | 单进程内的入站任务排队                                            | 是                                                                        |
| Redis                        | 当前不作为消息队列；只有已有缓存需求时才单独启用                               | 否                                                                        |
| Nginx                        | 反向代理、静态资源或多实例入口                                        | 否，未来按需加入                                                                 |

### 3.1 Runtime 术语与工作模型

本路线图统一使用以下术语。核心原则是：**Pipeline 描述业务链路，Worker 描述队列消费或部署单元；记忆召回和工具调用不是 Worker。**

| 术语 | 定义 | 当前例子 | 是否等同于 Worker |
| --- | --- | --- | --- |
| Pipeline | 一类交互或维护流程的阶段图，描述业务如何执行 | `PassiveTurnPipeline`、`ProactiveTurnPipeline`；`Drift` 是 Proactive 的无行动分支，不是独立并发 Pipeline | 否 |
| Stage / Module | Pipeline 内的处理阶段或可插拔模块 | `memory_context_guard`、`memory_retrieval`、reasoning、tool、delivery | 否 |
| Executor | 执行一次具体操作的组件或调用边界 | `ToolExecutor`、记忆检索 executor、LLM executor、`MemoryOptimizer` | 否；它是被调用的执行器 |
| Scheduler / Loop | 按时间、事件或条件产生 work item 的触发器 | `ProactiveLoop`、`MemoryOptimizerLoop`、turn-level consolidation gate | 否；它负责触发，不负责定义业务链路 |
| Work item / Envelope | 一次待执行任务及其可信上下文 | `work_kind`、`flow`、`stage`、`tenant_id`、`session_key`、`trigger`、`snapshot_id`、幂等键 | 否；这是任务数据 |
| Tenant identity binding | 服务端把已认证的入口 principal 绑定到 `account_id → tenant_id → canonical_conversation_id` 的可信关系；`tenant_id` 不是客户端字段 | WebChat auth session、Telegram 私聊 identity binding、admin provisioning | 否；这是身份映射 |
| RuntimeSnapshot lease | 一次 work 固定使用的进程级插件 generation 快照引用；保证代码与普通配置在 work 内一致，不承担 tenant 授权 | `snapshot_id`、generation、lease 生命周期 | 否；这是运行时租约 |
| TenantRuntimePlan | 根据 `snapshot_id`、tenant policy revision 和 tenant 配置解析出的不可变租户插件视图 | enabled plugins、lifecycle modules、tool catalog、jobs、config revision | 否；这是租户视图 |
| PluginInvocationContext | 每次 hook/tool 调用收到的 tenant-bound 调用上下文 | `WorkContext`、plugin config/KV、secret/policy/effect capability | 否；这是调用上下文 |
| Plugin contribution / 插件能力项 | AgentLoop 先定义可用的生命周期 Hook 和调用契约；插件再通过 `before_turn_modules()`、`@on_after_turn`、`@tool`、job/proactive spec 等方式声明自己参与哪个既有入口 | 一个 before-turn module、一个 tool、一个 job 或一个 proactive source | 否；它是插件暴露给 runtime/tenant 选择的一项能力 |
| Hook handler / Hook 处理函数 | 某个 Hook 触发时由 runtime 实际调用的插件函数或 module method；即 callback，不是新的 Hook 类型 | `record_context_prepare(event)`、`@on_after_turn` 修饰的方法 | 否；它是 Hook 上执行的代码 |
| Plugin binding policy / 插件绑定策略 | 平台为插件能力项声明租户默认策略；`required` 表示所有 tenant 强制启用，`default_on` 表示创建 tenant 时自动配置但可按策略关闭，`opt_in` 表示默认不启用 | memory infrastructure、默认工具、普通可选插件 | 否；它决定能力如何进入 `TenantRuntimePlan` |
| In-process Worker | 当前进程内消费队列、建立 lane 并驱动任务执行的组件 | `PassiveMessageWorker`、Markdown background maintenance worker | 是 Worker，但不是独立进程 |
| Horizontal Worker | 未来可独立部署、从持久队列或 Redis Streams consumer group 消费任务的进程实例 | 当前不引入 | 是扩展部署单元 |
| Maintenance | 不直接产生用户可见 turn 的记忆维护工作 | background consolidation、recent-context refresh、optimizer | 它是工作类型，不是 Worker |

本文中的“任务”统一指 **Work item / Envelope**：一项有明确触发来源、业务类型、租户/会话归属、执行状态和结果的待处理工作。它是数据和生命周期概念，不等于 `Worker`；一个 Worker 可以连续消费多个任务，一个任务也可以在多个 Stage/Executor 之间流转。交互消息提交后形成的 `TurnRecord` 是一种任务，maintenance queue 中的 consolidation 也是一种任务，proactive/optimizer 的 tick 则是触发任务的调度事件。

当前单体的 interactive turn 由 `ConversationRuntime` 持有和收束：任务通过 `turn_id` 关联到 `thread_id`，tenant 信息随 request metadata 传入；同一 thread 同时只允许一个 active turn。任务状态持久化为 `queued`、`in_progress`、`completed`、`interrupted`、`failed` 或 `cancelled`。当前没有统一的 ConversationRuntime 全局 turn/tool deadline；超时由具体 provider/tool 自己实现，例如 provider timeout 会转成 `failed`，Shell tool 有自己的前台/后台 timeout。运行时取消通过取消 turn task 并由 `_run()` 的 `CancelledError` 处理器提交终态完成。

因此，以下归类固定下来：

- `Passive`、`Proactive`、`Drift` 是 **interactive flow / Pipeline**；其中 Drift 只有在 Proactive 判定“本轮无行动”后才进入。
- `turn-level consolidation` 是交互 turn 内的 **maintenance stage**；`background consolidation` 和 `recent-context refresh` 是 turn 提交后的 **maintenance work**。
- `optimizer` 是定时触发的 **maintenance flow**，由 `MemoryOptimizerLoop` 触发并由 `MemoryOptimizer` 执行。
- `memory retrieval` 是 turn Pipeline 中的 **Stage / Executor**；`tool call` 是 tool stage 中的一次 **Executor operation**。二者都不因为异步、耗时或可重试而自动变成 Worker。
- 代码中名为 `*Worker` 的类可以保留原名，但文档必须注明它是 `in-process background worker`；只有未来从持久队列独立消费的进程才称为 `Horizontal Worker`。

Pilot 的逻辑模型如下：

```text
Ingress / Trigger
├── PassiveMessageWorker
│   └── PassiveTurnPipeline
├── ProactiveLoop / Scheduler
│   └── ProactiveTurnPipeline
│       └── no-action branch → Drift flow
└── Maintenance Triggers
    ├── turn-level consolidation gate
    ├── post-turn maintenance queue
    │   ├── background consolidation
    │   └── recent-context refresh
    └── MemoryOptimizerLoop → optimizer flow

Shared Runtime Controls
├── tenant/session admission
├── single execution-chain admission for the current Pilot
├── per-session state-write serialization
├── interactive / maintenance work kind
├── tool timeout, cancellation and idempotency
├── plugin RuntimeSnapshot binding
└── flow / stage / tenant observability
```

### 3.2 Pilot 运行时约束

以下是当前单体行为需要在多租户 Pilot 中明确化的运行时契约。这里的并发边界是**按 tenant 划分**：当前 Pilot 设定为一个 tenant 只有一个规范 session，因此 tenant 就是 session admission key。

- **租户内串行、租户间异步**：同一个 tenant 内，Passive、Proactive、Drift、consolidation、optimizer 和 tool call 不并发执行，共用一条串行执行链；不同 tenant 之间不共享这条串行 lane，可以异步执行，不能因为某个 tenant 忙而阻塞其他 tenant。
- **工具调用的边界**：用户所说的“执行链”不仅指工具调用，也包括一次 interactive turn 和它触发的 memory maintenance。工具调用在 tenant 内串行；不同 tenant 的工具调用可以异步并行，但仍受全局 LLM、工具、数据库和外部服务资源上限约束。
- **Drift 依附 Proactive**：Proactive 先完成自己的判定；只有结果为“无行动”时才进入 Drift。不能把 Drift 作为与 Proactive 并行竞争的独立 scheduler。
- **工作类型**：`interactive` 表示 Passive/Proactive/Drift 产生用户 turn；`maintenance` 表示 consolidation、recent-context refresh 和 optimizer。`active LLM turns` 只统计当前同时执行、且至少正在调用一次 LLM 的 interactive turn 数，不等同于 Worker 数；maintenance 单独统计。
- **租户内调度顺序**：同一 tenant 内优先保证当前 interactive turn；maintenance 在 tenant lane 空闲时执行，或在有新的 interactive turn 到来时 defer。一个 tenant 的 backlog 或 maintenance 不影响其他 tenant。
- **同 session 串行**：一个 tenant 当前只有一个规范 session，因此不设计跨 session 并发或跨 session 锁。任何会修改该 tenant 的 session、memory cursor 或近期上下文的操作都必须进入该 tenant 的串行 lane。
- **Consolidation 以当前代码为准**：当前默认 `agent.context.memory_window = 40`，`bootstrap/memory.py::_memory_keep_count()` 将其换算为 `keep_count = 20`；`before_turn.py::_MemoryContextGuardModule` 使用 `threshold = keep_count + max(5, keep_count // 2)`，所以默认 threshold 为 **30 条 pending 消息**。
  - pending 少于 threshold：当前 turn 不触发 memory context guard consolidation。
  - pending 达到或超过 threshold：turn-level guard 调用 `trigger_memory_consolidation()`。
  - consolidation 成功：继续当前 turn。
  - consolidation 抛异常或返回 `False`：当前实现会立即阻断当前 turn；不是“先跳过、下个 turn 再试”的 10–20 区间策略。
  - 当前日志会记录异常、pending 和 threshold；用户可见的 abort reply 会显示 pending、threshold、keep_count、`last_consolidated` 和 total messages，但不会把内部异常原因拼进用户文案。Pilot 先保持这个单体行为，不额外要求用户看到失败原因。
- **租户上下文**：所有入口、Pipeline、Stage/Executor、consolidation 和 optimizer 都使用服务端派生的 `tenant_id`。任务 envelope 显式携带 `tenant_id + session_key`；禁止从客户端参数或 `DEFAULT_TENANT` fallback 推导租户。
  - **服务端派生**的含义是：channel/HTTP/WebSocket adapter 先验证入口携带的可信 principal（例如有效 WebChat auth session，或管理员预绑定的 Telegram 用户私聊 identity），再由服务端数据库中的 binding 查询得到 `account_id → tenant_id → canonical_conversation_id`。客户端提交的 `tenant_id`、`account_id`、`chat_id`、`session_key` 或 tool 参数只能作为不可信输入，不能决定数据查询、工具授权或副作用目标；`session_key` 只是路由/兼容标识，不是授权凭据。
  - 认证账号与 tenant 的映射在 work 创建时固化到 `WorkEnvelope`，后续 hook、tool、后台 job 和恢复流程只接受该可信归属；如果入口没有可验证的 binding，必须拒绝或进入显式 dev-only 单用户路径，不能猜测归属。
- **工具调用与连接状态**：WebSocket 断线只影响实时显示和事件投递，不自动取消服务器端正在执行的 turn/tool call；完成状态必须可通过重连补拉。账号封禁则立即阻断该 tenant 的后续执行，并取消或截断正在执行的 turn/tool call。
- **外部副作用**：工具即使有外部副作用，也只能作用于当前 tenant 的资源；不得允许客户端在执行过程中切换 tenant。断线后允许当前服务器端调用作为当前 turn 的一部分完成，但不允许把它脱离 turn 变成没有归属、没有终态和没有取消边界的永久后台任务。重连不得重复执行；以当前单体工具行为为基线，后续只补齐每类工具已有的超时、取消、失败和幂等记录。
- **插件快照**：Pilot 目标契约是每个 Passive、Proactive、Drift、maintenance 和 plugin job 在 work 开始时取得并绑定一次 `RuntimeSnapshot` lease，执行中不切换 snapshot。当前代码已在 proactive loop、plugin jobs 和 Dashboard plugin host 显式取得 lease，但尚未证明所有 Passive/control path 都普遍满足该契约，因此必须在 P0 做 binding audit。`RuntimeSnapshot` 是插件热重载的一致性快照，不是 memory maintenance lock。
- **插件安装、激活与租户 Hook 绑定**：Pilot 把三个状态分开：`installed` 只表示管理员已把插件包放入服务端插件目录并把 manifest 登记进 package catalog；这可以称为“登记了插件包”，但还不是 Python 代码层面的 class/handler 注册。装饰器和 `Plugin.__init_subclass__()` 只有在候选激活阶段 import 插件代码后才会执行；`active generation` 表示该插件已由进程级 `PluginManager` 完成候选 import、实例化、初始化和 snapshot 编译，并进入 base `RuntimeSnapshot`；`tenant binding` 表示某个 tenant 实际启用了哪些 contribution。插件安装后可以不挂任何 Hook，也可以不向任何 tenant 暴露 tool/job，此时保持 dormant，不进入 tenant 的 prompt、Hook、tool catalog 或后台任务。只要至少一个 tenant 启用某项 contribution，resolver 才把对应 active generation 和 contribution 纳入该 tenant 的 `TenantRuntimePlan`。AgentLoop 声明有哪些 Hook 及其 context/顺序，插件通过返回 module 或使用 decorator 声明自己参与其中哪个 Hook；每个插件能力项（contribution）使用稳定 `contribution_id` 并固定所属 hook/tool/job 类型。tenant 可以热启用或停用这些已注册能力项，但不能把一个只实现 `before_turn` 契约的方法直接改挂到 context 不同的 `after_turn`。安全 gate、授权 interceptor 和 process-scoped channel/managed service 不作为普通 tenant 可配置项。
- **插件实例状态**：插件 generation/definition 和无 tenant 内容的共享连接池可以进程级复用；tenant enable/config/policy/KV/credential 必须按 tenant 绑定；当前 turn、tool 参数、hook 临时结果和 cancellation 必须放在 work/call context 中。插件实例不得保存可变的 `current_tenant`、`current_session`、当前 prompt/tool 参数或未分区的 tenant 业务缓存；`ContextVar` 只可用于 trace/log 和兼容适配，不能作为授权、资源定位或副作用路由的唯一依据。
- **插件代码与 Hook 配置热更新**：Pilot 由管理员承担插件代码可信判断，不建设签名、恶意代码扫描、sandbox、来源证明或复杂依赖审计；管理员上传/安装即代表信任。单纯安装只登记包和 manifest，不 import/initialize；当管理员首次激活插件或更新一个已激活插件时，服务端只做保证运行时不会被半成品替换所必需的最小正确性检查：插件可发现、manifest/`contribution_id`/Hook 类型可解析、候选 generation 可 import/initialize、snapshot 可编译。检查成功后原子发布，新 work 使用新 generation，进行中 work 继续持有旧 lease，旧 generation 在 lease 结束后释放；检查失败则保留旧 generation 并向管理员显示错误。tenant 对已声明 contribution 的启用/停用、顺序和配置修改不需要重载插件代码，只更新该 tenant 的 policy/config revision，并从下一个 work 解析新的 `TenantRuntimePlan`；进行中的 work 不改变 Hook 集合。若要新增 Hook、把 handler 改到插件未声明的 Hook 或修改 handler 代码，则属于插件代码/manifest 更新，仍走 generation 热更新。Pilot 不预先建设滚动重启、native extension 识别、process-global side-effect 分类或自动兼容迁移；遇到无法热更新的插件，保留旧 generation、报告失败，由管理员在维护窗口手动重启单实例服务。Pilot 不支持 tenant 间运行不同的 plugin binary version；若未来确有不可信或完全独立版本需求，再评估独立进程/容器。
- **插件调用边界**：所有生命周期 hook 都通过统一的 `PluginInvocationContext`/dispatcher 调用，显式携带同一个 `WorkContext`、tenant-bound session/memory/KV/policy/effect capability 和 snapshot lease；后台 EventBus handler、proactive、optimizer、consolidation、recovery 和 plugin job 若会修改状态或产生副作用，必须转换为带 tenant ownership 的 work 并重新进入 tenant lane。
- **“全局 maintenance lock”不采用**：全局锁就是所有 tenant 共用的一把锁，任何一个 tenant 做 consolidation/optimizer 时都会挡住其他 tenant。这个模型与“租户互不影响”冲突，Pilot 不引入它；只使用 tenant/session 级串行 lane。当前代码中的 `_maintenance_locks` 已是按 session 建立的锁，optimizer 自身 lock 仍需在多租户化时按 tenant 隔离，不能让单个 tenant 的 optimizer 锁住所有租户。
- **可靠性术语**：`at-least-once` 表示任务至少尝试执行一次，失败后允许重试，因此必须幂等；`best-effort` 表示尽力执行，失败或重启后可以跳过，之后从持久化状态重新生成即可。这里不是要新增抽象，而是描述不同任务的恢复要求。
- **重启基线**：当前单体的 `MessageBus._inbound`、outbound queue、Passive lane queue、Markdown maintenance queue 和大多数 `asyncio.create_task` 都只存在于进程内；`AppRuntime.shutdown()` 会取消 runtime tasks，`PassiveMessageWorker` 会取消 lane tasks，`ProactiveLoop`/`MemoryOptimizerLoop` 停止循环。因此当前重启不会自动恢复这些内存队列中的未完成任务，也没有统一的启动补偿扫描。
- **用户侧 manual consolidation**：当前 manual consolidation/control path 仍有单体时代的 `DEFAULT_TENANT` 风险。多租户 Pilot 暂时关闭用户侧 manual consolidation；后台自动 consolidation 保留。待 tenant-aware control API、权限和审计完成后再恢复。

这些约束不要求 Pilot 现在引入 Redis、独立 maintenance service 或多个进程；首先要求建立 tenant-scoped admission，使不同 tenant 异步运行、同一 tenant 串行运行。

### 3.3 当前单体基线、Pilot 问题与解决路径

本节把“现状”和“目标”分开。当前单体行为是 Pilot 的兼容基线，但不能直接当作多租户 Pilot 的完成方案。

| 当前单体基线/问题 | 对 Pilot 的影响 | Pilot 解决路径 | 验收证据 |
| --- | --- | --- | --- |
| `ConversationRuntime._admission` 是进程级锁；它会把不同 tenant 的 turn 放进同一条全局串行路径 | 一个 tenant 的慢 LLM、tool 或 consolidation 可能阻塞其他 tenant，违反“租户互不影响” | 以服务端派生 `tenant_id` 建立 tenant-scoped lane；tenant 内串行、tenant 间异步；只保留独立的全局资源上限，不用 global maintenance lock | 不同 tenant 的执行时间线无交叉等待；同一 tenant 的状态写入无重叠 |
| `MessageBus`、Passive lane、maintenance queue 和运行中 task 主要是进程内状态 | 重启会丢未消费项、流式事件和未完成 task；当前没有统一启动补偿扫描 | 为 inbound/outbound/maintenance 建 durable record；启动扫描并按 work 状态 replay 或 recompute；active tool 先确认 outcome，再 compensation 或标记 unknown | 重启演练能逐条解释 replay、recompute、compensate、cancelled 或 unknown |
| tenant 信息已在部分 request metadata 中传递，但仍存在单体 control path 的 `DEFAULT_TENANT` 风险 | 用户可能越权访问其他 tenant，或后台任务丢失真实归属 | 所有 ingress、Pipeline、Stage/Executor、memory、consolidation、optimizer 和恢复记录强制携带服务端派生 tenant；禁止客户端 tenant 覆盖和隐式 fallback | 越权测试失败；每个 work record 都能回溯 tenant/session |
| 工具都实现 `Tool.execute()`，但 timeout、取消、retry、幂等和副作用能力不一致；`ToolRegistry` 还会把部分异常转换为字符串结果 | Runtime 无法仅凭统一接口判断“工具失败”“业务返回错误”还是“结果未知”，也无法通用 rollback | 先建立 capability matrix；在不改变单体业务语义的前提下，为 Pilot 工具补充 typed outcome、tool_call_id、状态查询和必要 compensation 适配 | 每类启用工具都有明确 capability 行；失败、取消、超时和 unknown 可观测 |
| 当前 turn 有 `queued/in_progress/completed/interrupted/failed/cancelled` 状态，但没有统一的 turn/tool 全局 deadline | 连接断开、账号封禁或上游卡住时，不能仅靠 turn 状态推断底层 tool 是否已经停止 | 保留 provider/tool 自有 timeout；把取消请求、timeout、tool outcome 和 turn terminal state 串联记录；封禁时 tenant 级取消，工具不响应则使用其已有 timeout 兜底 | 每个 active turn/tool 最终都有 owner、timer 来源、取消结果和 terminal/unknown 状态 |
| WebSocket 连接生命周期与服务器端执行生命周期是两套状态 | 断线后容易误以为执行已取消，重连又可能重复提交 | 断线只停止显示订阅；服务器端继续执行或收束；重连按稳定 id/sequence 补拉；账号封禁走服务端 cancellation，不依赖 socket | 断线/重连不重复执行；封禁只截断目标 tenant |

**优先级和边界**：

1. P0 记录 work/turn/tool 生命周期和当前全局 admission 的等待事实，建立 tenant-scoped admission、有界 queue、persistence/backup map 与 snapshot lease audit，但不先改变已稳定的工具业务语义；
2. P0.5 在 dev-only WebChat 闭环中建立 durable inbox/acceptance、canonical final、outbox/delivery 状态机和重连协议；
3. P1 完成认证、provisioning/readiness、tenant principal、attachment auth 和 PostgreSQL canonical control-plane 公网门禁，并消除 control/manual path 的 `DEFAULT_TENANT` fallback；
4. P2 建立账号封禁到 lane、turn、tool、schedule 和 delivery 的取消/暂停传播，同时完成 Pilot 工具能力矩阵；
5. P3 完成启动恢复扫描、delivery/schedule/provisioning/attachment 恢复、active tool outcome 确认和备份演练；只有真实 backlog、恢复和重复副作用数据越过 P4 闸门时，才评估 Redis Streams 或 Horizontal Worker。


### 3.4 当前实现地图与阶段计划接缝

下表只记录会改变阶段拆分、协议、表结构或恢复语义的代码事实。它不是缺陷清单，也不表示 Pilot 目标已经实现；后续每个 change 的 design 必须显式引用相关行，并说明是保留兼容、迁移还是替换。

| 域 | 当前实现事实 | 对 Pilot 的缺口 | 阶段计划前必须冻结 | 目标阶段 |
| --- | --- | --- | --- | --- |
| WebChat 宿主 | `bootstrap/chat_api.py` 提供 sessions/messages/uploads/media/`/ws` 路由；`infra/channels/web_chat_channel.py` 已实现 dev 适配器（hello 握手、client_message_id 幂等、重放 buffer、有界队列降级、终态帧），`frontend/chat/`（assistant-ui）构建到 `static/chat`，经 `[channels.chat]`（默认 disabled、127.0.0.1:6322）启用 | dev 闭环可用，但没有 auth、tenant principal、CSRF/Origin、请求体上限、媒体 ownership 与 durable control plane | 协议 fixture 的 P1 契约冻结（错误码、schema version、close code）、认证依赖、dev-only 暴露门禁维持 | P0.5（dev）/P1（公网） |
| Ingress identity 与去重 | `InboundMessage` 只有 channel/sender/chat/content/media/metadata/tenant；默认 `session_key = channel:chat_id`，没有 canonical `message_id`、delivery id 或稳定 inbox id | Telegram 重试、WebChat 客户端重发和跨端同步无法依赖统一幂等键 | source/client message key、acceptance transaction、canonical message identity | P0.5 |
| MessageBus 与 Passive lane | inbound/outbound queue 和 per-session Passive lane 都是无界进程内 `asyncio.Queue`；Passive lane key 是 `session_key`，MessageBus `ChatLane` key 是 `(channel, chat_id)`，都不是 canonical tenant key | 重启丢 backlog；慢 tenant 可耗尽内存；当前 `complete_inbound()` 只表示 worker 已处理并发布到内存 outbound，不表示 channel 已送达 | tenant admission key、容量、overload、durable inbox/work 重建规则 | P0/P0.5 |
| Outbound delivery | `OutboundMessage` 没有 tenant/canonical message/delivery/idempotency identity；dispatcher 调 channel callback，2 秒后重试一次，再尝试一次 fallback；全部失败后日志明确表示消息丢失；没有 delivery record、ack 或 durable outbox | “模型生成完成”和“用户已收到”被混在一起，无法可靠重试或审计 | final message、outbox intent、delivery attempt/ack 的事务边界和幂等键 | P0.5/P3 |
| Turn control plane | `ConversationRuntime` 的 admission lock 是进程全局；active task/result/subscriber 在内存；queued turn 可先持久化，但 PG session/message 与 SQLite `turn_audit.db` 仍是双存储 | 不同 tenant 会互相阻塞；重启后 active work 不能只靠 turn 表安全重放 | tenant lane、terminal/unknown 状态、取消传播、PG control-plane cutover | P0/P1 |
| 持久化 ownership | PG mode 已承载部分 tenant session/message/memory；turn audit、proactive state、schedule、Markdown memory、attachments、config/secrets 仍分散在 SQLite/JSON/Markdown/文件系统 | 不能把“PostgreSQL 统一 primary data”描述成当前事实；备份范围不明确会造成静默数据缺口 | 每类数据的 canonical store、compatibility store、backup/restore manifest | P-1/P0/P3 |
| Tenant provisioning | partition readiness service 有 `unknown/pending/ready/failed`，但状态和队列只在单进程内存；首次 turn 可触发 provisioning 后立即因 not-ready 失败 | 账号签发后首轮体验不稳定；重启后 pending/failed provisioning 不可可靠恢复 | account `provisioning` 生命周期、ready 后发 Token、启动恢复和 admin retry | P1/P3 |
| 显式用户 schedule | `ScheduledJob` 只保存 channel/chat target，落在全局 `schedules.json`；one-shot 超过 5 分钟 grace 会丢弃，recurring 只跳到下一未来时间；execution task/outcome 不 durable | 用户创建的业务任务可能跨 tenant 误投递或重启后无法解释；它不能与可重算 proactive tick 混为一类 | tenant/account/conversation owner、服务端 target binding、misfire、execution idempotency/outcome | P2/P3 |
| RuntimeSnapshot 与 tenant 配置 | snapshot 是进程级共享 immutable generation，含工具 registry、workspace MCP generation、plugin generations、channel/hooks/jobs；tenant catalog/credential/revocation 不能安全地被旧 snapshot 隐含缓存 | 热重载一致性与 tenant 授权是两套边界；旧 snapshot 不能绕过封禁或密钥轮换 | base snapshot 与 per-task tenant context 分层、lease 覆盖面、revocation recheck、hook failure policy | P0/P2 |
| 单体 PluginManager 与租户插件集合 | 当前 plugin definitions、instances、KV/data dir 和 hooks 主要按进程/插件组织，没有统一 tenant plugin catalog；共享 context 过宽，插件实例若保存当前调用状态会发生串租户 | 不需要每 tenant 一个 PluginManager；保留共享 base snapshot，由 tenant policy 解析 `TenantRuntimePlan`，所有 hook/tool/job 通过 tenant-bound invocation context 调用；Pilot 只允许管理员管理的 trusted plugin package，tenant 自定义先限于 enable/config/binding | tenant settings/KV namespace、依赖闭包、dispatcher、旧 generation drain、插件实例状态审计和跨 tenant 负向测试 | P0/P1/P2 |
| Attachments/media | `AttachmentStore` 以 UUID 文件名写 workspace uploads 或 `/tmp/nexus_uploads`；Inbound media 暴露本地路径；没有 tenant namespace、canonical attachment metadata、quota、retention 或 backup manifest | 路径泄露、越权读取、临时文件丢失和无界存储风险 | immutable `attachment_id`、ownership、MIME/size、清理、备份和下载授权 | P1/P3 |
| Dashboard/admin edge | 当前 Dashboard API 没有可作为公网 Pilot 边界的统一认证中间件，部分页面/API 仍依赖 owner/single-user 假设或 tenant filter 参数 | Dashboard 一旦公网暴露会把“筛选参数”误当授权 | admin principal、session、CSRF/Origin、tenant 下钻审计、内容访问权限 | P1/P2 |
| Observability/privacy | 已有结构化指标与 turn trace 基础，但 provider payload、prompt、tool args、secret/PII/path 的默认采集与脱敏边界尚未统一 | 可能为了排障把跨 tenant 内容或密钥写入日志/指标 | 默认采集字段、redaction、content access audit、retention；SLO 数值后置 | P-1/P3 |

> **决策注（2026-09-06 已定案）**：turn 记录与查询日志（turn audit，现 `RoutingTurnLogger` → `{workspace}/logs/{passive,proactive,drift}.db`）归属定为**独立 PG audit 库/schema**（分库/分表，与 memory/session 主库隔离，不并入主库 schema），过渡期 SQLite-only 须显式声明一致性/备份/恢复策略；完整 ADR 见 [records/turn-audit-storage-ownership.md](records/turn-audit-storage-ownership.md)，同步于 [storage-migration spec](specs/storage-migration/spec.md)「turn control plane 数据归属定案」。

## 4. 记忆召回链路

本节区分“当前代码基线”和“Pilot 计划目标”。Pilot 首版支持两个由平台管理的 memory engine 插件：`default` 和 `rachael`。`default` 仍是新租户的默认引擎和主要质量基线，`rachael` 作为可选替代引擎通过 WebChat 选择。每个 work 只使用一个已经解析好的 active memory engine，不把外层 `AgenticRAGPipeline` 自动当作默认召回层，也不能把后续计划中的 `jieba + ParadeDB pg_search + reranker` 描述成已经存在的实现。

### 4.1 当前 `DefaultMemoryEngine` 实现

`DefaultMemoryEngine.query()` 先按 `MemoryQuery.intent` 分流：

| intent | 当前行为 |
| --- | --- |
| `context` / `procedure` | 调用 `_query_context()`；解析 scope、memory types 和辅助 query，进入 `Retriever.retrieve()`；`procedure` 默认限定 `procedure`、`preference` 两类 |
| `answer` | 并行调用轻量模型生成 `event` 和 `general` 两个 HyDE-style hypothesis，再进入 `Retriever.retrieve()`；候选 `top_k = max(request.limit, 15)`，最终切回 `request.limit` |
| `interest` | 调用 `_query_interest()`；只检索 `preference`、`profile`，使用 `request.limit` |
| `timeline` | 不走 dense/keyword/RRF；要求时间范围，直接按时间范围读取事件 |

对 `context`、`procedure` 和 `answer` 的语义检索，当前实际链路是：

```text
MemoryQuery
    ↓
DefaultMemoryEngine.query()
    ↓
按 intent 解析 memory_types / scope / aux_queries
    ↓
Retriever.retrieve()
    ├── dense lane
    │     ├── 原始 query + aux_queries 去重
    │     ├── 每条 query 调用 Embedder.embed()
    │     ├── 阿里云兼容接口 text-embedding-v3
    │     └── MemoryStorage.vector_search_batch()
    │           失败时逐向量回退到 vector_search()
    │           └── dense score = semantic + hotness 融合分数
    │
    ├── keyword lane
    │     ├── 当前使用自定义正则提取 ASCII token 和中文二元词
    │     ├── SQLite MemoryStore2：优先 FTS5 BM25
    │     │   （查询含 3 字符以上 ASCII/CJK token 时）
    │     ├── SQLite 不满足条件或 BM25 无结果：summary OR-LIKE 保底
    │     └── PostgreSQLMemoryStore：当前只有 summary ILIKE OR 查询，
    │         尚未接入 jieba 或 ParadeDB pg_search BM25
    │
    └── _rrf_merge()
          ├── vector 候选按 dense final score（semantic + hotness）排名
          ├── 当前 keyword 候选沿用 keyword lane 顺序（尚未融合 hotness）
          ├── RRF_K = 60
          ├── dense 权重 = 1.0；keyword 权重 = 0.5
          ├── 按 item id 合并，取两路 id 的并集
          └── 按 rrf_score 排序后截断为 actual_top_k
    ↓
DefaultMemoryEngine.build_injection_block()（仅 context/procedure）
    ├── 当前按 score 再排序并按 memory type 阈值过滤
    ├── procedure/preference 与 event/profile 分区
    ├── procedure guard 可强制注入带 tool_requirement 的 procedure
    └── 应用字符预算，默认上限 1200 字符
```

当前候选数量、分数和过滤细节：

- `Retriever` 的 `actual_top_k` 来自请求的 `top_k`，未指定时使用 retrieval 配置的 `top_k_history`；最终 RRF 结果数量为 `actual_top_k`。
- keyword lane 的查询上限为 `max(30, actual_top_k * 2)`；它只使用原始 query，不使用 `aux_queries`。
- dense lane 对原始 query 和 `aux_queries` 分别生成 embedding，并通过 `vector_search_batch` 批量查询；同一 item 在多条向量查询中只保留最高 dense final score 的版本。
- 当前 dense lane 内不做热度混合：`hotness_alpha` 默认 `0`。`MemoryStore2` 和 PostgreSQL vector store 都先用 semantic similarity 做 `score_threshold` 过滤，纯语义分数进入 RRF；热度在 RRF 融合后乘性增强 `fused = rrf_score × (1 + hotness_beta × hotness)`，`hotness_beta` 默认 `0.05`，`hotness_half_life_days` 默认 `14.0`。keyword lane 命中同样携带热度三件套参与增强。
- 当前 hotness 由“强化频度 × 时间衰减”得到：频度使用 reinforcement 的平滑函数，时间衰减使用半衰期；`emotional_weight` 会把有效半衰期按 `1 + 0.5 * emotional_weight / 10` 拉长。每个结果还保留 `_score_debug.semantic`、`_score_debug.hotness` 和 `_score_debug.final` 供诊断。
- answer intent 使用 `score_threshold=0.35` 和候选 `top_k=max(request.limit, 15)`；其他语义 intent 使用 Retriever 配置的阈值（构造器默认值为 `0.45`，实际可由配置覆盖）。
- 当前没有独立 reranker。`rrf_score` 用于 RRF 候选排序；返回 item 的 `score` 仍是 dense lane 的语义分（lane 内 α=0，final==semantic），keyword-only 条目才回填 `keyword_score`。当前注入阶段又按 `score` 排序和阈值过滤，因此可能覆盖 RRF 排序；后续应明确以 RRF 结果为默认顺序，并在启用 reranker 时以 reranker 分数为最终顺序。

### 4.2 外层 `DefaultMemoryRetrievalPipeline` 的边界

`agent/retrieval/default_pipeline.py::DefaultMemoryRetrievalPipeline` 虽然在代码中兼容到 `AgenticRAGPipeline`，但**不属于本 Pilot 的默认召回链路**：

- Pilot 当前运行时已支持按 `config.memory.engine_names` 注册多个 engine；`default` 与 `rachael` 分别由各自的 memory plugin 构建，且可以保留各自的 tool profile、存储和诊断能力。当前代码的 `primary_engine` 仍按配置顺序确定，尚未把“租户选择哪个 engine”接入 WebChat。
- 对某个租户的正式 turn，Runtime 必须先从 tenant memory-engine binding 解析一个 active engine，再将该选择固定到该 work 的 `TenantRuntimePlan`；客户端传入的 engine 名称只能作为候选设置，不能直接绕过服务端 catalog、权限和状态检查。
- `default` 与 `rachael` 都不是外层 Agentic RAG 的强制数据源。选中 `default` 时进入上面的 `DefaultMemoryEngine → Retriever` 链路；选中 `rachael` 时进入 Rachael 自己的 engine/retrieval 链路。只有未来需要把多个 engine 的结果合并为一次召回时，才重新评估外层多源编排、融合和统一重排。
- 外层 `Evaluator` 即使未来启用，也属于检索结果质检、重试或 query rewrite，不是 memory item reranker；不能把它记作“已经有 reranker”。

### 4.3 Memory engine 插件与 WebChat 选择

Pilot 把完整 memory engine 视为一种可插拔的产品能力，而不是把所有引擎强行拆成同一条召回链路。首版纳入两个平台内置、管理员审核和部署的实现：

| engine/plugin | 定位 | 首版策略 | 数据与能力边界 |
| --- | --- | --- | --- |
| `default` | 通用语义记忆引擎，使用 `DefaultMemoryEngine` 和 `Retriever` | 新 tenant 默认启用；作为召回质量基线 | 按 tenant 隔离；保留自身 ingest、context retrieval、memory tools 和 inspector 契约 |
| `rachael` | 独立的 Rachael 记忆引擎和检索诊断链路 | 已安装但默认不选；由 tenant policy 允许后提供选择 | 不复用 `default` 的 store/retriever；按 tenant 隔离；Rachael 专属诊断只在 active engine 和 admin 权限允许时暴露 |

两者都通过统一 `MemoryPlugin` / `MemoryEngine` 契约接入 runtime。插件可以拥有不同的存储、Embedding、召回、写入和诊断实现，但必须统一接受 `tenant`、`MemoryQuery`、ingest/mutation 请求和 capability 声明。这样 WebChat 可以切换产品策略，而 AgentLoop 不需要知道某个引擎的内部实现。

WebChat 的选择流程固定为：

1. 前端从服务端读取当前 tenant 的 allowed memory-engine catalog、active engine、显示名称、状态和能力摘要。
2. 用户选择 `default` 或 `rachael` 后，前端提交 engine id；服务端从认证 principal 派生 tenant，检查该 engine 是否在 tenant catalog、是否 ready、是否允许用户侧切换，再持久化 active binding。
3. 选择结果写入 PostgreSQL control-plane，并提升 `tenant_policy_revision`。客户端字段只是设置请求，不是授权依据。
4. 正在执行的 turn、proactive、consolidation 和 optimizer work 不中途换引擎；下一项 work 取得新的 `TenantRuntimePlan` 后才使用新选择。
5. 切换不自动迁移、合并或删除旧引擎数据。记忆记录和诊断记录至少按 `tenant_id + engine_id` 可追踪；如果未来需要跨引擎迁移，另行设计显式导入、去重、失败回滚和审计。

首版不允许一次 turn 同时调用两个 engine，也不把两个 engine 的结果默认做 union。这样可以先比较两套引擎在同一 tenant、同一问题集上的命中率、延迟、成本和失败降级，再决定是否需要多引擎融合。

### 4.4 `default` 引擎的 Pilot 目标实现

选择 `default` engine 时，Pilot 的默认召回目标是：**DefaultMemoryEngine + raw query + dense semantic/hotness + BM25/hotness + RRF + top-k**。其中 hotness 同时作用于 dense 与 BM25 两条召回 lane，是 `default` 引擎基线的一部分，后续仍然保留，不作为默认移除项或独立消融项。HyDE-style hypothesis、query rewrite 和 reranker 都先做成默认关闭的实验开关，不进入默认路径。`rachael` 不强行复用这条链路，必须通过相同的 `MemoryEngine` 读写、能力声明、tenant scope、审计和延迟指标契约接入。

```text
DefaultMemoryEngine.query()
    ↓
原始 query
    ├── 默认：直接进入召回
    ├── 可选 HyDE-style hypothesis（默认关闭；后续消融）
    │       answer intent 当前实现会生成 event/general 两个 hypothesis
    │       （这是 default engine 内部的 query 扩展，不是 Agentic RAG）
    └── 可选 query rewrite（默认关闭；后续消融）
              ↓
    ┌──────────────────────────────────────────────┐
    │ dense lane                                   │
    │ text-embedding-v3 → PostgreSQL + pgvector    │
    │ semantic + hotness → dense final score       │
    └──────────────────────────────────────────────┘
              +
    ┌──────────────────────────────────────────────┐
    │ sparse lane                                  │
    │ jieba → ParadeDB pg_search BM25              │
    │ BM25 归一化 + hotness → sparse final score   │
    └──────────────────────────────────────────────┘
              ↓
    RRF（按 rank 融合，不直接比较异构 score）
              ↓
    top-k 候选
              ↓
    可选 reranker（默认关闭；后续消融）
              ↓
    memory type 过滤 + 注入预算
```

目标实现的约束：

- `jieba` 和 `pg_search` 是目标方案，不是当前代码事实；实现前需要补齐 PostgreSQL adapter、索引/分词配置和迁移策略。
- hotness 不是独立召回通道，而是 dense 与 BM25 两条 lane 的分数增强项；它属于 Pilot 默认基线，后续必须保留。dense lane 使用 semantic + hotness，BM25 lane 先做 query-local score normalization，再与 hotness 融合为 sparse final score；应分别保留 semantic、BM25 raw、BM25 normalized、hotness、dense final score、sparse final score 和 `rrf_score`，避免把它们混为一个 `score`。
- RRF 应继续以稳定 item id 合并两路候选；dense、BM25、RRF 候选数与最终注入数量分开配置，并记录每阶段候选数、item id、rank 和 score。RRF 的输入排名应分别来自 dense final score 与 sparse final score，而不是直接比较 cosine、BM25 和 hotness 的原始数值。
- 默认路径应保持 HyDE-style hypothesis 和 query rewrite 都关闭。当前 `answer` intent 已有 HyDE 行为，但目标实现必须增加显式开关，并确保 default-only Pilot 不因兼容旧逻辑而隐式调用。可以先实现两者的接口、配置开关和评测记录。
- HyDE-style hypothesis、query rewrite 与 reranker 是三个独立实验变量：HyDE 根据 query 生成假设文本参与召回；query rewrite 修改检索 query 后重新召回；reranker 对已经召回的 RRF 候选逐条重排。三者默认均关闭。
- reranker 的输入应是 RRF 截断后的候选；启用 reranker 后，最终注入顺序和过滤应明确使用 reranker 结果，不能继续无意间按 dense `score` 或旧的 RRF 顺序覆盖排序。
- 建议保留以下消融矩阵：A（raw query + dense semantic/hotness + BM25/hotness + RRF + top-k）；B（A + HyDE-style hypothesis）；C（A + query rewrite）；D（A + reranker）；预算允许时再评估 HyDE、query rewrite、reranker 的组合。所有实验都保留 dense 与 BM25 两条 lane 的 hotness，Pilot 默认运行 A。
- 评测至少记录 Recall@k、MRR 或 nDCG、最终注入命中率、P95 延迟、模型成本和失败降级行为，并分别记录 query rewrite、reranker 的开关状态。

> 当前实现基线与上述 Pilot 目标之间存在明确差距：当前 PostgreSQL 路径是 `summary ILIKE OR`，SQLite 路径才有 FTS5 BM25；当前没有 `jieba` 分词、ParadeDB `pg_search`、独立 query rewrite 开关或独立 reranker。当前 default engine 的 `answer` intent 已有 HyDE-style hypothesis，但尚未提供“默认关闭”的目标开关；当前 dense score 已融合 hotness，BM25/keyword lane 尚未融合 hotness，需在目标实现中补齐 BM25 分数归一化与 hotness 融合；Roadmap 只记录目标和迁移方向，不代表目标组件已经完成。

## 5. 一次 Token 登录与可撤销会话

### 5.1 用户体验

1. 管理员优先通过现有 Dashboard 中新增的 Pilot 管理面板创建测试账号和邀请 Token；CLI 保留为应急与自动化入口。
2. 用户第一次访问 WebChat，输入管理员发放的 Token。
3. 前端调用 `POST /api/auth/exchange`。
4. 服务端原子校验 Token：存在、未使用、未撤销、未过期，并确定对应 `account_id` 与 `tenant_id`。
5. 服务端生成新的随机登录会话，数据库只保存会话值的 hash。
6. 服务端通过 `Set-Cookie` 写入 HttpOnly 登录 Cookie。
7. 浏览器后续访问 HTTP API 和 `/ws` 时自动携带 Cookie，不再重复输入邀请 Token。
8. 用户主动退出登录时删除本地 Cookie，并撤销当前登录会话。

这里的“保存登录状态”指**保存登录会话 Cookie**，不是把邀请 Token 长期放在 URL、LocalStorage 或前端日志里。

### 5.2 Token 与登录会话的边界

| 凭据 | 用途 | 生命周期 | 是否可撤销 |
| --- | --- | --- | --- |
| invitation token | 第一次兑换测试账号 | 一次性使用，支持过期 | 是 |
| login session cookie | 后续 HTTP/WebSocket 登录 | 可配置，例如 30 天，支持 idle timeout | 是 |
| admin credential | 管理测试账号和封禁 | 独立于测试用户体系 | 是 |

服务端只保存 `token_hash` / `session_hash`，不保存明文 Token，不把明文写入日志。邀请 Token 兑换必须单次消费且原子化，避免同一个 Token 被两个浏览器同时兑换；它不承诺在响应丢失后返回同一份 session 明文。

### 5.3 账号封禁与永久撤销

临时封禁账号时必须同时完成：

```text
test_accounts.status = suspended
所有 access_tokens.revoked_at = now()
所有 auth_sessions.revoked_at = now()
```

账号创建后先进入 `provisioning`；tenant partition、规范会话和必要 seed 全部 ready 后才进入 `active`，并且只有 `active` 账号才能签发邀请 Token。运行期状态为 `active → suspended → active`，以及从 `provisioning`/`active`/`suspended` 进入终态 `revoked`。解除封禁只恢复账号为 `active`，不会复活旧 Token、旧 auth session 或已取消的 turn；管理员必须重新签发邀请 Token。永久撤销使用 `revoked`，Pilot 不提供原地恢复。

行为要求：

- 新 HTTP 请求返回未授权或禁止访问；
- 新 WebSocket 连接被拒绝；
- 已连接 WebSocket 由当前 Gateway 主动关闭；
- 账号的历史 session/message/memory 不删除，保留审计能力；
- `tenant_id` 始终由服务端账号映射派生，不能信任请求参数。

单进程 Pilot 可维护 `account_id → active WebSocket connections` 注册表。未来多副本时，才增加跨实例撤销通知。

### 5.4 推荐端点

```text
POST /api/auth/exchange       一次性邀请 Token → 登录 Cookie
GET  /api/auth/csrf           当前用户 session-bound CSRF token
POST /api/auth/logout         撤销当前登录会话
GET  /api/auth/me             返回当前账号的最小身份信息
GET  /api/memory/engines       返回当前 tenant 允许使用的 memory engine catalog 与 active engine
PUT  /api/memory/engine        更新 active memory engine（首版允许 `default` / `rachael`）
POST /api/admin/auth/exchange 管理员 recovery token → admin Cookie
GET  /api/admin/auth/csrf     当前 admin session-bound CSRF token
POST /api/admin/test-accounts 发放测试账号/邀请 Token
GET  /api/admin/test-accounts 查看账号状态
POST /api/admin/test-accounts/{id}/suspend   临时封禁账号
POST /api/admin/test-accounts/{id}/unsuspend 解除封禁
POST /api/admin/test-accounts/{id}/revoke    永久撤销账号
POST /api/admin/tokens/{id}/revoke           撤销单个邀请 Token
WS   /ws                     只接受有效登录 Cookie
```

管理端点必须使用独立 admin credential，不能用普通测试用户 Token 代替。Dashboard 只是这些管理 API 的现有 UI 宿主，不得绕过服务端鉴权、审计或 tenant 边界。

### 5.5 复用现有 Dashboard 作为管理端

Pilot 不新建第二套管理后台，直接复用当前 React Dashboard 的宿主页面、统一设计系统和插件面板扩展机制。当前 Dashboard 的实际使用方式仍以 owner/single tenant 为中心；前端虽然已有 `activeTenant` 和向 `/api/dashboard/*` 注入 `tenant_id` 的基础，但这只算 tenant 路由基础，不等于已经完成安全的多用户切换或全局总控制台。

目标 Dashboard 分成两个工作层级：

```text
现有 React Dashboard
    ├── 用户视图（tenant-scoped）
    │     ├── 从测试账号选择器切换当前用户
    │     ├── 复用当前单用户 sessions / proactive / logs / metrics
    │     ├── 复用 memory 与其他 plugin panel
    │     └── 查看该用户的 Token、会话、Telegram 绑定与在线状态
    │
    └── 全局总控制台（admin-global）
          ├── 全部账号状态与在线规模
          ├── LLM input/output/cache-hit token 消耗
          ├── LLM prompt cache 与应用缓存命中率
          ├── 请求量、错误率、P50/P95 延迟
          ├── WebSocket 在线数、队列 backlog 与限流情况
          └── 按时间、用户、channel、model 下钻
```

用户视图的原则是“把目前针对单用户的 Dashboard 原样 tenant 化”：切换用户后，现有 sessions、proactive、logs、metrics、memory 和插件管理页面都改为读取该账号对应的 `tenant_id`，而不是另外实现一套用户详情页面。账号选择器提交 `account_id`，服务端根据可信账号映射解析 `tenant_id`；不能把浏览器提交的任意 `tenant_id` 直接当作授权依据。切换用户和查看敏感详情均写入管理员审计日志。

全局总控制台只展示跨 tenant 聚合结果，默认不混合展示不同用户的原始消息或记忆内容。聚合指标至少包括：

- LLM 请求数、成功/失败数、模型分布和 P50/P95 turn latency；
- `input_tokens`、`output_tokens`、`cache_hit_tokens` 及统一口径的 cache hit ratio；
- 已接入应用缓存的 hit、miss、hit ratio，按 cache name 分组；
- 当前 WebSocket 在线数、活跃账号数、`asyncio.Queue` backlog、限流和拒绝次数；
- 按账号、tenant、Telegram/WebChat channel、模型和时间窗口过滤或下钻；
- 记忆检索的 dense/BM25 候选数、RRF 后候选数、最终注入数和各实验开关状态，作为召回链路观测项，不与 cache hit ratio 混为同一指标。

计划增加的账号管理能力：

- 创建和查看 `test_account`；
- 签发一次性 invitation token，并只在创建成功时展示一次明文；
- 查看 Token 的状态、过期时间、兑换时间和撤销状态；
- 查看账号的 auth session、`last_seen_at`、绑定的 Telegram identity 和当前 WebSocket 在线状态；
- 执行账号封禁、Token 撤销、登录会话撤销和解除封禁；
- 记录管理员、操作时间、目标账号和撤销原因，危险操作要求二次确认；
- Dashboard 不显示 token/session hash，不在浏览器日志或持久化状态中保存邀请 Token 明文。

Dashboard 管理页面调用 `/api/admin/*`，tenant-scoped 页面调用经过服务端授权的 `/api/dashboard/*`，全局控制台使用独立的聚合/metrics API。所有授权和状态变更均由服务端完成。CLI 的 `issue`、`list`、`revoke`、`expire` 继续保留，作为 Dashboard 不可用时的恢复通道以及脚本化管理接口。

**管理端验收条件**：管理员可以在现有 Dashboard 内完成账号签发、用户切换、状态查询和封禁；切换用户后所有 tenant-scoped 页面只显示目标用户数据；总控制台可以查看跨用户的缓存命中率、Token 消耗和运行状态；Dashboard 不可用时仍可通过 CLI 执行关键操作；普通测试账号无法访问管理视图、其他 tenant 数据或全局管理 API。

### 5.6 WebChat 与 Telegram Bot 双入口及对话同步

在目标架构中，WebChat 是 Pilot 的正式客户端，Telegram Bot 是外部平台接入通道。WebChat 实现后，两种入口必须允许同一测试账号同时活动：

```text
test_account
    ├── WebChat auth session
    └── Telegram identity binding
```

- Telegram 身份必须通过管理员预绑定，或通过一次性绑定码完成与 `test_account` 的可信关联；不能仅凭客户端提交的 user/chat 参数绑定。
- PostgreSQL 保存统一、规范的 conversation/message stream；每条消息至少保留 `account_id`/`tenant_id`、`source_channel`、`source_message_id`、创建时间和稳定的顺序游标。
- WebChat 首次加载读取该账号绑定 Telegram 身份的历史消息；Telegram Bot 通道收到的新消息通过统一消息流实时推送到 WebChat。
- WebChat 断线重连时按稳定 message id 或 sequence 游标补拉遗漏消息，不能只依赖在线 WebSocket。
- Telegram Bot 入站写入和 WebChat 同步必须幂等，避免 Bot API 重试、重连或重复投递造成重复消息。
- 账号与 `tenant_id` 必须由服务端认证和绑定关系派生；WebChat 不能通过请求参数访问其他账号的 Telegram 对话。
- Pilot 默认共享规范会话历史，但不自动把每条 WebChat 消息镜像发送到 Telegram；跨端消息镜像/双向广播作为独立策略后续评估，以避免重复回复和通知轰炸。

**同步验收条件**：WebChat 会话与 Telegram Bot 通道可同时活动；Telegram Bot 收到的新消息能在 WebChat 实时出现；WebChat 重连后能补齐遗漏消息；历史消息不重复且两种入口中的顺序一致；不同账号无法访问对方 Telegram 对话；启用 WebChat 不会停用或降级 Telegram Bot 通道。


### 5.7 人设、关系状态与多租户配置边界

Pilot 的目标不是重新设计单体用户体验，而是把当前单体已经形成的人设、关系和交互体验安全地拓展到多个 tenant。除非后续有明确的体验需求，本节以当前单体的实际语义为基线，不额外引入复杂的人格参数、关系状态机或独立的角色编排平台。

#### 5.7.1 配置分层原则

当前单体的人设配置主要由 [agent.persona] 下的 identity、personality_rules 和 self_model 三块自由文本组成。多租户化时保留这套语义，但调整其归属：

```text
RuntimeInvariantPromptBlock
    代码维护的运行时不可违反规则
    工具真实性、时间处理、安全、记忆纠错、权限和 tenant 隔离

PersonaProfilePromptBlock
    当前单体的 identity + personality_rules 语义
    用户首次登录时一次性设置；描述身份、人格、语言和表达风格

RelationshipStatePromptBlock
    当前单体 SELF.md 中随用户和互动演化的部分
    按 tenant 隔离；保留单体的关系体验和动态变化

ChannelPolicyPromptBlock
    Telegram、WebChat 等 channel 的渲染和传输约束
    由 channel adapter / plugin 维护
```

这四层是 prompt 组装和责任边界，不要求第一版把人格拆成大量结构化字段。特别是第一版不新增 Relationship Policy、Boundaries、Adaptive Style 等强制章节，也不把人格改造成“冷淡程度 / 亲密度 / 占有欲”等参数表。用户首次登录时可以直接编辑完整 PersonaProfile 文本，也可以选择管理员提供的可选 Persona；提交后形成该 tenant 的独立快照，用户侧不再提供二次修改入口。

`config.toml` 不再作为每个 tenant 的长期人设正文存储。确定采用以下兼容与存储语义：

- PostgreSQL 是多租户 Pilot 中 PersonaProfile 与 RelationshipState 的规范 source of truth，两者都必须按 `tenant_id` 隔离；
- PersonaProfile 保存 tenant 首次登录时提交的完整文本快照；RelationshipState 保存当前单体 `SELF.md` / `self_model` 语义对应、可随互动演化的 tenant 状态；
- 管理员可以新增、停用可选 Persona 模板；tenant 选择模板时复制完整内容形成独立快照，管理员后续修改模板不会静默改变已有 tenant；
- `config.toml` 只提供实例级默认 PersonaProfile、当前单体兼容和迁移 seed，不作为多租户运行时的 tenant 正文；
- 不采用“数据库保存 PersonaProfile、tenant workspace 文件保存 RelationshipState”的运行时双重规范源；Markdown 只可作为调试、导出或备份形式，不能与数据库同时争夺最终解释权；
- 单体 / SQLite 模式可以继续通过现有文件 adapter 使用 [agent.persona] 与 `SELF.md`，以保持当前体验；
- 最终 prompt 必须明确绑定 `tenant_id` 对应的人设与关系状态，不能使用进程级全局变量作为唯一来源。

逻辑数据边界按模板和两类 tenant 当前状态表达，例如 `persona_templates`、`tenant_persona_profiles`、`tenant_relationship_states`。这里沿用当前单体的“只读取当前值”模型，不额外建设 Persona/Relationship 历史版本链；具体表名可以在实现 change 中调整，但两类 tenant 状态都以 PostgreSQL 当前记录为准。

#### 5.7.2 SELF.md 的迁移与演化语义

`self_model` 可以继续作为新用户的初始化 seed，但 seed 的具体内容以当前单体实际使用的 `SELF.md` / `self_model` 语义为准，不为了多租户化另造一套关系模板。初始化时复制 seed；之后每个 tenant 的关系状态独立演化，不应因为全局配置变化而静默覆盖已有状态。

`SELF.md` 不被视为不可变文件。按照当前单体实现，PersonaProfile 与 RelationshipState 的行为不同：PersonaProfile 对应 `identity + personality_rules`，一次性人设设置流程提交后作为该 tenant 的当前固定配置；RelationshipState 对应当前 `SELF.md`，Memory Optimizer 可以随着互动原地更新当前内容。关键约束是：

- 用户只在首次登录的一次性人设设置流程中直接设置一次 PersonaProfile；提交后用户侧锁定，日常运行不自动改写 PersonaProfile；
- RelationshipState 按当前单体 `read_self()` → optimizer 计算 → `write_self(updated)` 的语义更新：数据库中直接覆盖该 tenant 的当前状态，不建设产品级 revision 链；
- PersonaProfile、RelationshipState 和运行时规则在 prompt 中有可识别的来源；
- 更新必须绑定当前 tenant，不能跨用户共享或污染，并写入专门的 Persona 审计记录，至少包含来源、更新时间和触发 turn，但不复制保存每一版完整正文；
- 同一 tenant 的更新必须经过 tenant 串行 lane / maintenance lock，并在一个数据库事务内完成，避免两个 optimizer 同时覆盖；既然写入者被约束为单写者，Pilot 不额外引入 compare-and-swap；
- 用户侧不提供 RelationshipState 历史浏览或回滚入口。异常恢复使用常规 PostgreSQL 备份/PITR，而不是另建 Persona 历史版本产品；
- RelationshipState 随互动变化是正常能力。只有缺少当前 tenant 对话依据、违反运行时规则或跨 tenant 的错误变化才需要被拦截。

#### 5.7.3 Prompt 组装与单体体验保持

主对话、Proactive 和 Drift 应使用同一个 PersonaProfile / RelationshipState 解析结果。admin/debug 模式可以展示 prompt source breakdown，也就是“最终 prompt 由哪些区块组成”的调试清单；它只展示区块来源和允许披露的内容，不等于展示模型隐藏推理。该清单能够区分以下来源：

1. RuntimeInvariantPromptBlock；
2. PersonaProfilePromptBlock；
3. RelationshipStatePromptBlock；
4. ChannelPolicyPromptBlock。

现有 identity、personality_rules、self_model 的内容和优先关系先以当前单体行为为准；本阶段不因为引入多租户而强行改变语言、称呼、句式或情绪表达。用户首次设置人设后，目标是让对应 tenant 保持自己的 PersonaProfile，并让 RelationshipState 按当前单体语义继续演化，而不是让所有 tenant 共享一个进程级 Persona 全局变量。

RelationshipState 的系统更新不修改已经开始执行的 turn：当前 turn 继续使用组装时取得的不可变 prompt snapshot，写入成功后从该 tenant 的下一轮 turn 开始重新解析并注入新的 RelationshipStatePromptBlock。PersonaProfile 在一次性人设设置流程提交后保持当前固定值。这样既不要求重启进程，也不会让同一轮执行过程中途改变关系状态。

### 5.8 Agent 工具调用的多租户隔离与作用管理

Pilot 不把“工具参数中带有 `tenant_id`”视为多租户隔离。工具调用必须同时满足身份、工具可见性、资源范围和副作用四道边界：

```text
认证身份
    ↓
服务端派生 ToolExecutionContext
    ↓
当前 tenant 的 ToolCatalog
    ↓
ToolPolicy / capability 检查
    ↓
资源解析与 tenant 过滤
    ↓
副作用确认、幂等、配额检查
    ↓
工具运行时
    ↓
审计、脱敏和结果限制
```

#### 5.8.1 执行上下文是唯一可信的 scope 来源

建议引入不可变的 `ToolExecutionContext`，逐步替代共享 `ToolRegistry.set_context()` 作为权限依据。上下文至少包含：

```python
@dataclass(frozen=True)
class ToolExecutionContext:
    request_id: str
    account_id: str
    tenant_id: str
    session_id: str
    turn_id: str
    channel: str
    chat_id: str
    principal_type: str
    capabilities: frozenset[str]
    resource_scope: ResourceScope
    path_resolver: TenantPathResolver
```

约束如下：

- `account_id`、`tenant_id`、`session_id`、`turn_id` 均由服务端从认证身份、可信 channel binding 和当前 turn 派生；
- LLM、WebChat 客户端、Telegram payload 和 MCP 参数不得覆盖或提供这些字段；
- `tenant_id` 不作为普通工具参数暴露给模型；工具需要租户范围时从 context 读取；
- 内部 context 不得原样转发给 MCP。发送给 MCP 的只能是经过校验的业务参数；
- 工具执行 API 应逐步从 `execute(name, arguments)` 演进为 `execute(name, arguments, context=...)`；
- 在迁移完成前，`ToolRegistry._context` 只能作为兼容性上下文，不能作为跨租户授权的最终依据；
- 工具执行器必须在每次调用时显式检查 context，不能假设“当前 Registry 恰好属于当前请求”。

当前代码基线中，`agent/lifecycle/phases/before_reasoning.py` 会把 `tenant_id` 写入 `ToolRegistry` 上下文，而 `agent/tools/registry.py` 的合并顺序曾允许 arguments 覆盖默认上下文；这只能作为兼容路径，P1 认证和工具隔离完成前必须修正为“系统 context 优先且不可覆盖”，最终移除对共享可变 context 的授权依赖。

#### 5.8.2 工具目录按全局、tenant 和管理员分层

全局 `ToolRegistry` 不能单独承担多租户隔离。Pilot 目标采用以下逻辑分层：

```text
GlobalToolRegistry
    ├── 内置工具
    ├── system MCP
    └── admin-only 工具

TenantToolCatalog(tenant_id)
    ├── 当前 tenant 自己的 MCP
    ├── 当前 tenant 获授权的插件工具
    └── tenant-scoped 内置工具 view

AdminToolCatalog
    └── 账号、MCP、插件、全局指标和运行时管理工具
```

模型每轮只接收当前 `ToolExecutionContext` 对应的工具 schema。工具是否出现在 schema 中只是第一道防线，真正执行前仍必须重新检查：

- 当前账号是否仍然有效、未封禁；
- 当前 tenant 是否拥有该工具或 MCP binding；
- 工具是否仍启用，是否刚刚被撤销；
- 当前 principal 是否具备所需 capability；
- 当前调用是否满足确认、配额和资源限制。

`ToolRegistry.fork()` 可以继续用于构造工具集合 view 或排除来源，但不能单独当作 tenant isolation：fork 后可能复用相同的有状态 Tool、MCP client、scheduler、session store 或 workspace 引用。

#### 5.8.3 工具作用等级与默认开放策略

每个工具必须声明 effect policy，而不是只依赖通用的工具名或粗粒度风险描述。建议至少区分：

```text
read-only
 tenant-local-write
 external-read
 external-write
 network
 process-exec
 admin
```

| 工具类别 | 普通 tenant | 管理员 | Pilot 决策 |
| --- | --- | --- | --- |
| `recall_memory` / `memorize` / `forget_memory` | 允许，仅当前 tenant | 允许，可审计下钻 | 保留，强制 tenant 过滤 |
| `search_messages` / `fetch_messages` | 允许，仅当前账号/tenant 会话 | 允许按授权范围查看 | 保留，禁止任意 `session_key`/`chat_id` |
| `read_file` / `list_dir` | 允许，仅 tenant resource root | 可访问指定 tenant | 保留，必须使用路径解析器 |
| `write_file` / `edit_file` | 允许，仅授权目录 | 可访问指定 tenant | 限制目录、类型、大小和额度 |
| `read_image_vision` | 允许，仅 tenant 文件 | 可访问指定 tenant | 保留，复用文件 scope |
| `message_push` | 允许，仅服务端绑定目标 | 可使用授权目标 | 保留，禁止任意目标地址 |
| scheduler / reminder | 允许，仅当前 tenant | 可查看和管理全局 | 增加 owner、目标 binding 和取消权限 |
| web search / fetch | 可选，限流和 SSRF 防护 | 允许 | 按网络策略开放 |
| `shell` | 禁止 | 可用，严格审计 | 普通 tenant 关闭 |
| `spawn` / `spawn_manage` | 严格限额或暂不开放 | 可用，可全局管理 | 先做 owner 和资源配额 |
| `peer_agent` | 禁止 | 仅内部或管理员 | Pilot 普通 tenant 关闭 |
| `mcp_add` / `mcp_remove` | 允许管理自己的 MCP，不允许修改全局 MCP | 允许管理 system MCP | 使用 tenant control plane |
| plugin install / config | 禁止 | 允许 | admin-only |
| drift flow 文件、shell 和发送工具 | 遵循同一 scope | 按能力开放 | 不得绕过主 Agent 规则 |

对于工具是否允许后台执行、是否要求用户确认、最大并发、输入输出大小和每日额度，也应成为 `ToolPolicy` 的一部分。

#### 5.8.4 用户可以自由管理自己的 MCP，但必须使用 tenant namespace

Pilot 区分两类 MCP：

```text
System MCP
    管理员配置
    workspace 级或进程级
    服务平台能力

Tenant MCP
    用户自己添加
    只对当前 tenant 可见
    独立配置、binding、runtime 和审计范围
```

当前 workspace 级 `mcp_servers.json` 继续保留为 system MCP 配置，不允许普通用户通过 `mcp_add`/`mcp_remove` 直接修改。用户自己的 MCP 建议进入数据库 control plane：

```text
tenant_mcp_servers
    id, tenant_id, display_name, transport, endpoint/command,
    runtime_type, status, created_by_account_id, config_revision,
    last_connected_at, last_error

tenant_mcp_tools
    id, mcp_server_id, remote_name, public_name, input_schema,
    effect_class, enabled, requires_confirmation

tenant_mcp_grants
    tenant_id, mcp_server_id, tool_id, allowed,
    confirmation_mode, network_policy, rate_limit, daily_budget
```

`mcp_add` 的目标行为不是“立刻把工具注册进全局 Registry”，而是：

```text
提交声明
    ↓
校验 transport、endpoint/command、env、cwd 和资源限制
    ↓
创建当前 tenant 的 MCP 记录
    ↓
启动临时 validation runtime
    ↓
连接并拉取工具 schema
    ↓
分类工具作用、检查 schema 和描述大小
    ↓
用户选择或确认启用的工具
    ↓
发布到该 tenant 的 ToolCatalog
```

用户可以自由添加远程 HTTPS MCP。远程 MCP 必须经过 URL、DNS、重定向和内网地址检查；禁止访问 localhost、私网网段、云 metadata 地址、数据库地址和 Docker socket。

用户如果要添加本地 stdio MCP，必须运行在独立 sandbox/container 或等效受限 runtime 中。Pilot 不把任意用户命令直接交给 NexusCompanion 所在宿主机执行。sandbox 至少要分配：

- tenant 独立的可写目录；
- 只读基础运行时；
- CPU、内存、进程数和 wall-clock 限额；
- 明确的网络 egress 策略；
- 仅允许的环境变量和 secret reference；
- 最大输入、输出和并发调用数；
- 独立的 start、stop、restart、disable 和 kill 生命周期。

MCP 工具的内部路由键不能只有 `server_name`，至少需要：

```text
tenant_id + mcp_id + remote_tool_name
```

公开工具名可以保持类似 `mcp_calendar__create_event`，但调用时必须通过当前 tenant 的 binding 定位到具体 runtime，不能因为两个 tenant 使用相同 server name 就共享 client 或互相影响。

#### 5.8.5 路径和资源引用管理

工具不直接接受任意物理路径。tenant 文件资源建议采用：

```text
file_id
    → tenant_id
    → resource_type
    → storage_key
    → physical path
```

对于用户操作的普通文本文件，可以接受相对逻辑路径，例如 `notes/today.md`，但 root 必须由服务端根据 context 和资源类别选择：

```text
workspace/
├── tenants/
│   ├── <tenant_id>/
│   │   ├── attachments/
│   │   ├── scratch/<turn_id>/
│   │   ├── exports/<job_id>/
│   │   └── mcp/<mcp_id>/
│   └── ...
├── system/
├── logs/
└── config/
```

普通 tenant 不得访问其他 tenant、`system`、`logs`、`config` 或未经授权的资源类别。路径解析器应提供类似接口：

```python
class TenantPathResolver:
    def attachments_root(self, context: ToolExecutionContext) -> Path: ...
    def scratch_root(self, context: ToolExecutionContext) -> Path: ...
    def exports_root(self, context: ToolExecutionContext, job_id: str) -> Path: ...
    def mcp_root(self, context: ToolExecutionContext, mcp_id: str) -> Path: ...
    def resolve_relative(self, root: Path, path: str) -> Path: ...
```

当前 `agent/tools/filesystem.py::_resolve_path()` 的 `allowed_dir + resolve()` 是基础能力，tenant-facing 工具还必须满足：

- `allowed_dir` 只能由服务端生成，不能由模型或客户端传入；
- 拒绝任意绝对路径；
- 拒绝 `..` 逃逸和符号链接逃逸；
- 写入前后都校验最终路径，使用临时文件和原子替换；
- 限制文件大小、目录深度、扩展名、MIME 和单次读取字节数；
- 上传文件、scratch 文件和 export 文件分别管理生命周期和清理策略；
- 不开放 shell 创建链接、挂载点或绕过 resolver 的旁路能力。

#### 5.8.6 外部作用、确认、幂等和审计

“用户自己的 MCP”只代表用户拥有该 MCP 的配置或外部账号绑定，不代表该 MCP 可以访问宿主机或任意平台资源。以下作用必须单独管理：

- 只读外部查询：允许但要限流、审计和限制返回量；
- tenant 内本地写入：只允许当前 tenant 的资源 root，并受大小和额度限制；
- 外部系统写入：默认要求用户确认，或者使用可撤销、可过期的持久授权；
- 不可逆、高成本或批量操作：Pilot 默认拒绝，后续再引入管理员批准和更高等级确认；
- 进程执行和全局配置变更：普通 tenant 默认禁止。

确认记录不能只表示“用户同意使用这个 MCP”，而应绑定具体调用：

```text
tenant_id
account_id
mcp_binding_id / tool_id
arguments_hash
expires_at
one_time
```

外部写操作还必须支持：

- 幂等键，避免模型重试或 WebSocket 重连造成重复写入；
- 超时、重试上限、断路和明确的失败状态；
- 调用前的 quota / budget 检查；
- 参数脱敏后的审计记录；
- 必要时的补偿或撤销操作。

建议记录 `mcp_audit_events` 或统一 `tool_audit_events`：

```text
request_id, account_id, tenant_id, session_id, turn_id,
tool_binding_id, tool_name, effect_class, status,
arguments_redacted/arguments_hash, duration_ms, error_code, created_at
```

不得把 API key、OAuth token、完整文件内容或其他敏感参数原样写入日志、浏览器状态、模型上下文或持久化审计记录。

#### 5.8.7 MCP、后台任务和账号状态联动

`mcp_remove` 不是简单注销几个工具名。推荐状态流转为：

```text
active
  ↓ remove/revoke
revoking
  ↓ 停止新调用，等待或取消运行中调用
runtime stopped
  ↓ 注销 tenant catalog，保留审计和声明
revoked
```

用户删除或禁用自己的 MCP 时：

- 当前 tenant 立即看不到该 MCP 工具；
- 新调用全部拒绝；
- 正在执行的调用按策略等待、取消或终止；
- 远程连接或本地 runtime 被关闭；
- 其他 tenant 的同名 MCP 不受影响；
- 声明、binding、调用审计和错误记录保留；
- secret 可以单独撤销。

`spawn`、`task_output`、`task_stop`、scheduler 和 MCP runtime 都必须保存并检查：

```text
job_id
account_id
tenant_id
session_id
turn_id
origin_channel
origin_chat_id
created_by
status
```

普通账号只能查看、轮询和取消自己 tenant 的任务；管理员才可以跨 tenant 管理。后台任务在真正执行前必须重新检查账号状态、tenant 状态、工具 binding、授权有效期和额度。

账号封禁时，除了拒绝新的 HTTP/WebSocket 请求，还应按 tenant 处理已有的：

```text
WebSocket、spawn job、scheduler、MCP runtime、外部工具调用和 outbound push
```

#### 5.8.8 Pilot 最小工具隔离闸门

在 WebChat 面向受邀用户开放前，至少完成：

1. 建立 `ToolExecutionContext`，包含 `account_id`、`tenant_id`、`session_id`、`turn_id`；
2. 禁止模型和客户端传入可信 identity、tenant、root、目标 chat 或权限字段；
3. memory、message、session、attachment 和 dashboard 查询强制使用 tenant-bound repository 或等效服务端过滤；
4. 所有文件工具绑定 `TenantPathResolver`；
5. 普通 tenant 关闭 `shell`、`peer_agent`、plugin 管理和全局 MCP 管理；
6. 用户 MCP 使用 tenant namespace、独立 binding、runtime 和工具 catalog；
7. `message_push` 只能使用服务端已绑定并授权的目标；
8. 后台任务保存 owner tenant 并在 `output/stop/execute` 时重新校验；
9. drift flow 和插件自带的 shell、filesystem、send 工具复用同一套 scope/effect policy；
10. 每次工具调用具备 tenant、account、turn、工具、作用等级、状态和耗时审计字段；
11. 增加跨租户负向测试和并发交错测试，证明共享 Registry context 不会串租户；
12. 用户自己的 MCP 不能读取平台环境变量、其他 tenant 目录、数据库连接或 Docker/socket 资源。

本节的路线图决策是：**普通 tenant 可以自由添加和管理自己的 MCP，但只能在平台分配的 tenant runtime、文件、网络、凭据和资源配额内运行；Shell、全局 workspace、全局 MCP、Peer Agent 和插件管理保持 admin-only 或 internal-only。**

### 5.9 编码前必须冻结的实现决策

本节是后续阶段计划和 OpenSpec change 的**设计门禁**。它来自对当前实现的复查，不表示这些能力已经完成。凡是会改变身份映射、表约束、协议游标、恢复语义或授权边界的事项，都必须先在 design/spec 中冻结，再开始对应迁移、接口或运行时代码；不能一边写代码一边由局部实现暗自决定。

#### 5.9.1 当前代码暴露出的硬冲突

| 决策域 | 当前代码事实 | Pilot 冻结结论 | 未冻结时禁止开始 |
| --- | --- | --- | --- |
| 账号、tenant 与规范会话 | `InboundMessage.session_key` 和 `tenant_id_for_channel()` 都仍以 `channel:chat_id` 派生；WebChat 与 Telegram 会自然形成不同 key | `account_id` 是登录主体，`tenant_id` 是资源边界；Pilot 每个 tenant 恰有一个 `canonical_conversation_id`，所有 channel binding 映射到该会话；旧单体数据留在 SQLite | WebChat channel adapter、Telegram 绑定、Pilot 新身份初始化 |
| Dashboard 鉴权 | 前端 `activeTenant`/`tenant_id` 只是路由参数；现有 Dashboard API 仍有 owner/single-user 假设 | Dashboard 的 tenant 选择只影响管理员查看范围，不能成为授权来源；普通用户和管理员使用分离的服务端 principal/session | `/api/admin/*`、跨 tenant Dashboard 页面 |
| WebSocket replay | `ConversationRuntime` 的 subscriber queue/history 在内存，进程重启后无法补回流式事件 | 规范消息和终态使用 PostgreSQL durable stream；token delta 只作在线展示，断线后按 message/turn 终态补拉，不承诺逐 token 重放 | WebSocket 消息协议、前端重连逻辑 |
| 队列与串行键 | `MessageBus` 队列无界；`PassiveMessageWorker` 当前按 `session_key` 建 lane，不是按 tenant；不同 session 可并行 | admission key 固定为 `tenant_id`；同 tenant 单 active work，不同 tenant 可并行；所有队列有界且定义 overload 行为 | 新 queue adapter、并发调度和限流 |
| durable control plane | PostgreSQL 模式下 session/message 已走 PG，但 turn control 仍可落到 SQLite `turn_audit.db`；lane/event queue 在内存 | P0.5 dev-only 可保留兼容路径；P1 面向受邀用户前，账号、会话、规范消息、turn/tool/work 状态统一以 PostgreSQL 为 durable source of truth | 公网 Pilot、重启恢复承诺 |
| Tool context | `ToolRegistry` 有共享可变 `_context`；现有参数合并路径不能作为多租户授权边界 | 采用 immutable catalog + per-call `ToolExecutionContext`；系统派生字段优先且不可被模型/客户端参数覆盖，`set_context()` 不再承担授权 | tenant-facing 工具、用户 MCP |
| Persona | 进程级 `NEXUS_IDENTITY`/`PERSONALITY_RULES` 会在启动时被全局修改；PG memory 已有 tenant-specific SELF 基础；当前单体 `SELF.md` 由 optimizer 原地覆盖 | PersonaProfile/RelationshipState 按 tenant 保存当前值并在 turn 开始时形成 prompt snapshot；PersonaProfile 在一次性人设设置流程提交后固定，RelationshipState 沿用单体的当前值覆盖语义，并由 tenant 串行 lane / maintenance lock 防止并发写入 | 多 tenant prompt、optimizer 写入 |
| 表约束与序号 | `sessions.key` 是全局主键；message `seq` 和 `next_seq()` 仍依赖应用层约定，尚无账号、binding、stream、attachment ownership 模型 | 在 Pilot DB design 中先冻结主键、外键、唯一约束、软删除和序号分配；sequence 必须由数据库事务原子分配 | Alembic initial schema、repository 改造 |


本轮实现复查的主要锚点如下，后续 design 不得脱离这些现状自行假设：

- identity/session：`bus/events.py`、`infra/storage/tenancy.py`、`session/manager.py`；
- queue/runtime/recovery：`bus/queue.py`、`bootstrap/passive_worker.py`、`agent/control/runtime.py`；
- Dashboard/WebChat 宿主：`bootstrap/dashboard_api.py`、`frontend/dashboard/src/api.ts`；
- PostgreSQL session/message：`bootstrap/db/models/session.py`、`bootstrap/db/repository/session_repo.py`、`alembic/versions/`；
- tool/MCP：`agent/tools/registry.py`、`agent/tool_hooks/executor.py`、`agent/tools/filesystem.py`、`agent/tools/message_push.py`、`agent/tools/spawn.py`、`agent/mcp/registry.py`；
- Persona/memory：`agent/persona.py`、`agent/memory_pg.py`、`plugins/default_memory/engine.py`、`memory2/query_rewriter.py`。

#### 5.9.2 Canonical identity 与 Telegram Bot binding

Pilot 冻结以下语义：

- 一个 `test_account`（登录主体）可拥有多个 agent，每个 agent = 一个 `tenant_id` = 一个 `canonical_conversation_id`（各自独立记忆/persona 域）；账号自身不携带 tenant。WebChat auth session 和 Telegram Bot identity binding 都只负责把入口映射到该账号下的某个具体 agent（tenant → 规范会话）。
- 本路线图中的“服务端身份映射”是一个可信查表过程，不是让客户端把 `tenant_id` 传给服务端：adapter 先验证 auth session、Telegram source identity 或其他已登记 principal，再查询 binding 得到 `account_id → tenant_id → canonical_conversation_id`，最后由服务端写入 `WorkEnvelope`。因此攻击者即使修改请求中的 tenant/chat/session 字段，也只能提供待校验输入，不能改变实际数据归属；没有 binding 的请求必须拒绝，不能通过 `DEFAULT_TENANT` 或 session 字符串猜测。
- Telegram 入口继续使用现有 **Telegram Bot API**。首版只把“某个 Telegram 用户与 Bot 的私聊身份”绑定到测试账号；不登录 Telegram 个人账号，也不把群聊绑定为 tenant。每个测试账号最多绑定一个 Telegram 用户身份，同一 Telegram 用户身份也只能绑定一个测试账号。
- 绑定支持管理员预绑定和一次性绑定码；绑定码 10 分钟过期、单次使用，兑换和解除绑定都写审计。解绑不删除历史消息，新绑定不得自动继承另一账号的历史。
- **现有单体数据不迁入 Pilot PostgreSQL。** 旧 `channel:chat_id` session、消息、记忆和其他既有 SQLite 内容继续留在原 SQLite/workspace 中，不生成逐 session mapping 清单，不做 dry-run、backfill、合并或 canonical identity 改写。
- Pilot 的账号与其 agent 由 provisioning 全新创建：首个 agent 即 `test_account → 首个 tenant_id → canonical_conversation_id`，后续可向同一账号添加更多 agent（各带独立 tenant 与规范会话），所有 canonical conversation 从空历史开始；即使绑定的是以前使用过单体 Telegram Bot 的用户，也不会自动继承旧消息、旧记忆或旧 Persona/Relationship 状态。
- 旧 SQLite 是独立的 legacy single-user store，不是 Pilot 的 fallback、第二 source of truth 或双写目标。Pilot 代码不得在 PostgreSQL 查不到数据时回退读取旧 SQLite，也不得把 Pilot 新消息反向写回旧库。
- 如果继续运行旧单体模式，它仍可独立使用自己的 SQLite；但同一个 Telegram Bot token/更新流不能同时由旧单体和 Pilot 消费。切换 Bot 接入时只切入口，不搬历史数据。
- 旧 SQLite 文件及其 workspace 按独立 legacy backup 项保留。未来若确实需要导入历史，必须另开 change，重新定义身份确认、内容范围、去重、sequence 和隐私规则；不属于当前 Pilot 范围。
- 每条入站消息包含服务端生成的 canonical `message_id`，并保留 `source_channel`、`source_identity_id`、`source_message_id` 和 `client_message_id`。Telegram update 以 source identity + source message id 去重；WebChat 以 account + client message id 去重。
- 规范消息 `sequence` 是每个 canonical conversation 各自独立递增的消息序号，并固定从 `0` 开始：会话 A 可以是 0、1、2，会话 B 也可以独立从 0、1、2 开始。当前单体已经有相同的基本思路和起始值：`SessionStore.next_seq(session_key)` 按旧 `session_key` 读取 `sessions.next_seq` 与 `MAX(messages.seq) + 1`，空 session 返回 0，`insert_message()` 再写入该序号，所以不同 session 各自编号。但当前“取号”和“插入”是两个操作，不能视为多进程并发下的最终原子保证。Pilot 保留当前实现的 0-based 编号以减少 adapter 和 contract 差异，把编号范围改为 canonical conversation，并在同一个 PostgreSQL 事务中原子分配 `BIGINT` sequence 和写入消息，用于稳定排序和断线补拉；禁止退回“读取 `next_seq`、在应用内加一、再单独提交”的并发语义。

#### 5.9.3 Auth、admin credential 与浏览器安全

Pilot 冻结以下安全边界：

- 邀请 Token 和登录 session 都使用至少 256-bit CSPRNG 随机值；数据库保存带服务端 pepper 的 HMAC-SHA-256 digest，不把明文凭据写入数据库、日志、审计或前端持久存储。pepper 来自 secret/env，不进入 `config.toml` 或数据库。
- 普通用户 session cookie 使用 `__Host-nexus_session`；admin 使用独立的 `__Host-nexus_admin`，两者不能互换。非 dev 环境固定 `HttpOnly`、`Secure`、`Path=/`、无 `Domain`，`SameSite=Lax`；其中 `Path=/` 的 `/` 是网站根路径，表示这个 Cookie 可用于同一站点的全部 URL 路由（包括 API 和 WebSocket 握手），不是操作系统文件根目录。本地 HTTP 仅允许显式 dev cookie 配置。
- 普通登录 session 默认 idle timeout 7 天、absolute timeout 30 天；管理员 session 默认 idle timeout 30 分钟、absolute timeout 12 小时。所有时间参数可配置，但上线前必须记录最终部署值。
- Pilot 只设一个 admin principal。`CLI bootstrap` 是“第一次在部署服务器终端里初始化管理员”的命令，不是浏览器登录：它创建唯一管理员身份和一份长期 recovery token，明文只显示一次，数据库只保存 digest。浏览器把 recovery token 交给 `POST /api/admin/auth/exchange`，兑换一份短期 admin session Cookie；recovery token 本身不直接作为 Cookie 使用。CLI 还可轮换或撤销 recovery token；不复用测试用户 Token，也不信任 Dashboard 的 `activeTenant`。

Admin CLI 的命令面冻结为：

```powershell
python main.py pilot-admin bootstrap
python main.py pilot-admin status
python main.py pilot-admin rotate-recovery-token
python main.py pilot-admin rotate-recovery-token --force-local
python main.py pilot-admin revoke-sessions --all
python main.py pilot-admin disable
python main.py pilot-admin enable
```

命令语义固定如下：

- `bootstrap` 只能在部署受信主机的交互式 TTY 中运行；仅当 admin principal 尚不存在时成功。它生成 32-byte CSPRNG recovery token，只向当前 TTY 显示一次，数据库仅写带 pepper 的 digest；禁止通过命令行参数、环境变量、配置文件或 stdin 重定向传入/回显 token。
- `status` 只显示 admin enabled 状态、recovery credential revision、最近轮换时间和 active session 数，不显示 digest、token 或 CSRF secret。
- `rotate-recovery-token` 默认要求交互式输入当前 recovery token；`--force-local` 仅用于凭据丢失后的受信主机恢复，不允许由 HTTP API、Dashboard 或非交互式远程任务触发。轮换只让旧 recovery token 立即失效并生成新 token，新 token 只显示一次；现有、未过期且未被撤销的管理员浏览器 session 默认继续有效。token 轮换与浏览器 session 撤销是两个独立操作，只有怀疑凭据或浏览器 session 泄露时才显式执行 `revoke-sessions --all`。
- `revoke-sessions --all` 用于紧急清除所有 admin browser sessions，但不改变 recovery token；`disable` 表示临时关闭管理员网页登录入口：新的 recovery-token exchange 一律拒绝，已经登录的管理员浏览器也立即退出。之后只能在受信主机本地执行 `enable` 重新开放；旧 session 不会自动恢复。
- 所有命令写 admin audit metadata，但日志、shell history、进程参数、环境 dump、数据库和审计都不得出现明文 recovery token。

Admin recovery runbook 冻结为：

1. **Recovery token 丢失**：取得部署主机的 OS/SSH 管理权限，在交互式 TTY 运行 `python main.py pilot-admin rotate-recovery-token --force-local`；将新 token 保存到密码管理器；确认旧 recovery token 已失效并复查轮换审计。现有有效 admin browser sessions 可继续使用；只有同时怀疑泄露时才显式运行 `revoke-sessions --all`。
2. **怀疑 token 或 admin session 泄露**：先运行 `python main.py pilot-admin revoke-sessions --all`，随后运行 `python main.py pilot-admin rotate-recovery-token --force-local`；若仍有异常，执行 `disable` 并关闭公网 Dashboard/Tunnel，按 IP 摘要、时间和 action 复查审计，确认后再本地 `enable`。
3. **数据库恢复**：PostgreSQL 与 pepper/secret 必须恢复到可对应的备份代次；恢复后先撤销备份中复活的 admin sessions。若 recovery digest 无法验证，执行本地强制轮换；最后验证 admin exchange、CSRF、session timeout 和 audit continuity。
- Cookie 认证的 mutation API（会修改数据的 `POST`/`PUT`/`PATCH`/`DELETE` 请求）必须同时检查 `Origin`/`Referer` 和 session-bound CSRF token。服务端先确认请求来自允许的前端站点，再由 `GET /api/auth/csrf` 或 admin 等价端点签发一段与当前登录 session 绑定的随机值；前端只把它放在页面内存，并通过 `X-CSRF-Token` 回传，不写 LocalStorage。这样可阻止恶意网站借用浏览器自动携带的 Cookie 替用户执行操作。`Origin allowlist` 是明确允许访问服务的前端来源清单，来源由协议、域名和可选端口组成，例如 `https://chat.example.com` 或 `https://admin.example.com`。WebSocket handshake 同样使用 Cookie + Origin allowlist；CORS 默认也只允许部署的 WebChat/Dashboard origin。
- Pilot 的 admin HTTP/API 入口默认只允许部署主机本机访问，不通过公网 Tunnel 暴露；如果确需远程管理，必须另开 change，增加受控 VPN/跳板机入口和独立 allowlist，不能仅依赖前端隐藏入口。普通 WebChat 仍可通过公网 Tunnel 暴露，但不得因此放宽 admin route 的网络边界。
- 401 表示没有有效 principal 或 session 已过期；403 表示 principal 有效但账号被封禁、capability 不足或资源不属于当前 tenant。错误响应不得泄露账号、Token 或 binding 是否存在。
- `logout` 默认只撤销当前 session；“撤销账号全部 session”是独立管理操作。账号封禁保留 session/token 记录并写 `revoked_at`，不物理删除审计链。
- Token 兑换在单个数据库事务中锁定并消费 Token、创建 session。若服务器已经创建 session，但网络在浏览器收到 Cookie 前中断，浏览器无法确认是否成功；服务端也不保存一份可供再次取回的原始 session token。此时不尝试“恢复同一份 session 明文”，而是由管理员重新签发新的邀请 Token，旧的未知 session 可撤销或等待过期。

#### 5.9.4 WebSocket 协议、游标与慢消费者

协议 change 在写 Gateway/前端前必须固定以下 envelope：

- handshake 认证成功后服务端先发送 `hello`。这里的 `hello` 不是聊天问候，而是连接初始化消息，用来告诉前端 `connection_id`、当前账号、规范会话、服务器已持久化到的最新消息序号和协议版本；客户端不得在 payload 中声明可信 `tenant_id`。
- 客户端在发送每条用户消息前生成稳定的 UUID `client_message_id`。WebSocket 断线时，客户端可能不知道上一帧是否已经到达服务器，因此重连后会重发；服务端看到相同 `client_message_id` 时返回原 canonical message/turn 结果，不创建第二条消息或第二个 turn。WebSocket 负责传输，`client_message_id` 负责应用层去重，两者不是同一个概念。
- `last_sequence` 是客户端在该规范会话中“最后确认看到的持久化消息序号”，也就是一个书签/游标。例如最后看到 120，重连时就请求 120 之后的消息；服务端返回后续 canonical messages、turn terminal state 和需要展示的 tool terminal state。
- `message.delta` 是非 durable 在线优化；`message.completed`、`message.failed`、`turn.*` 终态和规范消息必须 durable。断线期间 turn 继续执行，重连后恢复最终文本和终态，不补齐每个 token delta。
- “慢消费者”是指浏览器或网络处理事件的速度低于服务端产生事件的速度。每个连接的 outbound buffer 有界：先停止发送可丢弃的 token delta，通知客户端用 `last_sequence` 这个书签从数据库补拉最终消息；如果积压仍持续增长，就用明确的 overload close code 断开，让客户端稍后重连，避免单个连接无限占用内存。
- 协议 spec 必须列出 event type、schema version、ack/idempotency、错误码、WebSocket close code、心跳和 server restart 行为。前后端共用一套 contract fixture/test，也就是同一组示例 JSON 帧、预期响应和错误案例；后端测试与前端测试都读取它，避免一边叫 `latest_sequence`、另一边却实现成别的字段。

#### 5.9.5 Admission、队列容量与 overload policy

- interactive、maintenance 和 per-connection outbound 都使用有界队列；容量是部署配置，但 Pilot 初始默认值冻结如下，不允许保留无界默认：

| 资源 | Pilot 默认容量 | 满载语义 |
| --- | ---: | --- |
| global interactive ingress ready queue | 128 work items | WebChat/HTTP 在 durable acceptance 前返回 overload；已接受的 durable work 不丢失，由恢复扫描重新调度 |
| per-tenant pending interactive queue | 16 messages | tenant 内 active work 仍为 1；第 17 条未接受消息明确拒绝，不无限堆积 |
| global maintenance ready queue | 64 work items | 不拒绝用户消息；按 tenant + maintenance kind 合并或延后 |
| per-tenant maintenance | 每种 maintenance kind 最多 1 个 pending item | consolidation、optimizer、Proactive/Drift tick 等同类任务只保留最新需求或从 durable state 重算 |
| per-WebSocket outbound queue | 256 events | 达到 192 个 event 进入 delta degradation；达到 256 个 event 或累计 payload 1 MiB 时进入 hard overload |

- per-tenant active work 上限固定为 1；全局资源 semaphore 初始默认值为：active LLM turns 30、embedding calls 4、MCP calls 8、process-exec calls 2。semaphore 是异步“许可计数器”：LLM=30 表示最多 30 个 tenant 的交互 turn 同时占用 LLM 执行槽，第 31 个等待 admission/queue；它不是账号总数限制。LLM、embedding、MCP、process-exec 分开计数，是为了防止某一类慢资源把其他资源的并发额度全部耗尽。LLM 调用还必须处理 provider rate limit/429 退避并记录活跃数、等待时间和拒绝率。admission 和状态归属使用 `tenant_id`，不再使用 channel-specific `session_key`。
- interactive 优先于 maintenance。maintenance work 必须可合并、可从 durable state 重算；队列满时优先延后或合并 maintenance，不能挤占 interactive lane。
- interactive overload 必须发生在 5.9.11 的 durable acceptance transaction 之前：HTTP 返回 429 和 `Retry-After`，WebSocket 返回结构化 `overload` 事件；不得先返回 accepted 再丢 work，也不得无限阻塞 channel update handler。Telegram 等可重试 channel 只有在 durable acceptance 提交后才 ack；若未提交则让 provider 重试。
- WebSocket outbound 达到 192 个 event 时只丢弃非 durable `message.delta`，发送 `replay_required` 并要求客户端按 `last_sequence` 补拉；达到 hard overload 后使用明确 close code 断开。canonical final message 和 terminal state 不因连接队列满而删除。
- Proactive、Drift、consolidation 和 optimizer 在进入同一 tenant lane 前重新检查账号状态；有 interactive backlog 时排队或跳过可重算 tick，不打断 active interactive turn。
- 以上数值是 10–30 个测试账号、5–15 个同时在线 WebChat 会话的保守起点；配置可以调整，但 P0/P0.5 压测必须记录峰值、拒绝率、内存占用和调整理由，不能无证据地改成无界或让 maintenance 与 interactive 共用未分级容量。

#### 5.9.6 Durable state 与 restart recovery

P1 公网门禁前必须把以下对象纳入 PostgreSQL durable control plane：账号/token/auth session、provisioning readiness、channel binding、canonical conversation、inbox/inbound idempotency、canonical message/sequence、turn、tool call、background work item、outbox/delivery、显式用户 schedule/execution、tool audit、attachment metadata。实际 attachment bytes 保存到 tenant resource root，元数据和 ownership 落库。

恢复语义固定为：

- final user/assistant message、turn/tool terminal state 和用户必须看到的业务结果不能丢；逐 token delta 不承诺 durable。
- 启动时扫描非终态 turn/tool/work。纯内部、声明为幂等的 work 可重算；外部副作用 tool 先进入 `unknown` 或 `compensation_required`，查询 outcome 后再决定补偿。“无确认重放”是指不先确认第一次调用是否成功，就直接再执行一次外部操作；发送消息、邮件、扣款或创建日程都可能因此重复，必须禁止。
- 恢复扫描某 tenant 时，临时暂停的是该 tenant 的“新 work 接入”，不是把已经恢复好的 tenant 再停掉。系统先核对它在崩溃前未完成的 turn/tool/work，避免旧任务和新任务同时改同一份状态；核对完成后立即重新开放该 tenant，其他 tenant 全程不受影响。
- dev/single-user SQLite adapter 可以保留，但不得与 PostgreSQL 同时作为同一 Pilot tenant 的规范 control plane；dual-store 兼容路径必须有明确模式开关和测试。
- “不丢消息”的验收范围是 canonical inbound/final message 与可观察终态，不包括内存中的 token delta、错过的 proactive tick 或旧 sleep timer。

#### 5.9.7 ToolExecutionContext 的注入边界

- 三层职责分开：channel/HTTP/WebSocket adapter 负责认证入口并把 Telegram Bot 或 WebChat 消息转换成统一格式；canonical admission 负责在数据库中确定会话/消息身份、分配顺序号、做去重并决定是否接收；`ConversationRuntime` 只负责真正执行这一轮，创建 `turn_id` 和不可变 `ToolExecutionContext`。这样 channel 传来的 `tenant_id` 不能直接越过认证成为授权依据。后台任务只保存 context reference/ownership，执行前重新解析并校验账号和 binding 状态。
- `ToolRegistry` 目标形态是“全局只读工具定义 + 每个 tenant 的可见工具清单 + 每次调用的执行上下文”。这里 `immutable global definitions` 指工具名称、描述和输入 schema 这类所有 tenant 共用、运行中不被某个用户改写的只读定义；用户权限、凭据、目录和 MCP binding 不放在其中。不得通过共享 `set_context()` 在并发 turn 间切换授权身份。
- reasoning 只看到当前 catalog 的 schema；ToolExecutor、MCP adapter、filesystem、message push、scheduler、spawn/output/stop 都在执行时再次校验 context 和 resource ownership。
- `allowlist` 就是“明确允许普通 tenant 使用的工具白名单”，没有列入的工具默认不可见也不可执行。P1 初始范围按 5.8.3 冻结为 tenant-scoped memory（`recall_memory`/`memorize`/`forget_memory`）、本账号消息查询（`search_messages`/`fetch_messages`）、tenant 文件读写、图片读取、服务端绑定的 `message_push`、tenant-owned schedule/reminder，以及经限流和 SSRF 防护的 web search/fetch；P-1 的 tool policy spec 必须把实际注册的精确 tool id 逐项列出后才能写开放代码。`shell`、`spawn`/`spawn_manage`、`peer_agent`、plugin management、system MCP 对普通 tenant 默认关闭。
- 用户 MCP 不随 P1 Token 登录自动开放。也就是说，用户可以先登录并使用内置工具；只有 tenant 独立的 MCP 记录、runtime、文件/网络/secret 隔离、配额、审计和跨租户负向测试全部完成后，才通过后续独立 capability 开放用户添加 MCP。
- 所有工具调用都产生 `tool_call_id` 和审计终态；external-write、process-exec 和 admin effect 在没有幂等/outcome/补偿策略时默认拒绝，不以字符串错误伪装 typed terminal state。

#### 5.9.8 Persona 当前值更新与调试权限

- 本节按已核对的单体实现冻结，不新增 append-only revision、current pointer 或 compare-and-swap。当前代码的 `MemoryStore.read_self()` 读取当前 `SELF.md`，`MemoryStore.write_self()` 直接覆盖当前内容；`MemoryOptimizer` 通过单个 `asyncio.Lock` 串行执行 SELF 更新。
- 一次性人设设置流程在一个事务中写入该 tenant 的当前 PersonaProfile 和初始 RelationshipState。PersonaProfile 对应 `identity + personality_rules`，提交后作为当前固定值并锁定用户侧再次编辑；RelationshipState 对应 `SELF.md`，后续可由 optimizer 原地更新。
- 多租户实现把单体的单写者约束平移到 tenant：同一 tenant 的 optimizer/maintenance 更新必须经过 tenant 串行 lane 或 tenant maintenance lock，并在一个 PostgreSQL 事务中读取当前 RelationshipState、写入新当前值，并追加 Persona 审计记录。既然不允许两个写入者并发，Pilot 不需要额外 CAS；如果未来允许 lane 外多写者，再单独设计版本冲突策略。
- 不建设 Persona/Relationship 产品级历史版本，因此也不存在“revision 保留多少天”的周期决策。数据库只保存当前正文和 Persona 审计记录（来源、更新时间、触发 turn、操作类型）。这里的 Persona 审计记录专门记录 PersonaProfile / RelationshipState 的变更，不与账号、工具或管理员操作的其他审计记录混用；每次更新不复制完整历史正文。
- 异常恢复沿用数据库运维能力：通过 PostgreSQL backup/PITR 恢复整体一致状态，而不是让管理员在产品里浏览并切换旧 Persona revision。
- prompt source breakdown 是调试视图，用来说明最终 prompt 由 RuntimeInvariant、PersonaProfile、RelationshipState、ChannelPolicy 哪些区块组成；optimizer 依据是触发本次 RelationshipState 更新的 pending memory/input 引用。两者可能暴露系统安全规则和用户私密记忆，因此默认只对 admin/debug 开放；这里不包含、也不承诺展示模型隐藏思维链。普通用户只看到产品允许展示的当前 Persona 文本。

#### 5.9.9 数据模型、首次启用与 schema evolution 约束

首批 design 至少覆盖以下逻辑实体。这里“逻辑实体”是业务数据类别/职责的名字，用来先决定系统必须分别管理哪些东西；它还不是最终数据库表名。例如“登录会话”是一个逻辑实体，落库时可以命名为 `auth_sessions`。表名可以在 change 中调整，但责任边界不能合并回任意 JSON blob：

```text
test_accounts, access_tokens, auth_sessions,
telegram_identity_bindings, telegram_binding_codes,
canonical_conversations, canonical_messages, message_events,
inbox_records, message_deduplication_keys,
turns, tool_calls, tool_audit_events, tool_idempotency_keys,
background_work_items, outbound_delivery_intents, delivery_attempts,
tenant_provisioning_jobs, scheduled_jobs, schedule_executions,
attachments,
persona_templates, tenant_persona_profiles, tenant_relationship_states
```

必须先冻结的数据库约束：

- `canonical_conversations.tenant_id` 唯一：会话行即 agent 的资源域，一个账号可拥有多行，同一 tenant 不可被第二个账号占用；active Telegram binding 在 account 和 platform identity 两侧都唯一。
- canonical message 在 `(conversation_id, sequence)` 唯一；channel 入站在 `(source_channel, source_identity_id, source_message_id)` 唯一；WebChat 入站在 `(account_id, client_message_id)` 唯一。
- token/session digest 唯一；明文只在签发/交换响应中出现一次。
- 每个 outbox intent 有稳定 idempotency key；delivery attempt 只能推进自己的 intent，不能重复创建 final assistant message。
- schedule execution 在 `(job_id, scheduled_for)` 唯一；provisioning job 对同一 tenant/operation revision 幂等；attachment metadata 必须绑定 tenant owner 和 immutable storage key。
- account 状态使用 `provisioning`、`active`、`suspended`、`revoked`；只有 provisioning readiness 完成后才能进入 `active` 并签发邀请 Token。`suspended` 可解除但旧凭据不复活，`revoked` 是终态。binding、token/session 使用状态字段 + 时间戳软撤销；历史 message、turn、tool/audit 不级联物理删除。
- repository 的所有 tenant-facing 查询必须从 tenant-bound view 或可信 context 获取 tenant；客户端传入的 `tenant_id` 只能作为管理员筛选条件，不能作为普通用户授权条件。
- PostgreSQL 应增加防御纵深：普通 runtime 使用受限数据库角色，关键 tenant 表优先采用 Row-Level Security 或等价 tenant-bound view；admin 聚合、迁移和 reconciliation 使用独立权限。所有 raw SQL、向量检索、BM25 检索、聚合、导出、backup manifest 和后台 job 都必须有跨 tenant negative test。
- 所有缓存和派生存储必须明确 tenant scope。prompt/retrieval/embedding/tool-result cache、临时文件、attachment download、浏览器缓存和前端持久化状态都必须包含可信 tenant/account 维度，或明确禁止缓存。
- 初始 Pilot schema 不承担旧 `sessions.key`、`messages.session_key/seq` 或 legacy `channel:chat_id` 的数据导入；相关测试只验证新 PostgreSQL 模型的唯一约束、并发 sequence、跨租户隔离和新账号空历史。

Pilot **首次启用**流程冻结为 **Create → Verify → Enable**：

1. **Create**：Alembic 在 PostgreSQL 创建 Pilot 新表、索引、约束和必要 seed；不连接旧 SQLite，不复制旧 session/message/memory，也不修改旧库。
2. **Verify**：验证 schema revision、约束、空库基线、provisioning 幂等、并发 sequence 和跨 tenant negative query；测试数据与正式 test account 分离。
3. **Enable**：通过单一 feature/config flag 开放 Pilot provisioning、WebChat 和 Telegram binding。新账号创建后只写 PostgreSQL；旧单体 SQLite 路径保持独立，不参与切换事务。

首次启用的 rollback 是关闭 Pilot 入口并停止新 provisioning/work，保留已经产生的 PostgreSQL Pilot 数据用于修复后继续使用；不得把 Pilot 数据反向同步到 SQLite，也不得把旧单体库当成 Pilot 回滚目标。需要恢复业务数据时使用 PostgreSQL backup/PITR。

Pilot 上线后的 **PostgreSQL schema evolution** 仍采用 **Expand → Backfill → Verify → Cutover → Compatibility → Contract**，但这里的 backfill 只发生在 Pilot PostgreSQL 的旧/新 schema revision 之间，不包含现有单体 SQLite 数据：

1. **Expand**：先增加新表、nullable 列、辅助索引或兼容字段；同一 release 不做不可逆 data loss SQL。
2. **Backfill**：确有存量 Pilot 数据需要转换时，按稳定 PostgreSQL primary key 默认每批 500 行，使用可重跑 cursor 和幂等 upsert；没有存量数据时明确标记 `not_applicable`，不为了流程形式制造迁移任务。
3. **Verify**：核对 PostgreSQL 内的前后 row count、deterministic hash、foreign key、唯一约束和跨 tenant negative query；冲突必须落报告。
4. **Cutover**：在受控窗口通过单一 feature/config flag 把 Pilot 应用切到新 schema 路径，并记录 revision、前后计数和 backup id。
5. **Compatibility**：需要旧 schema/adapter 时默认只读保留 30 天；新写入只进入新 canonical 路径，不双主写。若变更没有兼容对象，可在 spec 中标记 `not_applicable`。
6. **Contract**：兼容窗口、备份验证和 rollback drill 通过后，才在独立 Alembic revision 删除旧 Pilot schema/adapter。

`Migration SQL` 指 Alembic/SQL 执行的 PostgreSQL schema 创建或版本演进语句，例如建表、加列、在 Pilot PostgreSQL 内批量转换数据和增加约束；在当前 Pilot 首次启用中，它不指 SQLite 历史数据导入。Capability spec 中采用以下默认形态：

```sql
-- initial create or additive expand
CREATE TABLE new_entity (...);
ALTER TABLE current_entity ADD COLUMN canonical_id UUID NULL;

-- only when existing Pilot PostgreSQL rows need conversion
INSERT INTO new_entity (...)
SELECT ...
FROM current_entity
WHERE id > :last_id
ORDER BY id
LIMIT 500
ON CONFLICT (source_id) DO NOTHING;

-- verify first, then tighten constraints in a later step
ALTER TABLE new_entity
    ADD CONSTRAINT fk_name FOREIGN KEY (...) REFERENCES ... NOT VALID;
ALTER TABLE new_entity VALIDATE CONSTRAINT fk_name;
```

- 普通 DDL 在 Alembic transaction 中执行；PostgreSQL `CREATE INDEX CONCURRENTLY` 等不能在普通 transaction block 中执行的操作单独 revision/step，并显式记录失败后的重跑方式。
- 新 required column 先 nullable 或带安全 server default，backfill/verify 后再 `SET NOT NULL` 并移除临时 default；大表不在一次事务中全表 `UPDATE`。
- `downgrade()` 只负责尚未 cutover、确认无人读取的新对象；cutover 后优先 forward-fix 或 PostgreSQL backup/PITR，不把 destructive reverse SQL 伪装成安全自动 downgrade。
- 每次 schema change evidence 保存适用的 before/after counts、hash/冲突报告、batch cursor、持续时间、revision、backup id 和 rollback drill 结果；不适用项明确记录 `not_applicable`。

#### 5.9.10 Change 拆分与开工顺序

为避免一个 change 同时修改身份、协议、认证、工具、Persona、恢复和召回，后续阶段计划至少拆成以下相互可验收的 capability/change；实际可以合并相邻的小 change，但不得跨越下述依赖边界形成一个“大一统 migration”：

1. canonical identity + PostgreSQL conversation/message 基础；
2. durable ingress/inbox + turn/work + outbox/delivery control plane；
3. tenant admission + bounded queue + restart recovery；
4. WebChat protocol + dev-only channel/Gateway/frontend；
5. invitation auth + account provisioning/readiness + admin/browser security；
6. authenticated attachment/media lifecycle；
7. tenant tool context/resource/effect isolation；
8. RuntimeSnapshot lease coverage + hook failure + tenant secret/revocation boundary；
9. Persona/Relationship tenant storage + optimizer concurrency；
10. Telegram binding + cross-channel synchronization；
11. tenant-owned explicit schedules + recovery semantics；
12. observability/privacy/redaction + backup manifest；
13. memory retrieval BM25/hotness/RRF 改造与离线评测；
14. memory engine plugin catalog/binding、tenant 选择持久化与 WebChat selector。

依赖顺序至少满足：1 是 2、4、5、10 的前置；2 和 3 是公网 delivery/recovery 承诺的前置；5 是 6、7、10、11 对普通 tenant 开放的前置；7 是用户 MCP 的前置；12 可以先定义契约并伴随各 change 落地。第 13 项是独立质量能力，不阻塞第 4、5 项建立安全 WebChat 登录闭环；第 14 项依赖 tenant identity、WebChat auth、RuntimeSnapshot 和 tenant plugin catalog，但不要求先完成 BM25、ParadeDB、jieba 或 reranker migration。除非阶段 spec 明确把召回质量设为出口条件，不得把认证/WebChat change 与检索质量 migration 绑成同一次发布。

#### 5.9.11 Ingress、canonical message、outbox 与 delivery transaction

Pilot 冻结以下事务与状态边界：

- **入站接受事务**必须原子写入 inbox/dedupe record、canonical user message 和 queued turn/work；只有该事务提交后才向 channel/WebChat 返回 accepted。Telegram 的 source message id 和 WebChat 的 `client_message_id` 是强制幂等键，不允许只靠连接顺序或内存去重。
- **执行完成事务**必须原子写入 final assistant message、turn terminal state 和 outbound delivery intent/outbox。流式 delta 可以只存在于在线连接，不进入恢复承诺。
- **模型生成完成不等于 channel 已送达**。delivery worker 单独记录 `pending/attempting/sent/failed/dead_letter` 等状态、attempt、provider receipt/error 和时间戳；`sent` 只能由 channel/provider ack 或明确成功结果推进。
- Delivery 采用 at-least-once，outbox item 必须有稳定 idempotency key；重启后只重试未确认送达的 intent，不重新生成 assistant final message。
- Delivery dispatcher 使用数据库 lease 认领 outbox：Pilot 初始 `lease_ttl=60s`、每 `20s` heartbeat，超过租约才允许其他扫描器接管；单次 delivery attempt 默认最多 5 次，退避建议为 `1m/5m/30m/2h/6h`，最终进入 `dead_letter`。这些是可配置的 Pilot 初始值，不是业务成功保证。
- stale lease、dead letter、`unknown` 和 `compensation_required` 必须有管理员可见的查询和人工处置入口；人工操作必须保留原 work、attempt、provider receipt 和处理原因，不能直接删除或重置状态。
- `complete_inbound` 或等价状态表示 durable acceptance/terminal processing 已收束，不表示 Telegram/WebChat 已成功展示；用户可见 delivery failure 必须能从 canonical final message 补拉或由管理员重投。

#### 5.9.12 Persistence ownership 与 backup manifest

Pilot canonical store 冻结如下：

- PostgreSQL 负责 account/binding/conversation/message、inbox/dedupe、turn/tool/work、outbox/delivery、tenant memory/persona/relationship metadata、显式用户 schedule、attachment metadata 和 provisioning readiness。
- SQLite、JSON 和 Markdown 只作为 dev/single-user compatibility、独立 legacy single-user store 或可重建派生物；现有单体 SQLite 不作为 Pilot 迁移输入、fallback 或双写目标。任何仍保留为 Pilot canonical state 的例外必须在对应 design 中逐项声明，不能默认沿用。
- Attachment bytes 可以继续使用文件系统或后续对象存储，但路径必须 tenant-namespaced，业务 API 只暴露 immutable `attachment_id`，ownership/size/MIME/checksum/retention/reference 落 PostgreSQL。
- backup manifest 必须显式列出 PostgreSQL、tenant workspace/blob root、必要配置和 secret 恢复方式，并给出一致性点、加密、校验与恢复顺序；这里的“数据库恢复”特指从 PostgreSQL base backup 或 PITR 恢复到隔离的恢复实例/目标数据库，不是从 SQLite 或内存队列重建 Pilot。恢复后必须先暂停公网入口、outbox 和 scheduler，完成 schema、ownership、credential、recovery action 和撤销状态 reconciliation，再重新开放服务。旧单体 SQLite/workspace 作为独立 legacy backup 项记录路径、checksum 和保留策略，只恢复到 legacy 模式，不恢复进 Pilot PostgreSQL；`/tmp` 不属于 durable backup 范围。
- PostgreSQL 恢复可能带回恢复点之后已经撤销的 session、invitation token、binding、schedule 或 outbox 状态。恢复流程必须提升全局 `auth_epoch`/`recovery_epoch`，默认撤销或重新核验恢复点中的活跃凭据，并在 reconciliation 完成前禁止发送外部副作用。
- config/secrets 不与普通业务备份混成明文归档；需要分别说明密钥来源、轮换与灾难恢复权限。

#### 5.9.13 Account provisioning 与 readiness

- Account 创建先进入 `provisioning`，由控制面创建 tenant partition、canonical conversation、Persona/Relationship seed 和必要目录/metadata；ready 前不签发或展示 invitation token。
- Provisioning 不由第一个用户 turn 隐式触发。turn 入口仍保留 fail-closed `require_ready()`，但它只作为纵深防御，不承担正常开户流程。
- pending provisioning 在进程启动时从 PostgreSQL 恢复；失败原因对管理员可见，可幂等 retry。retry 不生成第二个 tenant、conversation 或重复 seed。
- `suspended` 和 `revoked` 不删除 partition 或历史数据；`revoked` 终止新 work，保留恢复/审计所需 metadata。
- 具体 DDL job 状态、retry/backoff 和超时可以配置，但 terminal state、幂等键、管理员操作和审计字段必须在编码前冻结。

#### 5.9.14 显式用户 schedule 与 proactive tick 分离

- 用户通过 scheduler 创建的 reminder/cron 是 durable business work；Proactive/Drift/optimizer tick 是可从当前状态重新计算的 runtime tick。两者不能共享“错过就跳过”的笼统恢复语义。
- Schedule 必须保存 `tenant_id`、`account_id`、`canonical_conversation_id` 和服务端解析的 delivery binding；模型/客户端不能提交任意 `channel/chat_id` 作为最终授权目标。
- 每次计划执行使用 `(job_id, scheduled_for)` 作为幂等键，持久化 execution attempt、terminal outcome、delivery intent 和触发时使用的时区/计划版本。
- Pilot 默认：one-shot misfire grace 为 5 分钟；超过 grace 标记 `missed` 并可由管理员查看，不静默删除。Recurring job 不回放全部错过次数，只计算下一未来 occurrence；每次 skip/miss 都有记录。该默认值可在 P0/P2 压测或产品验证后调整。
- 账号 `suspended` 时暂停新执行，恢复后从下一未来 occurrence 继续；`revoked` 时禁用 schedule。时区使用 IANA 名称，DST 跳时和重复时刻必须有 contract test。
- 每个 tenant 可配置 proactive/Drift 开关、channel preference、quiet hours 和每日推送上限；用户关闭后不得由 runtime tick 或 plugin job 绕过。达到推送预算、无法确认 delivery target 或账号状态不明确时必须 fail-closed，并记录 skip reason。

#### 5.9.15 Attachment/media lifecycle

- 上传成功后只返回 immutable `attachment_id`；HTTP、WebSocket、tool 和 channel envelope 不传播可由客户端控制的本地绝对路径。
- Blob 路径使用 tenant namespace，PostgreSQL metadata 记录 owner、size、detected MIME、checksum、storage key、状态、引用 message 和 retention deadline；上传、读取、转发、删除都重新校验 principal 与 ownership。
- Pilot 初始 allowlist 只包含图片和纯文本，默认单文件上限建议 20 MiB，均必须配置化并在 P-1 spec 中列出精确 MIME/扩展名；PDF、压缩包和可执行内容默认拒绝。图片必须限制像素数、解码耗时和内存占用，文本必须限制字符数和编码；不执行上传内容。
- 服务端执行 MIME sniffing、扩展名规范化和 filename/path 隔离，不执行上传内容。未引用或失败的临时上传默认 24 小时后清理；已引用 attachment 跟随 message/account retention policy。
- Blob root 必须进入 backup manifest；落库成功但 blob 缺失、blob 存在但 metadata 未提交都要有 reconciliation/cleanup 流程。

#### 5.9.16 RuntimeSnapshot、hooks、credentials 与 revocation

- Process-wide immutable base `RuntimeSnapshot` 可以保留，用于 plugin definitions、generation 和共享只读 wiring；tenant tool catalog、MCP binding、credential、policy 和 config revision 必须作为 per-task/per-call context 解析，不能作为共享可变 snapshot state。
- **安装态、激活态与 Tenant Hook 绑定**：`installed`、`active generation`、`tenant binding` 分离。管理员安装插件后可以不挂任何 Hook；若没有 tenant 选择其 hook/tool/job contribution，插件保持 dormant，不进入任何 `TenantRuntimePlan`。这里区分两种“注册”：安装时只把 package/manifest 登记进 catalog，不执行插件代码；Python class、decorator handler 和 module provider 的代码级注册必须在候选激活时 import 插件后发生。共享 `PluginManager` 只需承载当前有实际绑定需求的插件 union；每个 work 由 `TenantRuntimeResolver` 根据 (`snapshot_id`, `tenant_id`, `tenant_policy_revision`) 生成不可变 `TenantRuntimePlan`。AgentLoop 定义 Hook 类型与调用契约，插件通过 module provider/decorator 把 handler 注册到某个既有 Hook；每个可选择能力项使用稳定 `contribution_id`、固定 hook/tool/job 类型和是否 `tenant_configurable`。tenant 只能启停这些已注册能力项，不能创造 AgentLoop 不存在的新 Hook，也不能把签名不兼容的 handler 任意搬到其他 Hook。安全 gate/授权 interceptor 和 process-scoped channel/managed service 只允许管理员控制。未在 plan 中的 contribution 不可见、不可调用、不可由后台任务隐式触发。
- **Hook 数据方向与回传契约**：插件通常不是主动向 `AgentLoop` 推送任意数据。work runtime 先依据当前 turn/tool 构造 Hook context 或 `PhaseFrame`，再只调用当前 `TenantRuntimePlan` 启用的插件能力项。GATE 型生命周期 handler 可以原地修改允许写字段或返回替换后的 context，结果按顺序传给下一个 handler 和后续 phase；phase module 通过返回同一个/新的 frame，并向声明过的 namespaced slot 写结果，由后续内建 module 收集；TAP/fanout 型 handler 的返回值不进入主链路，只用于观察、审计或通过 tenant-bound service 产生旁路副作用；pre-tool handler 可以返回新 arguments 或 deny，`ToolExecutor` 再使用最终参数调用真实工具。context 中的 turn/session/message/tool 数据来自 Agent runtime，不是插件自己伪造后交给 AgentLoop。
- **基础设施插件与租户默认配置**：不是所有 plugin contribution 都交给 tenant 自由选择。每项能力声明 `binding_policy=required|default_on|opt_in` 和 `tenant_configurable`：租户隔离、认证/授权 gate 等平台正确性能力用 `required + tenant_configurable=false`；记忆、默认工具等产品基础能力通常用 `default_on`，在创建 tenant 时自动生成 binding 和默认配置，由平台策略决定 tenant/admin 是否可整体关闭；普通扩展用 `opt_in`。基础设施插件仍可共享一份进程级代码 generation/连接池，但其数据库、KV、memory scope、credentials、配置覆盖和后台 work 必须按 `tenant_id` 隔离。Pilot 预装并由管理员信任 `default` 与 `rachael` 两个 memory engine plugin。不同 tenant 可以选择不同的已安装引擎，但不能上传任意 engine、选择未批准的代码版本，或在一个 work 中途切换 engine。每个 work 仍通过 `RuntimeSnapshot` 固定代码 generation，并从 tenant binding 解析一个 active engine；不同引擎的存储、KV、memory scope、配置覆盖和后台 work 必须按 `tenant_id + engine_id` 隔离。当前 `plugins/default_memory/memory_plugin.py` 实际是由 bootstrap 直接构建的 memory engine infrastructure，而 `plugins/default_memory/plugin.py` 主要是 recall inspector；自动记忆写入由 `DefaultMemoryEngine` 订阅 `TurnCommitted` 完成，并不存在独立的 `save_memory` 插件目录。多租户迁移时把 engine runtime、自动 ingest 和 engine-specific tools 按统一 memory plugin 契约接入；inspector 仍标为 admin-observability contribution，不能把它误当成普通 tenant 可选开关。
Pilot 对 memory engine 插件及其默认配置进一步固定为下表。这里的“默认配置”不是给每个 tenant 复制一套插件进程，而是在 tenant provisioning 时创建 binding、tenant-scoped 数据命名空间和允许覆盖的配置：

| 能力项 | `binding_policy` | tenant 能否关闭 | 多租户含义 |
| --- | --- | --- | --- |
| memory engine slot | `required` | 不能关闭，但可以在允许目录内切换 | 每个 tenant 始终有且只有一个 active engine；初始绑定为 `default`，管理员允许后可切换到 `rachael`。active engine 名称和 policy revision 进入 `TenantRuntimePlan`，所有 query/write 必须使用该 engine 的 tenant-bound storage view |
| memory engine implementations | `default_on` / `opt_in` | 由平台 catalog 和 tenant policy 决定 | `default` 在 tenant 创建时作为初始选择；`rachael` 作为已安装的可选实现暴露给 WebChat selector。切换只影响后续 work，不迁移或删除旧 engine 的数据 |
| automatic memory ingest/save | `default_on` | Pilot 默认允许管理员按 tenant 关闭 | 由当前 active engine 按自身契约处理新 `TurnCommitted`；关闭后不再写入记忆，但不删除已有 tenant memory |
| memory context retrieval/injection | `default_on` | Pilot 默认允许管理员按 tenant 关闭 | tenant 创建时自动绑定；关闭后不向 AgentLoop 的 prompt/context 注入召回结果，但不影响 memory engine 本身和已有数据 |
| recall/memorize/forget tools | `default_on`，逐项配置 | 是 | tenant 创建时进入默认 tool catalog；可逐项关闭，工具调用仍必须经过 tenant tool policy 和 tenant-bound memory service |
| default-memory inspector | admin observability | 普通 tenant 不控制 | 只供管理员诊断；状态、JSONL/数据库记录和 Dashboard 查询都必须按可信 `tenant_id` 过滤，不能成为普通 tenant Hook 开关 |

Tenant provisioning 与运行时解析规则固定为：

1. 创建 tenant 时，为全部 `required` contribution 建立不可关闭的有效 binding；
2. 为全部 `default_on` contribution 建立有效 binding，并物化该 tenant 的默认配置；
3. 为 memory engine slot 建立一个 active binding；默认使用 `default`，只有在 tenant policy 允许且用户在 WebChat 确认后才切换到 `rachael`；
4. 不为其他 `opt_in` contribution 建立有效 binding，直到管理员显式启用；
5. 同时创建或确认 `tenant_id + engine_id` 维度的 memory/KV/credential namespace，禁止依赖 `DEFAULT_TENANT` fallback；
6. 每次 binding/config 修改提升 `tenant_policy_revision`；下一个 work 使用 `base RuntimeSnapshot + required contributions + tenant bindings/config overrides` 解析新的不可变 `TenantRuntimePlan`，进行中的 work 保持原 plan。

- **Plugin invocation seam**：`PluginContext` 保持 generation/process-scoped，不放 `current_tenant`；每次 hook/tool/job 调用都创建 `PluginInvocationContext`，显式携带不可变 `WorkContext`、tenant-bound session/memory/KV/secret/policy/effect services。插件实例不得保存跨 await 的当前 tenant/session/turn 状态；`ContextVar` 只做观测和兼容，不做授权边界。
- **Pilot 热更新边界**：管理员上传/安装代表其已经信任插件，Pilot 不建设插件签名、恶意代码扫描、sandbox、来源证明和复杂依赖审计。这里必须明确声明：插件以 in-process trusted code 运行，`TenantRuntimePlan` 只隔离插件暴露的能力和调用上下文，不能阻止有 bug 或恶意的插件直接访问进程内存、共享对象、数据库连接或其他 tenant 资源。普通 tenant 不得安装或激活插件；用户添加的 MCP/第三方代码在后续 capability 中必须使用独立 sandbox/container/runtime，不能沿用该信任模型。单纯安装只登记 package/manifest，不执行插件代码；首次激活或更新 active generation 时才执行最小正确性 gate：发现/manifest、稳定 `contribution_id` 与 Hook 类型、candidate import/initialize、snapshot compile；成功后原子发布，失败保留旧 snapshot 并返回错误。插件代码/manifest 变化走新 generation + 旧 lease drain；tenant contribution 的启停、顺序和配置变化只提升 tenant policy/config revision，从下一个 work 生效，无需重启服务或重载插件代码。进行中 work 保持原 `TenantRuntimePlan`。新增 Hook、改变未声明 Hook 映射或修改 handler 实现才走代码 generation 热更新。Pilot 不实现滚动重启和对特殊插件类型的提前分类；候选无法热发布时保持旧版本，由管理员在维护窗口手动重启当前单实例。
- Passive、Proactive、Drift、consolidation、optimizer、recovery work 和 plugin job 在 work start 时各取得一次 snapshot lease；P0 必须审计所有入口并用测试证明 lease coverage。进行中 work 不切 snapshot。
- 旧 snapshot 不得绕过账号 suspension/revocation、secret rotation 或资源 ownership；在外部副作用、schedule trigger 和 outbound delivery 前再次读取当前 account/policy 状态。
- snapshot compile/publish 失败时继续使用上一份 committed snapshot，并记录 generation/error；不能发布半成品，也不能清空当前可用 snapshot。
- Tenant secret 必须静态加密，不能进入 tool schema、模型可见参数、普通日志、metrics label 或错误字符串。密钥轮换/撤销的生效边界必须有测试。
- Hook policy 分层：gate/interceptor 在超时、异常或上下文缺失时 fail closed；best-effort fanout/telemetry 使用有界 timeout 并记录失败，但不得反向改写已经提交的业务终态。`require_ready()`、tenant ownership、账号状态、资源授权和副作用确认任一无法判定时也必须 fail-closed，不能用 `DEFAULT_TENANT`、旧 session 或“暂时允许”作为降级路径。
- LLM、embedding 和外部 MCP 的 data handling 必须在 provider contract 中冻结：允许发送的字段、脱敏规则、provider retention/region、是否用于训练、request log retention、凭据 owner 和撤销方式。供应商策略未知、凭据状态未知或数据分类无法确认时，不得把原始 tenant 内容发送出去。

#### 5.9.17 Observability 与 privacy 默认值

- 默认只采集结构化 lifecycle metadata：tenant/account 的内部标识、work/turn/tool/delivery id、状态、耗时、token 数、queue depth、错误类别和 provider request id 摘要。
- raw provider payload、完整 prompt、message content、tool args/result 和 attachment content 默认关闭；确需 debug 时使用短期、admin-only、审计化开关，并在入库前做 secret、PII、本地路径和 credential redaction。
- Metrics label 不允许包含 account/message/tool-call 高基数字段、原始 tool args 或内容；这些只能进入有访问控制的 trace/audit storage。
- 普通 operational log 建议默认保留 30 天，audit metadata 建议默认保留 180 天，均配置化；内容型 debug 数据使用更短独立 TTL。精确保留期限可在 P-1 合规/运营评审后调整。
- Admin 内容查看、跨 tenant 下钻和导出必须产生 audit event。具体 SLO、采样率和容量阈值由 Pilot 数据决定，不在编码前拍脑袋冻结。

## 6. 分阶段路线

### P-1：编码前决策冻结

**目标**：先把 5.9 中会影响协议、表结构、授权和恢复的决策转成 ADR/design/spec，不写对应的应用功能代码。

- 完成 5.9 的全部设计门禁：canonical identity、Auth/browser security、WebSocket、admission/overload、durable ingress/outbox/delivery、persistence/backup、provisioning、ToolExecutionContext、Persona、schedule、attachment、RuntimeSnapshot/hooks/secrets、observability/privacy 和 DB initial rollout/schema evolution；
- 为新增安全边界建立一张可执行的 negative-test matrix：PostgreSQL role/RLS 或 tenant-bound view、raw SQL/vector/BM25/聚合/导出/后台 job、cache/temp/attachment、旧 `security_epoch` work、outbox lease、插件 context、provider data handling 和 fail-closed 路径都必须有明确 owner、测试入口与失败证据；
- 按 5.9.10 为 identity/control-plane、WebChat、auth/provisioning、attachment、tool/snapshot、Persona、Telegram、schedule、observability 和 retrieval 建立独立 change 边界、依赖图和验收命令；
- 固定协议 fixture、状态机、表约束、错误码和回滚路径；
- 对尚未确定的纯运行参数给出配置项、默认值和压测后调整方式，避免把参数常量散落在实现中。

**出口条件**：5.9 表中不存在会阻塞首个 change 的“由实现决定”事项；每个 change 都能明确说明输入、输出、状态、失败语义、DB rollout/schema evolution 和测试证据。新增的 DB 防御纵深、`security_epoch`、outbox lease/dead-letter、附件解析上限、Proactive 用户政策、provider data handling 和插件信任边界均已标注为 `implemented`、`deferred` 或 `not_applicable`，不能只写在讨论文字中。P-1 完成只代表设计冻结，不标记任何目标能力为 `verified`。

### P0：Pilot 基础运行基线

**目标**：为 WebChat 建设准备最少、可复现的运行基线；本阶段不宣称 WebChat 已经可用。

- FastAPI/Uvicorn 作为应用层基础；
- 单进程 AgentLoop + `asyncio.Queue`；
- PostgreSQL + pgvector 作为长期测试主数据源；
- Cloudflare Tunnel 提供 HTTPS/WSS；
- 配置、数据库和 workspace 做定期备份；
- 增加健康检查、结构化日志和基础错误告警，并形成当前 persistence/backup manifest；默认日志执行 secret、PII 和本地路径脱敏；
- 建立 `interactive` / `maintenance` 两类 work kind，以及 `passive`、`proactive`、`drift`、`consolidation`、`optimizer` 五类 flow 的运行时基线；每个 tenant 的 flow 先经过 tenant-scoped admission，在 tenant 内不并发执行；不同 tenant 异步运行、互不阻塞；所有进程内 queue 给出保守容量和 overload 行为；
- 以独立质量 change 建立 default engine 的 raw query + dense semantic/hotness + BM25/hotness + RRF + top-k 记忆召回基线，并将两条 lane 的 hotness 作为长期保留的默认组成；该 change 不阻塞 P0.5/P1 的 WebChat 与认证安全闭环；
- 建立工具调用的 tenant scope/effect 基线：普通 tenant 不开放宿主机 shell、全局 MCP、Peer Agent 和插件管理；文件、记忆、消息、推送、scheduler 与后台任务必须绑定服务端派生的 tenant context；
- 收束人设 prompt 的来源边界：以当前单体的 `identity`、`personality_rules`、`self_model` 语义为默认基线，拆出 RuntimeInvariant、PersonaProfile、RelationshipState、ChannelPolicy 四类 prompt 来源；不新增复杂人格参数模型；
- 完成 RuntimeSnapshot lease coverage audit，证明 Passive/Proactive/Drift/maintenance/plugin job 都按 5.9.16 绑定 snapshot，且旧 snapshot 不能绕过 revocation；
- 在 P0 建立跨 tenant negative-test harness 和受限数据库连接配置，为后续 PostgreSQL RLS/tenant-bound view、cache isolation 与 stale-work fencing 提供可复现的验证入口；
- 建立 tenant plugin policy/catalog 的最小执行接缝：共享 `PluginManager`/base snapshot + `TenantRuntimePlan` + `PluginInvocationContext`；区分 `installed`、`active generation`、`tenant binding`，允许插件安装后保持 dormant；为插件 contributions 固化稳定 ID、固定 hook/tool/job 类型、`binding_policy=required|default_on|opt_in` 和 `tenant_configurable` 元数据，tenant 只能热启停/排序策略允许修改的 contribution，变更从下一 work 生效；tenant provisioning 自动物化全部 `required`/`default_on` binding、默认配置和 tenant-scoped namespace，运行时按 `base snapshot + required contributions + tenant overrides` 解析 plan；将 required memory engine slot、`default`/`rachael` 两个已安装实现、default-on automatic ingest/context retrieval/memory tools 与 admin-only inspector 分开；覆盖 lifecycle hooks、EventBus、proactive、maintenance 和 plugin job，不允许插件实例保存当前 tenant 状态；
- HyDE-style hypothesis、query rewrite 与 reranker 都保留开关和离线评测入口，但 Pilot 默认关闭，后续通过消融实验决定是否启用。

**出口条件**：Telegram Bot 通道可以连续运行 7 天；重启后已提交到现有持久化存储的数据不丢失，进程内 queue/task 的已知丢失窗口、恢复缺口和 P0.5 durable ingress/outbox 前置条件有明确记录；数据库、配置和 workspace 可以恢复；同一 canonical conversation 不会无序并发执行多个状态写入任务；后台 maintenance 不会长期挤压 interactive turn；FastAPI 应用层能够启动并提供后续 WebChat 建设所需的基础运行环境。

### P0.5：WebChat 最小可用闭环

**目标**：真正建设一个可以与 AgentLoop 对话的 WebChat 最小版本，而不是只预留 Gateway 或认证接口。

- 实现 WebChat 后端 channel adapter，将 WebSocket 入站消息转换为内部消息，并将 AgentLoop 的回复、流式片段、工具状态和终态事件推送回对应连接；
- 让 `turn_id`、`message_id`、`sequence` 和 `tenant_id` 贯穿 turn、tool call、outbound 与重连补拉；明确 WebSocket 断线只影响显示、账号封禁截断执行，以及工具超时、取消、失败、重试和状态补拉策略；
- 实现 WebChat Gateway 的 HTTP/WebSocket 路由、连接生命周期管理、心跳、断线清理、重连所需的稳定 `message_id`/`sequence` 协议；
- 明确定义 WebChat 消息协议和错误协议，至少覆盖连接建立、发送消息、回复增量、回复完成、工具调用状态、服务端错误和重连补拉；
- 新增 WebChat 前端页面或独立前端 bundle，提供消息列表、文本输入、发送状态、流式回复展示、连接状态、重连和错误提示；
- 接通 durable ingress/inbox、canonical conversation/message stream 和 outbox/delivery record：入站接受、执行完成、channel 送达分别按 5.9.11 收束；按 per-conversation sequence 原子落库，持久化 final message 与 turn/tool 终态，流式 delta 只作为在线优化；P0.5 只在本机或显式 dev mode 下运行，不能通过公网 Tunnel 暴露 admin HTTP/API；
- 为 WebChat channel、Gateway、Cookie/Origin handshake、WebSocket 重连、client_message_id 幂等、消息顺序、慢消费者和前端基本交互增加 contract test；
- 完成 ToolExecutionContext、TenantToolCatalog 和统一 ToolPolicy 的最小接缝；在没有 P1 认证、资源 scope 和负向测试前，不得向公网暴露任何 tenant-facing 工具；
- 将 WebChat auth session、Telegram 私聊 identity binding 和其他入口统一收敛到服务端 `account_id → tenant_id → canonical_conversation_id` 映射；客户端 tenant/chat/session 字段不参与授权；
- 本阶段只允许本地或显式 dev mode 使用临时单用户身份，不得在没有 P1 认证和 tenant 隔离能力的情况下将 WebChat 暴露给公网测试用户。

**出口条件**：在本地或受控 dev mode 下，用户可以打开 WebChat、发送消息、收到 AgentLoop 回复和流式更新；刷新或断线重连后不会重复消息；消息顺序稳定；异常连接能够清理；相关后端、协议和前端测试通过。此时仍不视为可供受邀用户使用的 Pilot 客户端。

### P1：一次 Token 登录

**目标**：实现受邀用户的低摩擦登录和服务端身份派生。

- 创建 `test_accounts`、`access_tokens`、`auth_sessions` 和 provisioning readiness 数据模型；账号先 provisioning，ready 后才签发 invitation token；普通用户/admin 分离 session，按 5.9.3 落地 Cookie、CSRF、Origin、timeout 和 401/403 契约；admin HTTP/API 默认只绑定部署主机本机，公网 WebChat 不得成为 admin route 的旁路；
- 实现邀请 Token 的生成、hash 存储、过期、一次性兑换和撤销；
- 实现 `POST /api/auth/exchange` 与 HttpOnly Cookie；
- HTTP API、上传、媒体读取和 WebSocket 统一接入认证依赖；attachment/media 按 5.9.15 使用 tenant ownership、immutable attachment id、MIME/size limit 和清理策略；首版只接受图片和纯文本，图片像素/解码资源、文本字符数/编码均有硬上限，PDF、压缩包和可执行内容 fail-closed 拒绝；
- 登录会话过期、退出登录和失效后的 401/403 行为；
- WebChat 提供 memory engine selector：读取当前 tenant 被允许的 engine、展示能力/状态、提交 `default` 或 `rachael` 的选择；服务端校验 tenant binding 和 engine readiness，选择结果持久化到 PostgreSQL，切换只对下一次 work 生效；
- `tenant_id` 由认证账号服务端派生，并贯穿 Passive、Proactive、Drift、ToolExecutor、memory retrieval、consolidation 和 optimizer；memory engine 则由同一 tenant 的 active binding 派生，不能信任客户端直接提交的 engine 作为授权依据；
- 将 tenant plugin settings/catalog/KV 接入 PostgreSQL canonical control-plane：不同 tenant 可以启用不同 trusted plugin 集合，并为每个 tenant 持久化 active memory engine（首版为 `default` 或 `rachael`）；不复制 `PluginManager`/`RuntimeSnapshot`。WebChat 只展示服务端返回的允许目录并提交选择，普通配置从下一 work 生效，hard revocation 在副作用前即时生效；同时在文档和 admin UI 明确插件是管理员信任的 in-process code，普通 tenant 不能安装或激活插件，用户 MCP/第三方代码不复用该信任边界；
- `tenant_id` 同时绑定该用户的 PersonaProfile 与 RelationshipState，确保主对话、Proactive 和 Drift 不读取其他 tenant 的人设或关系上下文；
- 用户首次登录时进入一次性人设设置流程：可以选择管理员提供的可选 Persona，也可以编辑并提交完整自由文本；提交后保存 tenant 独立快照并锁定用户侧编辑入口；
- WebChat 登录账号与 Telegram Bot 用户私聊身份绑定的可信关联；按 `account → tenant → canonical conversation` 映射两种入口，并对 channel 重试和客户端重发做幂等去重；
- 落地 `persona_templates`、tenant PersonaProfile 与 RelationshipState 的 PostgreSQL 当前值存储；PersonaProfile 在一次性人设设置流程提交后固定，RelationshipState 由 tenant 串行 lane / maintenance lock 保护并原地更新，沿用当前单体语义；runtime role 使用受限数据库权限，并为 RLS/tenant-bound view、raw SQL/向量/BM25/聚合/导出/后台 job 和 cache isolation 增加跨 tenant negative test；provider data handling contract 未确认时不得发送原始 tenant 内容。

**出口条件**：用户只输入一次 Token；首次进入时可以完成一次性人设设置流程，提交后的 tenant PersonaProfile / RelationshipState 可从 PostgreSQL 正确恢复且用户侧不能再次修改；刷新页面、重新打开浏览器后仍能登录（在会话有效期内）；越权请求全部被拒绝；重复兑换同一 Token 不会产生多个账号；admin route 在公网 WebChat 暴露时仍不可达；图片/纯文本超限、ownership 不明、数据库 tenant context 缺失或 provider policy 未知时均 fail-closed；raw SQL、向量检索、缓存和后台任务的跨 tenant negative tests 通过。

### P2：账号控制与长期试用

**目标**：管理员可以低成本运营十几个测试用户。

- 复用现有 React Dashboard，增加 Pilot 账号管理视图或插件面板，不新建独立管理后台；管理员可以查看每个 tenant 的 active memory engine、允许目录、切换记录和按 engine_id 聚合的召回/延迟/失败指标；
- Dashboard 支持账号签发、Token 状态查询、登录会话查看、账号封禁和解除封禁；
- 增加账号选择器，将现有单用户 sessions、proactive、logs、metrics、memory 和插件页面复用为 tenant-scoped 用户视图；
- Dashboard 为管理员提供可选 Persona 模板的新增、停用和预览能力；模板变更只影响后续的一次性人设设置流程，不静默覆盖已创建的 tenant 快照；
- 用户侧 PersonaProfile 只在首次登录的一次性人设设置流程中设置一次，提交后不再提供修改入口；RuntimeInvariant 和 channel 硬限制始终不向用户开放；
- 新 tenant 从当前单体语义对应的 self seed 初始化 RelationshipState；已有 tenant 的关系状态不因默认人设或模板修改而静默覆盖；
- CLI 保留 `issue`、`list`、`revoke`、`expire`，作为应急恢复与自动化入口；
- 账号级撤销、Token 级撤销和原因记录；账号封禁、binding 撤销、secret rotation 和关键 tenant policy 变更提升 `security_epoch`，旧 work/tool/outbox 在状态写入和副作用前重新校验，失败后进入 `cancelled`/`stale` terminal state；
- 封禁后主动断开现有 WebSocket；
- 基础限流、单账号并发上限、消息大小限制和有界队列 overload 策略；interactive 满载明确拒绝，maintenance 可合并/延后；
- 分别观测 interactive backlog、maintenance backlog、proactive/drift skip、tool timeout、按 `engine_id` 区分的 retrieval latency/命中率/失败降级、consolidation failure、optimizer 状态和账号封禁后的截断结果；Proactive/Drift 遵守 tenant 开关、channel preference、quiet hours、每日推送上限和用户关闭状态，无法确认 target 或账号状态时记录 skip reason 并 fail-closed；
- 记录 `last_seen_at`、登录失败次数、最近 IP 摘要和撤销原因；
- Dashboard 管理操作和跨用户切换使用独立 admin credential，并写入审计记录；
- 显式用户 schedule 迁移为 tenant/account/conversation owned durable work，delivery target 由服务端 binding 解析；账号 suspension/revocation、misfire 和执行幂等按 5.9.14 处理；
- 用户 MCP 只有在 tenant namespace、secret ownership、runtime 隔离、ToolExecutionContext 和 5.8.8 负向测试全部通过后才开放，不作为 P1 Token 登录的发布依赖；外部 LLM、embedding、MCP provider 的字段 allowlist、脱敏、region、retention、训练用途、日志保留、凭据 owner 和撤销方式必须可审计。

**出口条件**：管理员可以在现有 Dashboard 中新增或停用可选 Persona 模板，并切换到指定测试用户，于 1 分钟内定位和封禁异常账号；模板变更不影响已有 tenant 快照，切换后所有页面保持 tenant 隔离；Dashboard 故障时可通过 CLI 完成同一关键操作；封禁后新旧连接均无法继续使用，旧 epoch work 不得继续写入 message/memory/outbox；Proactive/Drift 的 quiet hours、用户关闭和推送上限均有测试；误封可解除 `suspended` 状态并重新发放新 Token 恢复，而不需要删除历史数据；`revoked` 账号不原地恢复。

### P3：稳定性与备份

**目标**：把“能跑”变成“适合长期跑”。

- PostgreSQL 自动备份与恢复演练；
- 以 PostgreSQL durable control plane 为基线补齐进程重启后的 inbox、turn/tool、outbox/delivery、显式用户 schedule、provisioning、background consolidation 和 optimizer 恢复契约；明确 delta 不重放、幂等 work 可重算、副作用 tool 先 outcome query/compensation；
- PersonaProfile/RelationshipState 不建设产品级 revision 链；验证 tenant 当前值备份、PostgreSQL PITR 恢复和 optimizer 单写者约束，避免引入与当前单体行为不同的版本浏览/回滚系统；
- 按 5.9.12 执行 PostgreSQL、workspace/blob、配置和 secret 的 backup manifest 与恢复顺序；“数据库恢复”特指 PostgreSQL base backup/PITR 恢复到隔离恢复实例或目标数据库；恢复期间暂停公网入口、outbox 和 scheduler，完成 ownership、credential、撤销状态与外部副作用 reconciliation，并提升 `auth_epoch`/`recovery_epoch`，避免旧备份复活已撤销 session/token；旧单体 SQLite/workspace 独立备份和恢复到 legacy 模式，不导入 Pilot PostgreSQL；对 attachment orphan/missing blob 执行 reconciliation 和 24 小时临时文件清理；
- 进程崩溃自动重启；
- 建设 Dashboard 全局总控制台，聚合所有 tenant 的缓存命中率、`input_tokens`、`output_tokens`、`cache_hit_tokens`、请求量、错误率、P50/P95 延迟、WebSocket 在线数与队列 backlog；
- 支持按时间、账号、tenant、channel 和 model 筛选、聚合与下钻；
- 明确维护窗口和升级回滚步骤；
- 定期清理过期 auth session、已撤销 Token 的敏感字段和过期 debug content；演练 delivery dead-letter 重投、schedule misfire、provisioning retry 与 attachment 恢复；outbox 使用数据库 lease（Pilot 初始 `lease_ttl=60s`、heartbeat `20s`），处理 stale lease，最多 5 次 delivery attempt，按 `1m/5m/30m/2h/6h` 退避，最终进入 `dead_letter` 并支持人工 re-drive/ignore；

**出口条件**：完成一次从备份恢复的演练；演练验证恢复到隔离 PostgreSQL 实例、旧撤销凭据不会直接复活、恢复期间不会产生新的公网请求或外部 delivery；stale outbox lease 不会造成无幂等保护的重复发送，dead-letter 可以由管理员审计并安全 re-drive；总控制台可以查看所有测试用户的聚合运行状态、缓存命中率和 Token 消耗，并下钻到单个 tenant；能够区分“应用故障、数据库故障、上游模型故障、账号滥用”；长期运行期间没有未解释的数据丢失。

### 6.1 运行时可靠性细化

以下工作在 Roadmap 阶段先定义清楚范围和观测证据，再进入具体 OpenSpec change。它们不改变当前单体已经稳定的业务语义；实现时应优先复用现有 `ConversationRuntime`、`MessageBus`、工具 timeout 和 turn 状态模型。

#### A. Tenant-scoped serial lane

**目标**：把当前单体的串行执行边界从进程级 admission 细化为 tenant/session 级 lane，使不同 tenant 可以异步运行，同时保持一个 tenant 内只有一条执行链。

- lane key 固定为服务端派生的 `tenant_id`；由于 Pilot 一个 tenant 只有一个规范 session，不另建跨 session 调度模型；
- 所有 `interactive` 和 `maintenance` work item 都先进入对应 tenant lane，再进入 Pipeline/Stage/Executor；不能绕过 lane 直接调用 turn、memory write、consolidation、optimizer 或 tool executor；
- 同一 tenant 内保持串行：同一时刻最多一个 active turn/tool/maintenance execution；当前 interactive turn 优先，maintenance 在 lane 空闲时执行，新 interactive 到来时可 defer maintenance；
- 不使用跨 tenant 的 global maintenance lock；一个 tenant 的 lane backlog、取消或失败不得持有其他 tenant 的 lane；
- 每次入队、开始、结束、取消和异常都记录 `work_id`、`tenant_id`、`session_key`、`work_kind`、`flow`、`stage` 和时间戳，用于验证是否真的满足租户内串行、租户间异步；
- 账号封禁、binding 撤销、secret rotation 和关键 tenant policy 变更都必须提升 `security_epoch`。Work、ToolExecutionContext、outbox intent 和后台 work 记录捕获创建时的 epoch；所有 memory、Persona、message、delivery、schedule 和 audit 的状态写入都要校验当前 epoch/status。旧 work 校验失败时进入明确的 `cancelled`/`stale` terminal state，不得继续提交。
- lane owner 必须在成功、失败、取消、超时和异常路径统一释放；进程关闭时禁止产生新的 work item，并将未完成项交给恢复扫描。

**落地顺序**：P0 先给当前 admission 和 Passive lane 加观测，并建立 tenant admission key、有界 queue 与 overload；P0.5 将 accepted work 与 durable inbox/control-plane 接通；P1 通过 auth principal 拒绝越权并完成公网门禁；P2 接入账号封禁取消；P3 演练 lane backlog 重建。验收重点是：同一 tenant 无重叠状态写入，不同 tenant 不因共享 lane 互相等待。

#### B. Durable recovery / replay

**目标**：需要保留的业务任务重启后可恢复，但不对已经可能产生外部副作用的函数做无确认重放。

- **inbound**：落 durable inbox，记录稳定 `message_id`、tenant/session、sequence、接收时间和处理状态；启动扫描 `queued`/`in_progress` 的未完成项，按原 tenant/session 顺序重新入队；
- **outbound**：落 durable outbox 或等价 delivery record，记录消息内容引用、channel、目标、发送状态、attempt、last_error 和 provider message id；断线或重启后按幂等键补发/确认，不能因 WebSocket 重连重复发送；
- **maintenance**：consolidation、recent-context refresh 和 optimizer 使用可重算的 work record；启动时根据 `last_consolidated`、session messages、memory 文件/版本和 optimizer cursor 判断是否幂等重跑；
- **active turn/tool**：重启时将运行中的 Python task 视为被中断；先确认 tool outcome，若产生副作用则查询并执行工具声明的 compensation，补偿成功才把外部状态视为恢复；结果未知、无法查询或补偿失败时保留 `unknown`/`compensation_required`，不自动从头执行；
- **显式用户 schedule**：从 durable schedule/execution 恢复，按 `(job_id, scheduled_for)` 去重；one-shot 超过 grace 标记 `missed`，recurring 只计算下一未来 occurrence；
- **runtime scheduler tick**：proactive/optimizer 不逐个补发错过的 tick，启动后按当前时间和状态重新计算下一次调度；
- **provisioning/attachment**：恢复 pending/failed provisioning 并幂等 retry；按 attachment metadata 校验 blob，清理 orphan/temp，缺失 blob 保留错误状态并告警；
- 每次恢复记录 `recovery_started_at`、`recovery_finished_at`、`recovery_action`、`recovery_result`、原始 work id 和 attempt，用于计算恢复窗口和重复/丢失情况。

这里的两个状态要明确区分：

| 状态 | 含义 | 后续动作 |
| --- | --- | --- |
| `unknown` | 无法确认远端/外部工具到底执行成功、失败还是根本未执行，例如请求超时或连接在响应前断开 | 先查 outcome；未确认前禁止重试原调用；若确认有副作用，再转入 compensation |
| `compensation_required` | 已确认或高度怀疑原调用产生了副作用，但外部状态尚未恢复到调用前，或工具没有自动撤销能力 | 保留原调用记录，执行人工或自动补偿；补偿成功后才关闭恢复事项，失败则继续保留该状态并告警 |

`compensation_required` 不是“普通失败重试”，而是“先恢复外部状态再结束”；`unknown` 也不是成功或取消，只表示结果尚未可判定。

**落地顺序**：P0 明确状态和丢失边界；P0.5 建立稳定 message/turn id、durable inbox/final/outbox/delivery；P1 完成 PostgreSQL canonical control-plane、provisioning 和认证门禁；P2 接入 tenant-owned schedule 与账号状态传播；P3 执行启动补偿扫描及 delivery/schedule/provisioning/attachment 恢复演练。恢复验收必须能解释每一条未完成任务最终是 replay、recompute、compensate、cancelled、missed 还是 intentionally skipped。

#### C. Tool capability inventory

**目标**：先盘点当前工具真实能力，不先发明统一的 timeout、retry 或 rollback 语义。

每类工具建立一行 capability matrix，至少记录：

| 字段 | 记录内容 |
| --- | --- |
| tool identity | 工具名、实现模块、所属 plugin、是否 MCP/本地工具 |
| ownership | `tenant_id`、`turn_id`、`tool_call_id` 如何传入和落库 |
| side effect | `none`、tenant-local write、tenant-local external call；不得跨 tenant |
| timeout | 当前 timeout 来源、默认值、最大值；没有统一 timeout 就明确记录 `not provided` |
| cancellation | 是否响应 `CancelledError`、是否有显式 stop、取消时是否清理子进程/连接 |
| retry | 当前是否重试、由哪一层重试、哪些错误可重试 |
| idempotency | 当前幂等键/去重方式；没有则记录 `none` |
| outcome query | 超时或断线后能否查询远端执行结果 |
| compensation | 是否能撤销已产生的副作用，补偿动作及失败终态 |
| terminal mapping | 成功、失败、取消、超时、结果未知分别如何映射到 turn/tool 状态 |

第一轮只覆盖 Pilot 实际启用的工具，并以代码和现有测试为准；例如 Shell、MessagePush、MCP 和文件类工具分别记录，不因为它们都实现了 `Tool.execute()` 就假设能力相同。盘点结果再决定哪些工具需要补幂等键、状态查询或 compensation。

**当前代码盘点（2026-08-28）**：以下是已确认的 Pilot 相关工具基线；“未发现”表示在当前实现和对应调用路径中没有发现通用能力，不表示未来不能增加。

| 工具类别                                                  | 当前副作用/归属                                                                                                        | 当前 timeout 与取消                                                                                                                 | 当前 retry / 幂等 / outcome query                                            | 当前 compensation 与状态映射                                                                                    |
| ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| `ShellTool`、`ShellTaskOutputTool`、`ShellTaskStopTool` | Shell 命令可能任意读写；当前工具接口没有通用的 tenant-local side-effect/compensation 声明；后台任务以进程内 registry 的 `background_task_id` 关联 | 默认 60s；普通最大 600s；`auto_promote=false` 阻塞最大 21600s；前台约 15s 后可自动转后台；turn 取消时前台会 kill process tree 并清理临时日志；后台有 stop 工具和显式 timeout | 未发现通用 retry 或幂等键；后台可用 `background_task_id` 查询输出/状态，但重启后 registry 不持久     | 未发现通用 rollback；工具返回 JSON/文本，超时和退出码由结果字段表达，尚未统一映射为 typed `timeout`/`unknown`                              |
| `MessagePushTool`                                     | 明确产生渠道发送副作用；channel/chat_id 从参数或 registry context 进入，当前没有统一 `tenant_id`、`turn_id`、`tool_call_id` 幂等记录           | 没有工具层统一 timeout；取消能否停止取决于 sender；未发现撤回/stop 接口                                                                                 | 未发现幂等键、provider message id 记录或发送后查询；发送失败常被转换为文本结果                        | 未发现通用撤回/补偿；成功/失败主要是返回“已发送/发送失败”文本，需后续适配 delivery outcome                                                 |
| 文件类：`ReadFileTool`、`WriteFileTool`、`EditFileTool`     | Read 无副作用；Write/Edit 修改本地文件，当前通过路径解析和 file mutation lock 串行化，但没有统一 tenant ownership/版本记录                        | 未发现工具层 timeout；异步取消依赖 task cancellation；写操作使用 mutation lock，未发现通用取消后恢复旧内容                                                      | 未发现 retry、幂等键或内容版本/结果查询协议；重复 write/edit 可能重复改变文件                         | 未发现通用 rollback；成功/失败返回文本，需要在 Pilot 中记录文件版本或变更摘要后才能安全 compensation                                        |
| MCP tools / `McpClient.call`                          | 远端工具副作用取决于 MCP server；当前 client 只有 server/tool 名称和 JSON-RPC request id，不强制 tenant/turn/tool envelope            | connect 8s、receive 默认 30s、disconnect 5s；同一 MCP client 用 `_call_lock` 串行；取消/断线会影响 client process，但远端 server 是否已执行可能未知           | JSON-RPC request id 只解决一次协议请求匹配，不是业务幂等键；未发现统一 retry 或远端 outcome query    | 未发现通用 compensation；JSON-RPC error/timeout/connection close 目前转为异常，需在 Pilot 适配为 failed 或 unknown，并禁止无确认重放 |
| `ToolExecutor` / `ToolRegistry` 公共边界                  | ToolExecutor 负责 pre/invoker/post hooks，不自动知道副作用；Registry 将 context 合并入 kwargs                                   | 未提供统一 timeout；`CancelledError` 不会被普通 `Exception` 捕获，但具体 tool 决定如何清理                                                            | 未提供统一 retry、幂等或 outcome query；Registry 对普通异常返回错误字符串，可能让上层把“工具失败”当作正常文本结果 | 未提供 compensation 或统一 typed terminal mapping；需要增加观测/适配层，而不是假设所有工具能力一致                                     |

**由盘点直接得出的 Pilot 问题**：

- 当前工具接口没有统一的副作用声明、tenant ownership、tool call identity 或补偿协议；
- `MessagePushTool` 和 MCP 的外部执行结果可能在请求超时/连接断开时变成未知，不能安全重试；
- Shell 的后台任务状态只存在进程内，不能作为重启后的可靠恢复依据；
- 文件写入有串行锁，但没有可审计的旧版本或通用 rollback；
- `ToolRegistry` 的字符串化错误结果削弱了上层对失败/unknown 的区分；
- 因此 Pilot 先做“能力盘点 + 观测 + 明确终态”，再对确实需要的工具补 typed outcome、幂等键、状态查询或 compensation，不把所有工具强行套成同一种 Worker 或 retry 模型。

#### D. Account-ban cancellation and timeout fallback

**目标**：账号封禁后只影响该 tenant，阻断新任务并收束已有执行，不依赖 WebSocket 连接仍然在线。

1. 认证/账号状态在 ingress、tenant lane admission 和每次 tool dispatch 前检查；封禁后拒绝新 work item；
2. 账号状态变为 banned 时，向该 tenant lane 发出 cancellation request，并调用当前 `ConversationRuntime.interrupt_turn()` 收束 active turn；
3. tool 首选协作式取消：收到取消信号后停止等待、关闭连接、清理子进程或释放临时资源，并返回可判定的取消结果；
4. 工具不响应取消时，使用该工具已有 timeout 作为兜底；不得在没有盘点现有行为前擅自给所有工具套一个新 timeout；
5. 上游不可取消或 outcome 不可确认时，停止后续同 tenant 执行，记录 `unknown`，若有副作用则进入 `compensation_required`，不自动重试原调用；
6. 封禁操作记录 `ban_requested_at`、`cancel_requested_at`、`terminal_at`、`tool_call_id`、`cancel_result` 和 `compensation_result`；解除封禁后只允许新 turn 重新进入，不自动恢复已被截断的原 turn。

**落地顺序**：P1 建立服务端 tenant 状态检查和封禁 API；P2 完成 lane 级取消传播、WebSocket 断开和 Dashboard 状态；P3 对不可取消工具做超时演练、补偿演练和恢复验收。验收重点是：封禁不会误伤其他 tenant，所有被截断的 turn/tool 都有终态或明确的 `unknown`。

### P4：升级闸门

只有出现以下任一真实信号，才考虑离开 Pilot：

- 并发 WebSocket 长期超过单机可接受范围；
- `asyncio.Queue` backlog 持续增长，或真实运行数据显示等待、恢复、丢失/重复风险已经影响体验，才评估是否需要持久队列；
- interactive backlog、maintenance backlog 或 tool/retrieval latency 持续超过 Pilot 可接受范围；
- background maintenance 长期挤压 interactive turn，或任务丢失、重复副作用和恢复窗口无法接受；
- 重启恢复窗口或任务丢失风险无法接受；
- 需要多个 Gateway/Worker 同时处理任务；
- 用户规模明显超过 30 个受邀账号，或出现多个运营管理员。

升级顺序固定为：

1. 先补齐指标和压测证据；
2. 保留 FastAPI 应用层认证和账号模型；
3. 把队列适配为 Redis Streams consumer group；
4. 再评估 transactional outbox、DLQ、多 Worker；
5. 最后才评估 Nginx、多 Gateway、容器编排。

## 7. Pilot 验收指标

这里的指标不是新的运行时对象，而是用来回答“任务在哪里、等了多久、为什么失败”的观测字段：

| 术语 | 中文解释 | Pilot 关注点 |
| --- | --- | --- |
| `work_kind` | 任务类型：`interactive` 或 `maintenance` | 交互是否被维护任务挤压 |
| `flow` | 任务所属链路：`passive`、`proactive`、`drift`、`consolidation`、`optimizer` | 哪条链路在积压或失败 |
| `stage` | 链路内阶段，例如 `memory_retrieval`、`reasoning`、`tool`、`delivery` | 具体卡在哪一步 |
| `backlog` | 等待执行的任务数量 | 队列是否持续增长 |
| `latency` | 从任务开始到结束的耗时 | 用户体验和维护耗时 |
| `skip` / `failure` / `timeout` | 跳过、失败、超时次数 | 是否需要重试、降级或修复 |
| `tenant_id` / `session_key` | 任务所属租户和规范 session | 隔离和串行化是否正确 |
| SLO / 可接受目标线 | 不是当前必须填写的数字，而是后续根据真实数据设定的“正常范围”和“红线” | 例如某阶段 P95 延迟、最长等待时间、恢复耗时或失败率超过红线时才触发优化 |

### 7.1 Pilot 必须先采集的数据

Pilot 阶段先定义并采集数据，不先承诺固定 SLO 数字。所有记录都应避免写入消息正文、Token、密码或完整敏感参数；日志和指标只保留稳定 id、摘要或脱敏标签。

#### 任务生命周期事件

每个 work item 至少需要能关联以下字段：

| 类别 | 字段 |
| --- | --- |
| 身份与归属 | `work_id`、`turn_id`、`tool_call_id`、`message_id`、`tenant_id`、`session_key` |
| 分类 | `work_kind`、`flow`、`stage`、`trigger`、`tool_name`、`channel` |
| 版本 | `snapshot_id`、代码版本/部署版本、model 名称 |
| 生命周期时间 | `enqueued_at`、`started_at`、`finished_at`、`cancel_requested_at`、`timeout_at` |
| 生命周期结果 | `status`、`result`、`error_type`、`retryable`、`attempt`、`last_error` |
| 副作用与恢复 | `side_effect`、`outcome_status`、`compensation_status`、`recovery_action` |
| 资源 | `input_tokens`、`output_tokens`、`cache_hit_tokens`、provider、数据库/外部服务耗时 |

由这些时间戳计算并分别保留：

- `queue_wait_ms = started_at - enqueued_at`；
- `execution_ms = finished_at - started_at`；
- `end_to_end_ms = finished_at - enqueued_at`；
- tool 的 `cancel_to_terminal_ms`、`timeout_to_terminal_ms`；
- account ban 的 `ban_to_admission_reject_ms`、`ban_to_terminal_ms`；
- recovery 的 `restart_to_recovered_ms`。

#### 必须按维度聚合的数据

至少按 `tenant_id`、`work_kind`、`flow`、`stage`、`channel`、`tool_name`、`model` 和时间窗口聚合：

| 数据 | 采集方式 | 用途 |
| --- | --- | --- |
| interactive backlog | 入队、出队和 lane snapshot；记录数量和 oldest age | 判断交互是否被维护任务挤压 |
| maintenance backlog | consolidation、recent-context refresh、optimizer 各自记录 | 判断后台维护是否持续积压 |
| queue wait / latency | 记录上述三段耗时的 count、P50、P95、P99、max | 定位排队慢还是执行慢 |
| success / skip / failure / timeout / cancelled / unknown | 按终态计数，并保存 `error_type` | 区分业务跳过、工具失败和基础设施故障 |
| tool outcome | 按工具统计 timeout、cancel 响应、结果未知、重试和 compensation 成功/失败 | 决定是否需要补工具能力 |
| consolidation | 触发次数、pending、threshold、成功/失败、阻断 turn 次数、耗时 | 验证当前默认 threshold=30 的真实影响 |
| tenant isolation | 同 tenant 重叠执行数、跨 tenant 等待、状态写入冲突 | 验证 tenant-scoped lane 是否正确 |
| restart/recovery | 重启时各类未完成 work 数、replay/recompute/compensate 数、重复/丢失数、恢复耗时 | 判断持久化恢复是否足够 |
| account ban | 封禁时 active work 数、取消成功/失败、终态分布、补偿结果 | 验证封禁不会留下无主执行 |
| resource saturation | LLM 并发、provider timeout、数据库连接池等待、外部 API 限流 | 区分 tenant lane 问题和共享基础设施瓶颈 |

#### 数据保留与验收规则

- 交互和维护任务都记录生命周期，但 Dashboard 默认分开展示，避免 maintenance backlog 掩盖 interactive backlog；
- 指标至少保留原始事件的稳定引用和按小时/天聚合结果，便于从总览下钻到 tenant、flow、stage 和具体 tool call；
- 不把 `active LLM turns` 当作 Worker 数；同时记录 interactive active count 和 maintenance active count；
- Pilot 前两周或首批受邀账号完成一轮稳定运行后，先输出基线报告，再讨论目标线；基线报告必须包含 backlog、latency、failure rate、cancellation、compensation 和 recovery window；
- 在基线报告前，不把 P95、失败率或恢复时间写成硬性数字；如果出现任务丢失、跨 tenant 污染、重复副作用或无法解释的无主任务，则不需要等待 SLO 数字，直接作为阻断性问题处理。

最小事件示例：

```text
work_kind=interactive
flow=passive
stage=memory_retrieval
latency_ms=240
tenant_id=test-account-01
session_key=telegram:12345
result=success
```

验收项：

| 指标 | Pilot 目标 |
| --- | --- |
| 首次登录 | 输入一次邀请 Token 后成功建立会话 |
| 持久登录 | 在有效期内刷新页面/重新打开浏览器无需再次输入 Token |
| 撤销生效 | 新请求立即拒绝；现有 WebSocket 在可接受窗口内断开；正在执行的工具按封禁策略截断 |
| 隔离 | 账号只能访问服务端派生 tenant 下的 session、message、memory 和附件；工具副作用不能跨 tenant |
| 双入口在线 | WebChat 实现并验收后作为正式客户端；同一测试账号可同时通过 WebChat 和 Telegram Bot 访问规范会话 |
| Telegram Bot 同步 | WebChat 可读取绑定 Telegram 历史消息，并实时接收 Bot 通道收到的新增消息；断线后按游标补齐 |
| 重启恢复 | 应用重启后账号、登录会话策略和已持久化业务数据仍可用；内存队列丢失和可重放边界有明确记录 |
| 队列安全 | 单进程有界排队，区分 interactive 与 maintenance backlog；超载时明确拒绝、延迟或提示重试，不静默丢消息 |
| 运行时顺序 | 同一 tenant 内 Passive、Proactive、Drift、consolidation、optimizer 不并发执行；不同 tenant 异步运行；Drift 只作为 Proactive 无行动分支进入 |
| 用户切换 | 管理员可选择测试账号，将现有单用户 Dashboard 视图切换到对应 tenant，且不会越权读取其他用户数据 |
| 全局总控 | Dashboard 可按 `work_kind`、`flow`、`stage`、tenant、channel 和 model 聚合查看缓存命中率、Token 消耗、错误率、延迟、在线数和队列状态 |
| 运维 | 管理员可在现有 Dashboard 查询账号状态、最近活动和撤销原因，并可通过 CLI 应急操作；Pilot 阶段不开放用户侧 manual consolidation |

## 8. 状态与决策规则

- `planned`：已确定但尚未实现；
- `in_progress`：已有 active OpenSpec change；
- `verified`：有合并 commit、可复现测试或 benchmark 证据；
- `blocked`：依赖或外部条件未满足；
- `deferred`：明确不属于当前 Pilot，等待升级闸门触发。

路线图只在有证据时更新为 `verified`。认证功能应创建独立 OpenSpec change，避免把尚未实现的 Token 登录方案误写成当前代码行为。

## 9. 当前决策摘要

| 决策          | 当前选择                                                                 | 暂不选择                                |
| ----------- | -------------------------------------------------------------------- | ----------------------------------- |
| 试用模式        | 管理员发放一次性邀请 Token                                                     | 自助注册、长期共享密码                         |
| 登录状态        | HttpOnly Cookie 保存可撤销登录会话                                            | 把长期 JWT 或 Token 放在 URL/LocalStorage |
| Gateway     | FastAPI/Uvicorn                                                      | 手搓协议层、Nginx 作为必需入口                  |
| 队列          | 进程内 `asyncio.Queue`                                                  | Kafka、NATS、Redis Streams            |
| 数据库         | PostgreSQL + pgvector                                                | 额外独立向量数据库                           |
| 公网入口        | Cloudflare Tunnel                                                    | 为十几个用户专门部署复杂公网层                     |
| 管理方式        | 复用现有 React Dashboard：单用户视图 tenant 化 + 用户切换 + 全局指标总控制台；CLI 作为应急与自动化入口 | 另起炉灶建设独立管理后台                        |
| 接入方式并存      | WebChat + Telegram Bot 同时活动，共享规范会话历史                                 | 移除 Telegram Bot 通道                  |
| 对话同步        | PostgreSQL 统一消息流 + WebSocket 实时推送 + 游标补拉                             | 仅维护两套互不相通的 session                  |
| 规范会话身份 | `account → tenant → canonical conversation`；Telegram 继续走 Bot API，首版只绑定“Telegram 用户与 Bot 的私聊身份”，不绑定群聊 | 继续用 `channel:chat_id` 同时承担 tenant 和 session identity |
| 消息顺序与幂等 | per-conversation `BIGINT sequence` 由 PostgreSQL 原子分配；Telegram source id 与 WebChat client id 分别唯一去重 | 应用层无锁递增、依赖 WebSocket 连接顺序 |
| WebSocket 恢复 | final message 与 turn/tool 终态 durable；delta 非 durable，慢消费者降级为游标补拉 | 持久化并逐 token 重放全部 delta，或断线后只靠内存 history |
| 浏览器安全 | 普通/admin 分离 `__Host-` Cookie；CSRF + Origin allowlist；401/403 契约固定 | LocalStorage 长期 Token、共用 admin/user credential |
| Queue overload | tenant 内单 active work、全局 LLM 默认 30；有界队列，interactive 明确拒绝，maintenance 合并/延后 | 无界队列、静默丢消息、maintenance 挤占 interactive |
| Durable control plane | 公网 P1 前 account/binding/message/inbox/turn/tool/work/outbox/delivery/schedule/provisioning/attachment metadata 统一 PostgreSQL；SQLite/JSON/Markdown 只保留明确的 compatibility 或派生用途 | 同一 Pilot tenant 长期依赖多个未声明的规范源 |
| Tenant isolation defense in depth | runtime 使用受限 PostgreSQL role；关键 tenant 表采用 RLS 或等价 tenant-bound view；raw SQL、vector/BM25、聚合、导出、backup manifest、后台 job 和 cache 都必须带可信 tenant scope | 只依赖应用层 `WHERE tenant_id = ...`，或把客户端 tenant 参数当授权依据 |
| Revocation fencing | 账号封禁、binding 撤销、secret rotation 和关键 policy 变更提升 `security_epoch`；旧 work 在状态写入和副作用前重新校验，失败进入 `cancelled`/`stale` | 只取消当前 asyncio task，或允许旧 work 在封禁后继续提交状态 |
| Admin network boundary | admin HTTP/API 默认仅部署主机本机可达；普通 WebChat 可走公网 Tunnel，但不能旁路放开 admin route | 把 admin route 藏在前端、依赖未认证的公网路径或与 WebChat 共用网络边界 |
| Provider data handling | LLM、embedding、外部 MCP 发送字段、脱敏、region、retention、训练用途、日志保留、凭据 owner 和撤销方式必须有 contract；未知时 fail-closed | 供应商隐私策略未知时默认发送完整 tenant prompt/attachment |
| Ingress acceptance | inbox/dedupe、canonical user message、queued work 在一个事务中接受；Telegram source id 与 WebChat client id 强制去重 | 消息先进入内存 queue，再尝试补写业务记录 |
| Outbound delivery | final assistant message、turn terminal、outbox intent 原子提交；channel delivery 独立 ack，采用 at-least-once + idempotency | 把模型生成完成当作已送达，或重试时重新生成回复 |
| Provisioning readiness | account 先 `provisioning`，tenant ready 后进入 `active` 并签发 Token；pending/failed 可恢复和重试 | 在用户第一轮 turn 临时建 partition |
| Explicit schedule | tenant/account/conversation owned durable work；`(job_id, scheduled_for)` 幂等；one-shot 默认 5 分钟 grace，recurring 不回放全部 missed occurrence | 继续保存任意 channel/chat target 的全局 JSON job |
| Attachment lifecycle | immutable `attachment_id` + tenant blob namespace + PG ownership metadata；MIME/size/retention/backup 受控 | 对客户端暴露本地路径，或把 `/tmp` 当长期存储 |
| RuntimeSnapshot 与插件热更新 | process-wide immutable base snapshot + per-task `TenantRuntimePlan`/`PluginInvocationContext`；新 work 用新 generation，旧 work 持有旧 lease 并 drain；副作用前重查 revocation | 每 tenant 复制 `PluginManager`/snapshot，或让共享插件实例切换可变 `current_tenant` |
| RuntimeSnapshot | process-wide base snapshot + per-task tenant catalog/credential/policy；所有 work 取得 lease，副作用前重查 revocation | 把共享 snapshot/registry 当 tenant 授权缓存 |
| Observability/privacy | 默认结构化 metadata；content/payload/tool args 默认关闭并脱敏；admin 内容访问可审计 | 把 prompt、secret、路径或高基数字段写入普通日志/metrics |
| Tool context | immutable definitions/catalog + per-call `ToolExecutionContext`；用户 MCP 独立 capability 后置开放 | 共享可变 Registry context 作为授权，或让参数覆盖系统 identity |
| Telegram 身份 | 管理员预绑定或一次性绑定码关联 test account                                         | 信任客户端提交的 user/chat 参数               |
| Persona 规范存储 | PostgreSQL 按 tenant 保存 PersonaProfile 与 RelationshipState；`config.toml` 只保留实例默认、单体兼容和迁移 seed | 运行时文件与数据库双写，或所有 tenant 共享进程级 Persona 全局变量 |
| Persona 首次设置 | 用户首次登录时可选择管理员提供的 Persona，也可编辑完整自由文本；提交后保存 tenant 独立快照并锁定用户侧修改 | 把 Persona 简化成少量滑杆，或允许用户在日常使用中反复手动切换人格 |
| Persona 模板 | 管理员可新增、停用可选 Persona；模板只服务后续的一次性人设设置流程，已有 tenant 不跟随模板更新 | 管理员修改模板后静默改变所有已选择该模板的用户 |
| Persona 动态变化 | PersonaProfile 在一次性人设设置流程提交后保持固定；RelationshipState 按当前单体 `SELF.md` 语义由系统随 tenant 互动原地更新 | 把 `SELF.md` 视为不可变，或允许跨 tenant、无来源的自动改写 PersonaProfile |
| Persona 生效时机 | RelationshipState 更新成功后，从该 tenant 下一轮 turn 注入新 block；进行中的 turn 使用原 prompt snapshot；PersonaProfile 在一次性人设设置流程提交后固定 | 在同一轮 turn 中途替换 prompt，或要求重启整个进程才生效 |
| Persona 当前值与异常恢复 | 沿用单体：PersonaProfile 保存当前固定值，RelationshipState 保存当前可演化值并由 tenant 单写者原地更新；不建产品级 revision 链，异常恢复走 PostgreSQL backup/PITR | 在没有 tenant 串行保护时并发覆盖，或额外建设与单体行为不一致的 Persona 版本系统 |
| Persona 语义 | 第一版沿用当前单体 `identity`、`personality_rules`、`self_model` 的自由文本语义，不新增复杂人格参数或强制章节 | 先建设关系状态机、好感度数值或多 Persona 编排平台 |
| Prompt 责任边界 | 分为 RuntimeInvariant、PersonaProfile、RelationshipState、ChannelPolicy 四类来源 | 把运行时硬规则、人格正文、动态关系和渠道限制继续混成一段不可追踪文本 |
| self seed | 新 tenant 从当前单体实际 `self_model` / `SELF.md` 语义初始化；已有关系状态不因默认配置修改而静默覆盖 | 用一套新的多租户关系模板替代当前单体内容 |
| Runtime 术语 | Pipeline、Stage/Module、Executor、Scheduler/Loop、In-process Worker、Horizontal Worker、Maintenance 分层；memory retrieval 与 tool call 属于 stage/executor | 把所有后台任务、召回和工具调用统称为 Worker |
| 运行时负载 | 区分 `interactive` / `maintenance`；同一 tenant 内 Passive、Proactive、Drift、consolidation、optimizer 与 tool call 串行；不同 tenant 异步互不阻塞；Drift 是 Proactive 无行动分支 | 所有任务共享一个未定义优先级的并发池 |

## 10. 待决策项

本轮将事项分为四类：`DECIDED` 表示产品/架构语义已冻结，只差 design/spec 和实现；`PROPOSED DEFAULT` 表示 Roadmap 给出安全默认值，但仍需在 P-1 明确接受或调整；`OPEN FOR P-1 SPEC` 表示边界已定但字段级契约仍要在开工前完成；`DEFERRED BY EVIDENCE` 表示必须等 Pilot 数据后再决定。阶段计划不得把 `DECIDED` 重新降级为“由实现自行选择”。

| 状态 | 主题 | 当前结论 / 建议 | 编码前或后续动作 |
| --- | --- | --- | --- |
| DECIDED | 旧单体数据边界 | 现有 SQLite session/message/memory 不导入 Pilot PostgreSQL；Pilot 账号和 canonical conversation 从空历史开始；旧库独立保留 | identity/storage design 禁止 SQLite fallback、双写和反向同步；备份分别覆盖 Pilot PostgreSQL 与 legacy SQLite/workspace |
| DECIDED | DB rollout/rollback | Pilot 首次启用使用 Create → Verify → Enable，不导入 SQLite；后续 PostgreSQL schema evolution 才使用 Expand → 500-row idempotent backfill → verify → cutover → compatibility → contract | 每个 capability spec 区分 initial create 与 future schema evolution；回滚关闭 Pilot 入口或使用 PostgreSQL forward-fix/PITR，不反向同步 SQLite |
| DECIDED | Admin bootstrap | 单一 admin principal；`pilot-admin bootstrap/status/rotate-recovery-token/revoke-sessions/disable/enable`；本地强制恢复只允许 trusted-host TTY；token 轮换默认不撤销有效 browser sessions | auth design 按 5.9.3 将 token 轮换与 session 撤销实现为独立操作，并覆盖 lost-token、suspected-leak、database-restore runbook 与应急测试；明文不写进程参数、仓库、配置或数据库 |
| DECIDED | Ingress/outbox/delivery | 采用 5.9.11 的 acceptance transaction、execution completion transaction 和独立 delivery ack；outbox 使用 lease/heartbeat/stale-lease/dead-letter 语义 | 固化表字段、状态机、幂等键、provider receipt、lease owner 和人工 re-drive/ignore API |
| DECIDED | Persistence ownership | 采用 5.9.12 的 PostgreSQL canonical ownership 和显式 backup manifest | 每个 change 标明 canonical/compatibility/derived store；设计恢复一致性点和 secret 处理 |
| DECIDED | Provisioning lifecycle | account 从 `provisioning` 到 `active`；ready 后才签发 Token；pending/failed 可恢复、可审计 retry | 固化 job/status schema、启动扫描、admin retry 与幂等测试 |
| DECIDED | Tenant admission | 一个 tenant 只有一个 canonical conversation；tenant 内所有 flow/tool 串行，不同 tenant 异步；账号和关键 policy 变化使用 `security_epoch` fence 旧 work | 实现 tenant-scoped lane、取消/封禁传播和全局资源上限；不引入跨 tenant maintenance lock |
| DECIDED | Consolidation 阈值与失败语义 | 保持当前默认 `memory_window=40`、`keep_count=20`、guard threshold=30；失败立即阻断当前 turn | 若要改变语义，另开 change；当前 change 只做 tenant/recovery 接缝 |
| DECIDED | Persona/Relationship 存储语义 | 沿用当前单体的 current-state 模型：PersonaProfile 在一次性人设设置流程提交后固定，RelationshipState 由 tenant 单写者原地更新；不建 revision 链，因此没有 revision 保留周期 | P1/P3 验证当前值备份/PITR、Persona 审计记录和 tenant 并发写入约束 |
| DECIDED | Explicit schedule 语义 | tenant-owned durable business work；`(job_id, scheduled_for)` 幂等；suspend 暂停、revoke 禁用；recurring 不回放全部 missed occurrence | 固化 schedule/execution/delivery schema、IANA timezone 与 DST contract test |
| DECIDED | RuntimeSnapshot/hooks/revocation | `installed`、`active generation`、`tenant binding` 分离，插件可安装后不挂任何 Hook；共享 `PluginManager` 通过 tenant plan 按稳定 contribution ID 过滤插件/Hook；contribution 使用 `required`、`default_on`、`opt_in` 区分平台基础设施、租户默认能力和可选扩展；memory engine slot 为 `required`，首版允许 tenant 在已安装且管理员批准的 `default` / `rachael` 中选择一个 active engine，自动 ingest、context retrieval 和 memory tools 绑定到当前 active engine，inspector 为 admin-only；代码更新使用新 generation + 旧 lease drain，tenant Hook 启停/排序/配置和 active engine 选择从下一 work 热生效；管理员承担插件可信判断 | P0 固化最小正确性 gate、contribution hook/type/binding policy/`tenant_configurable`、tenant provisioning 默认 binding/config/namespace、tenant plugin settings/catalog/KV、统一 invocation seam 和副作用前 revocation recheck；不建设插件安全扫描、sandbox 或滚动重启 |
| DECIDED | 工具取消 | 断线不取消当前服务器端 turn/tool；账号封禁截断 tenant work；协作式取消为主、工具 timeout 兜底 | 每次 tool call 保留 owner、取消请求、timer 来源和 terminal/unknown 状态 |
| DECIDED | 副作用工具契约 | 已产生副作用时先 outcome query，再执行声明的 compensation；审计保留原调用 + 补偿 | 建 capability inventory；无补偿或结果未知时标记 `compensation_required`/`unknown`，不盲重试 |
| DECIDED | Queue 容量初始值 | global interactive 128、per-tenant pending 16、maintenance 64、per-kind maintenance 1、WS outbound 256/soft 192/1 MiB hard；LLM/embedding/MCP/process 默认 30/4/8/2 | 作为可配置 Pilot 初始值实现；P0/P0.5 压测记录拒绝率、backlog、429/退避和内存后，后续 change 才可调整 |
| PROPOSED DEFAULT | Attachment policy | 图片、纯文本；20 MiB/文件；图片像素/解码资源和文本字符数/编码受限；未引用临时上传 24 小时清理；PDF、压缩包和可执行内容拒绝 | P-1 接受或修改精确 MIME、扩展名、大小、像素/字符/资源上限、TTL，并固化 sniffing/reconciliation 测试 |
| PROPOSED DEFAULT | Schedule misfire | one-shot grace 5 分钟，超过后标记 `missed`；recurring 前进到下一未来 occurrence | 产品确认是否保留 5 分钟；无论数值如何都必须持久化 miss/attempt/outcome |
| PROPOSED DEFAULT | 日志与审计保留 | operational metadata 30 天，audit metadata 180 天；内容型 debug 更短且默认关闭 | P-1 接受或调整 retention、访问审批和删除 job；指标 label 规则不可放宽为内容采集 |
| OPEN FOR P-1 SPEC | Exact schema/DDL | 实体、首次启用和后续 schema evolution/rollback 语义已冻结；精确表名、列、index、constraint 名及 capability-specific SQL 尚未冻结 | 各 capability design/spec 给出可执行 DDL/SQL 和并发/唯一性测试，并明确哪些步骤适用或为 `not_applicable` |
| OPEN FOR P-1 SPEC | WebSocket frame/error schema | hello/send/delta/completed/error/replay 语义已冻结，精确 JSON 字段、版本、错误码和 close code 尚未冻结 | P0.5 开工前提交 protocol fixture 与 contract test vectors |
| OPEN FOR P-1 SPEC | Digest/encryption/key rotation | token/session 只存 digest、tenant secret 静态加密已冻结，具体算法、key source、rotation/recovery 未冻结 | Auth/tool-secret design 指定算法、版本字段、rotation 和 disaster recovery runbook |
| DEFERRED BY EVIDENCE | SLO/容量阈值 | 先采集真实 latency、backlog、failure、recovery 数据 | Pilot 运行后再设 P95、最长等待、错误率、RPO/RTO 红线 |
| DEFERRED BY EVIDENCE | Queue backend/多副本 | Pilot 保持单进程 `asyncio.Queue` 抽象 | 只有 backlog、恢复窗口或多副本需求越过 P4 闸门时评估 Redis Streams |
| DEFERRED BY EVIDENCE | Retrieval 增强开关 | BM25/hotness/RRF 建基线；HyDE、query rewrite、reranker 默认关闭 | 依据离线评测和 Pilot 消融结果决定是否开启 |

### 重启恢复建议

当前代码里需要区分以下信息：

| 信息 | 当前单体形态 | 建议的恢复动作 |
| --- | --- | --- |
| 未消费 inbound | `MessageBus._inbound` 进程内队列 | 需要持久化 inbox；重启后按原 tenant/session 顺序重新入队 |
| 已进入 Passive lane、尚未执行的消息 | 每个 session/tenant 的进程内 lane queue | 需要从 inbox 或 turn 状态表重建，不单独依赖内存 lane |
| 正在执行的 interactive turn/tool call | `ConversationRuntime` / task 内存状态；turn 持久化状态可落库，但当前运行中的 Python task 会在重启时取消 | turn 先收束为 `interrupted`/`cancelled`/`unknown`；若 tool 已产生副作用，查询执行结果并运行声明的补偿动作，补偿成功后才视为外部状态已恢复；没有确认前不得从头重放 |
| final outbound 与 delivery | `MessageBus._outbound` 是内存队列，dispatcher 只有有限重试/fallback，没有 ack/outbox | final message 与 outbox intent 已提交时不重新生成；恢复 pending/attempting delivery，按 idempotency key 重投；delta 不重放 |
| WebSocket 流式事件 | runtime event subscriber/history 是内存状态 | delta 可丢弃；客户端用 canonical message/turn sequence 补拉 final/terminal state |
| background consolidation / recent-context refresh | Markdown maintenance queue 和 task 内存对象 | 按 `last_consolidated`、session messages 和 memory 文件状态重新判断并幂等重跑 |
| 显式用户 schedule | 全局 `schedules.json` + 内存 `asyncio.create_task`；owner 只有 channel/chat，one-shot 超过 5 分钟直接丢弃 | 从 PG schedule/execution 恢复；按 `(job_id, scheduled_for)` 去重；one-shot 超 grace 标记 missed，recurring 前进到下一未来 occurrence |
| provisioning job/readiness | `TenantProvisioningService` 的 state/queue 是单进程内存 | 从 PG 恢复 pending/failed；幂等 retry，ready 前不发 Token；不得靠第一轮 turn 重建正常开户流程 |
| attachment upload/blob metadata | workspace uploads 或 `/tmp/nexus_uploads` 本地文件，没有统一 owner/metadata/reconciliation | 对已提交 metadata 校验 blob；清理 orphan/temp；缺失 blob 标记并告警；blob root 随 backup manifest 恢复 |
| proactive tick | `ProactiveLoop` 的时间循环 | 不必补发每一个错过的 tick；启动后重新计算下一次 tick，并重新评估当前状态 |
| optimizer tick | `MemoryOptimizerLoop` 的 sleep 循环 | 不必恢复旧 sleep；启动后重新计算调度时间，基于当前 pending/memory 状态重新运行 |

所以，“都得重放”可以作为业务目标理解为：**不能丢掉需要用户看到或需要记忆处理的业务结果；正在执行的外部副作用调用则先确认结果，必要时执行补偿/撤销，把外部状态恢复后再结束恢复流程，而不是无确认地盲目重放。**“当这个 tool call 没发生过”只适用于补偿动作确实成功的外部可见语义；系统内部仍保留原调用、取消/超时和补偿记录。

### 已确定但需落地的规则

- `memory retrieval` 是 Stage/Executor，`tool call` 是 Executor operation，不统称 Worker。
- 一个 tenant 只有一个 canonical conversation（旧称规范 session）；同一 tenant 内 Passive、Proactive、Drift、consolidation、optimizer 和 tool call 串行；不同 tenant 异步运行、互不阻塞。
- 规范 identity 固定为 `account_id → tenant_id → canonical_conversation_id`；Telegram 继续使用 Bot API，首版只绑定 Telegram 用户与 Bot 的私聊身份，不绑定群聊；旧单体 `channel:chat_id` 数据继续留在 SQLite，Pilot 不导入、不 fallback、不双写。
- canonical `sequence` 按规范会话由 PostgreSQL 原子分配；入站 acceptance、final message/turn terminal/outbox intent、channel delivery ack 是三个明确边界；流式 delta 非 durable。
- P1 公网开放前，account/binding/message/inbox/turn/tool/work/outbox/delivery/schedule/provisioning/attachment metadata 以 PostgreSQL 为规范源；SQLite/JSON/Markdown 只保留明确的 dev/single-user compatibility 或派生用途。
- Tool 授权采用 per-call immutable context；共享 `ToolRegistry.set_context()` 不得作为多租户授权依据。
- Drift 只作为 Proactive 无行动分支进入，不是独立并发 scheduler。
- `active LLM turns` 表示当前正在执行且正在调用 LLM 的 interactive turn 数，不是 Worker 数；Pilot 初始全局上限为 30，maintenance 单独统计。
- consolidation 以当前单体实现为准：默认 threshold 为 30；失败立即阻断 turn，用户文案不展示内部失败原因。
- Pilot 目标是所有 work 在开始时取得 `RuntimeSnapshot` lease，任务中途不切换；当前只有部分入口已显式绑定，P0 必须完成 Passive/control/maintenance/plugin job 覆盖审计。它与 memory maintenance lock 是不同概念。
- “任务”指带有归属、触发、状态和结果的 Work item/Envelope，不等于 Worker；turn、maintenance work 和调度产生的待处理项都可以是任务。
- 当前单体 turn 的归属是 `turn_id → thread_id → request.metadata.tenantId`；状态终态包括 `completed`、`interrupted`、`failed`、`cancelled`。当前没有统一的 turn/tool 全局 deadline，超时由 provider/tool 自己定义。
- WebSocket 断线不取消服务器端执行；账号封禁截断当前 tenant 的执行。取消通过向 turn task 发出取消请求，由执行链捕获并提交终态；这就是当前实现语境中的协作式取消。
- 副作用调用若已发生，恢复流程必须先确认结果并执行工具声明的补偿/撤销；补偿成功才恢复外部状态，审计记录不能删除；无补偿能力或结果未知时不得假装成功，也不得盲目重放。
- Account 必须完成 provisioning/readiness 后才进入 `active` 并签发 invitation token；第一轮 turn 不负责正常开户。
- 显式用户 schedule 是 durable business work，与可重算的 proactive/optimizer tick 分开恢复。
- Attachment API 只暴露 immutable `attachment_id`，不暴露本地路径；tenant ownership、MIME/size、retention 和 backup 必须落地。
- 默认日志只采集结构化 lifecycle metadata；raw content/prompt/tool args 默认关闭并先脱敏，admin 内容访问可审计。
- 多租户 Pilot 暂时关闭用户侧 manual consolidation，保留后台自动 consolidation。

这些后续工作可以通过 OpenSpec change 固化；在实现完成并有测试、压测或恢复演练证据前，不应把相关行为标记为 `verified`。
