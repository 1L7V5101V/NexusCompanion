## Context

动机与实测证据见 `proposal.md` — Why，行为契约见 `specs/tenant-memory-profile/spec.md`。这里只记约束现状。

当前画像层的读路径是一条代理链：`core/memory/runtime.py:36-49` 的 `read_long_term` / `read_self` / `read_recent_context` / `get_memory_context` 全部转发到同一个 `markdown.store` 实例；该实例在 `bootstrap/memory.py:170`（主进程）与 `:246`（dashboard）各建一次，构造时把目录固化成 `workspace / "memory"`（`agent/memory.py:50-51`）；`agent/looping/core.py:169-174` 在 AgentLoop 构造期缓存它并作为 `memory_profile` 注入 `ContextBuilder`（`agent/context.py:226-237`），每轮组装 `TurnContext` 时原样透传（`:335-346`）。AgentLoop 是每进程一个，循环内靠 `msg.tenant_id` 区分租户，不存在 per-tenant AgentLoop。

两个已存在、可以直接复用的机制：

- **per-turn 不可变快照**。`bootstrap/persona.py:resolve_persona_snapshot` 的契约是「turn 入口在 tenant lane 内调用一次，取该 tenant 的 PersonaProfile 与 RelationshipState 组成不可变快照随 ContextRequest 走 prompt 组装」。这正是画像层缺的那个形状。
- **租户 category 根**。`agent/tools/path_resolver.py:52` 与 `bootstrap/attachments/blob_store.py:71-85` 已经用 `<workspace>/tenants/<tenant_dirname(tenant_id)>/<category>` 这套布局，`tenant_dirname`（`path_resolver.py:109`）是公开 helper。

约束：`core/memory/markdown.py:61-80` 的 `MemoryProfileApi` 协议全部同步；`agent/memory_pg.py` 的 `PgMemoryMarkdownStore` 全部 async（`read_long_term:62`、`read_self:135`、`read_recent_context:127` 等），且全仓库无任何实例化点。prompt 块侧 `agent/core/prompt_block.py:168/185/216` 是同步调用。

**`PgMemoryMarkdownStore` 并不满足 `MemoryProfileApi`。** 协议 10 个方法里它实现 6 个：缺 `backup_long_term`、`get_memory_context`、`has_long_term_memory`，协议的 `read_recent_history` 在它那里叫 `read_history`（`:88`）。文件侧靠子类 `MarkdownMemoryStore`（`core/memory/markdown.py:985-996`）补齐后两个并给 `read_history` 加一个重命名别名，PG 侧没有对应子类。task 1.2 验的是「无实例化点」，因此这条协议从未被一致性检查过。接上就在 `core/memory/runtime.py:46` 抛 AttributeError。

`MemoryOptimizer._optimize`（`proactive_v2/memory_optimizer.py:261-298`）的调用形状也是同步的，在 PG 实现上的失效方式值得记下来，因为它不是「读到空」而是「抛异常并被吞掉」：`:266` 的 `self._memory.read_long_term().strip()` 对协程对象取 `.strip()` 立刻 AttributeError，被 `:296` 的 `except BaseException` 接住后 re-raise；`:264` 的 `snapshot_pending()` 未 await，返回值是协程对象而非 None，协程为真值，所以 `if not current_memory and not pending:` 这条空跳过分支永远判不出来。三个 pending 方法在 PG 侧本来就是空实现（`agent/memory_pg.py:176-188`），因为文件后端的「原子重命名」语义在 `memory_items` 上表达不出来。

**三条旁路不经 `ContextBuilder`。** `ContextBuilder.render` 的生产调用点只有两个，都在主对话轮次里：`agent/lifecycle/phases/prompt_render.py:87` 与 `agent/lifecycle/phases/before_reasoning.py:79`。主动推送、drift、唤醒各自手搓消息列表后直接调 `provider.chat`：`plugins/default_proactive/runtime.py:557-576`、`plugins/drift_flow/runtime.py:224-227`、`plugins/wake_proactive/prompt.py:62-65`。它们自己那次画像读取是该次模型调用里画像内容的唯一来源，不存在「主链已经注入过、旁路读的是冗余」这种关系。

其中两条已经在用组装器的段词汇，等于组装器的段渲染被分叉了一份：`plugins/proactive_flow/prompt.py:135` 建的就是 `list[PromptSectionRender]`，`:159-161` 写死 `self_model` / `long_term_memory` / `recent_context` 三个段名（与 `agent/core/prompt_block.py:157/182/218` 三个块的 label 一字不差），`:188` 收尾调 `build_context_frame_message(build_context_frame_content(sections))`，这两个函数主链在 `agent/prompting/assembler.py:154` 与 `agent/context.py:103` 用；`plugins/drift_flow/runtime.py:673-677` 同样在建 `PromptSectionRender(name="long_term_memory", ...)`。

**唤醒那条今天不可加载。** `plugins/wake_proactive/runtime.py:48` 无条件 `from session.embedding_store import MessageEmbeddingStore`，而 `session/embedding_store.py` 在本仓库任何分支上都不存在（`git log --all -- 该路径` 零提交；该 import 自 `168fed4a3`「事件驱动唤醒与插件包系统」起就是坏的，`MessageEmbeddingStore` 在 `:115` 被真实使用，不是多余 import）。插件加载器把它吞成一条 warning + failed gate（`agent/plugins/manager.py:1598-1607`）并 `return None`，所以后端照常启动，wake-proactive 从未加载过。全仓库没有任何测试导入过该模块。因此本 change 的组装路径范围是**三条可加载路径**，唤醒的接入归另一个 change（design ADR-15、Risks 同名条目）。

## Goals / Non-Goals

**Goals:**

- 五类画像的唯一事实源落到画像专表（PG 与 SQLite 各建同形的两张，见 ADR-13），读取按租户、写入按租户，共享目录退出运行时路径。
- 画像与记忆召回分表：画像行不出现在任何召回 SQL 里，召回结果也不含画像内容。
- 同步的 prompt 块协议不被改成 async，改动不外溢到会话内容与召回面。
- 单机与多租户共用同一套按租户解析的画像接口，单机归到固定租户；不再维护第二份文件实现。
- 三条可加载组装路径（主对话、主动推送、drift）经同一个段渲染器取画像段，画像段的渲染只有一份实现；尚不可加载的唤醒插件由静态扫描看守（见 ADR-15）。
- `ContextBuilder` 不做 I/O：租户数据全部从传入的快照走，它不再持有画像存储。

**Non-Goals:**

- 不在多租户模式下建立 per-tenant 的 markdown 目录。选了专表之后，「把根目录信息搬到 admin1 的文件夹」这件事变成「把根目录内容迁进 admin1 的画像行」，磁盘上不会出现 `tenants/<admin1>/memory/`。
- 不改 `PersonaProfile` 的快照与固定语义。`RelationshipState` 的单写者与审计语义也不改，改的只是它当前值的存放位置（ADR-9、ADR-13）。
- 不做画像层的双写（表 + 文件）兼容期。
- 不统一「同名段放系统提示还是放上下文帧」这处路径间差异。改放置会改模型行为，需要跑评测判断好坏，不混进一个修隔离漏洞的 change（差异本身记在 ADR-15 末段）。
- 不修主动推送与 drift 两条路径注入进程级全局身份常量的问题（`plugins/proactive_flow/prompt.py:43-44`、`plugins/drift_flow/runtime.py:838-839` 插的是 `agent/persona.py:8/14` 的 `NEXUS_IDENTITY` / `PERSONALITY_RULES`，而非该租户的 PersonaProfile）。本 change 关掉的是画像泄漏，身份租户化另开 change。

## Decisions

（阅读顺序提示：ADR-13 是存储归属的根决策，ADR-1 / 8 / 9 / 10 都引用它；ADR-15 是组装侧的根决策，ADR-4 引用它。）

### ADR-1 每轮预取不可变画像快照，而不是把 prompt 路径改 async

同步/异步错位有三种解法。

**选定：预取快照。** turn 入口在租户 lane 内一次性把组装期要用的四类画像读成不可变对象，随 ContextRequest 传给 prompt 组装；两个画像块从快照同步读，`MemoryProfileApi` 协议与所有同步调用点不动。这样做的额外收益：四类一次批量查询，比逐块各查少三次往返；并且天然满足 `persona-relationship` 已经要求的「正在进行中的 turn 继续使用其组装时的快照，不中途替换」。

**不选：把 prompt 组装路径改 async。** 要连带改 `agent/context.py` 的组装入口和 drift、proactive、optimizer、dashboard 四条旁路的调用点，回归面覆盖整个 prompt 组装，而收益只是省一层快照。

**不选：给 PG 实现加同步门面。** AgentLoop 已经在事件循环内，同步方法里再阻塞等待异步会自锁；`asyncio.run` 在运行中的 loop 里不可用；改用独立线程加独立 loop 又会让 SQLAlchemy 异步 engine 跨事件循环使用。要绕开就得再维护一套同步连接池，等于双份存储层。（ADR-15 之后同步侧不再有画像读取调用点，这条解法的最后一点吸引力也消失了。）

**预取的落点：扩 `resolve_persona_snapshot`，同一次数据库会话内取回两份快照。** 该函数（`bootstrap/persona.py:29-54`）现在的形状是借一条数据库连接、跑两条 SELECT（`tenant_persona_profiles` 一行 + `memory_items` 的 `self` 一行）、还连接。本 change 改成同一次借用里跑三条 SELECT：`tenant_persona_profiles` 取身份两字段，`tenant_memory_profile_sections` 一次取回 `long_term` / `recent_context` / `relationship_state` 三行，`tenant_memory_profile_entries` 取回 `pending` 与 `history`。返回值从 `PersonaSnapshot` 变成一对快照。代价是 `bind_persona_resolver` 的签名要变，C9 那条装配线被牵连。

比原方案多一条 SELECT，但换来一处改善：`relationship_state` 与四类画像现在出自同一条查询，而原方案里它出自 `memory_items`、画像出自另一处，两者之间有时间缝。

那三条 SELECT 的 SQL 正文归画像存储，不归解析器——解析器借出会话后调用存储的批量读方法（ADR-14 末段），否则 sections 表会有两个读者、两份 SQL 正文，改列必须改两处，正是 ADR-11 要消灭的那种「每个地方自己记得一次」。

**不选：另立一个画像解析器、自己再借一次连接。** 边界更干净，但两次借用之间有时间缝：若那期间 optimizer 写了画像，同一轮拿到的 persona 与画像就来自两个不同瞬间。`persona-relationship` 规定进行中的 turn 使用组装时的快照、不中途替换——两个时刻拿到的东西严格说不是"组装时的快照"，而是两个时刻的拼贴。

（术语说明：本节的"数据库会话"指一次数据库连接的借用周期，即 `async with session_factory()` 那一段，与用户登录会话无关。）

### ADR-2 新建 `MemoryProfileSnapshot`，不扩 `PersonaSnapshot`

把五类画像塞进现有 `PersonaSnapshot` 会让 `persona-relationship` 的「PersonaProfile 提交后固定」语义被误读成画像层也固定——画像是每轮可变的。两者并列挂在 `TurnContext` 上，各自独立演进。`TurnContext`（`agent/core/prompt_block.py:24-33`）补 `tenant_id` 字段，用于快照缺失时的定位与审计。

### ADR-3 删掉 `core/memory/runtime.py` 的无租户代理方法，改为显式收 tenant_id

`MemoryRuntime` 上有六个无租户代理方法（`core/memory/runtime.py:36-52`）：`read_long_term` / `read_self` / `read_recent_context` / `read_recent_history` / `get_memory_context` / `has_long_term_memory`。它们的签名里没有租户，是泄漏的直接来源，保留它们就等于保留一个「随手一调就读全局」的入口。改为按租户解析：调用方要么已经持有租户上下文，要么拿不到画像。

六个里有三个（`read_recent_history` / `get_memory_context` / `has_long_term_memory`）零生产调用方，按 ADR-14 直接删掉不补替代入口；剩下三个改成显式收 `tenant_id`。

**不选：用 contextvar 隐式携带租户。** 隐式上下文正是 `tenant-tool-isolation` 要收敛掉的「进程级共享可变上下文 + 参数可覆盖」模式，同一进程内两个租户的 turn 交错时，隐式传播链上任一处漏 `copy_context()` 就会串。显式传参难看但可静态检查。

### ADR-4 启动期单例引用改为「按租户解析」的入口

原计划记的是「六处」。核过调用链之后是 **4 个独立根**，其中一个根有两个下游消费者，另一处被 ADR-15 消掉：

| 位置 | 性质 |
|---|---|
| `bootstrap/app.py:666`（optimizer） | 独立根 |
| `bootstrap/app.py:738`（dashboard） | 独立根 |
| `bootstrap/app.py:775`（proactive） | 独立根。传 `memory_store=self.memory_runtime` 进 `build_proactive_runtime`，经 `proactive_v2/loop.py:74` → `:105` → `:170` 成为 `scope.memory` |
| `bootstrap/tools.py:597`（maintenance 绑会话生命周期） | 独立根 |
| `plugins/drift_flow/factory.py:114` | 不是根，是上面第三个根的下游消费者（读 `scope.memory`） |
| `plugins/wake_proactive/runtime.py:127` | 同上，**原计划漏列**；但该插件今天不可加载，这处消费者是休眠的（见 ADR-15 与 Context 段），随唤醒 change 一起处理 |
| `bootstrap/tools.py:577-579`（ContextBuilder 注入） | ADR-15 删掉 `ContextBuilder` 的 `memory` 参数后，这处引用消失 |

改法：根上改为持有「租户 → 画像读写入口」的解析器，在真正处理某个租户数据的时刻解析。optimizer 与 proactive 本来就按会话逐个处理，租户上下文在手上，改动是把它传下去。

`scope.memory` 不会整个消失：唤醒侧声明还要用它的 `embedding_api`（`plugins/wake_proactive/runtime.py:553`、`:594`，该路径今天不可加载），消失的只是它的画像职责。ADR-15 之后主动推送与 drift 两条旁路都不再经它读画像。

### ADR-5 抽公共 `tenant_category_root` helper，同时给 blob_store 去重

`agent/tools/path_resolver.py` 的 `_category_root`（`:58-68`）是私有方法，`bootstrap/attachments/blob_store.py:71-85` 复制了同款逻辑。抽成公共函数供迁移读取与后续 category 复用。本 change 内它只服务一件事：定位迁移输入的共享目录，以及避免第三处复制。

### ADR-6 迁移与切换器都要求显式租户，且切换器不得进生产

迁移脚本必须显式传目标租户，缺参 fail-closed（对应 spec 的「未指定归属时拒绝」）。本地租户切换入口 SHALL 只在 dev 标志下注册，判据是它绕过了「会话在 `/api/auth/exchange` 时与账号 1:1 绑死」这条生产不变量——`bootstrap/db/models/canonical.py:96` 的 `uq_canonical_conversations_tenant_id` 约束决定了换租户必须换会话，生产开放它等于允许会话改绑他人租户。

### ADR-7 本地测试租户沿用 `pilot-<随机>` id，`admin1` / `admin2` 只作为 label

不改租户 id 生成规则。`bootstrap/db/repository/provisioning_repo.py:386-388` 的 `_new_tenant_id()` 固定产出 `pilot-<12 位随机>`，本 change 不为测试台加覆盖入口——加一个「可以指定租户 id」的口子等于在 1:1 身份链旁边开一条人工命名路径，与 `canonical-identity` 的派生链约束相抵。

做法是建两个常驻账号，把 `admin1` / `admin2` 写进 `display_name`，在本地测试台的清单里记 label → `tenant_id` 映射。代价：`tenant_dirname`（`agent/tools/path_resolver.py:109`）生成的目录名和 PG 里的 `tenant_id` 都是随机串，看日志时需要查一次映射；换来的是本地与生产走同一条 id 生成路径，不会出现「本地能建出的租户形状生产建不出来」。

### ADR-8 journal 一条事件一行，事件日期写进 `happened_at` 列

粒度与「日期是否占列」是两件不相干的事，分开定。

**粒度：一条事件一行。** `append_journal`（`agent/memory_pg.py:107-123`）的调用方全部按单条事件调用，一天多行天然贴合，也不需要在当天追加时读-改-写整行。备选「一天一行」要重写调用侧，且与现有 `(source_ref, kind)` 去重语义冲突，不选。

**日期：占 `happened_at` 列，且该列 NOT NULL。** 归属表换成 ADR-13 的 `tenant_memory_profile_entries` 之后，日期占列由建表约束保证，不再依赖实现记得填。这修掉了一个真实缺口：`_append_entry`（`agent/memory_pg.py:240-258`）只写了 `tenant_id` / `id` / `memory_type` / `summary` / `content_hash` / `status` / `created_at` / `updated_at`，`happened_at` 从来没填过，日期于是被降级拼进正文前缀成 `**2026-10-11** <正文>`（`:119`）。新表下正文列只放正文，同一事实只存一处。

`created_at` 不能替代它：迁移历史 journal 时 `created_at` 等于迁移当天，而事件日期来自 `journal/YYYY-MM-DD.md` 的文件名，两者必须分开。

索引：`tenant_memory_profile_entries` 建 `(tenant_id, category, happened_at DESC)`。按天回看走这条索引即可。

**journal 的定位：它是日志，只是和画像住在一起。** 不新建 `journal_entries`、也不挪去审计面。「只做记录、不被记忆路径消费」在 ADR-13 之后是结构性保证，不再需要论证：画像表不出现在任何召回 SQL 里，`recall_memory` 的三条 lane（向量 / BM25 / LIKE）都只打 `memory_items`。

这一点值得对照原方案：原方案把 journal 放进 `memory_items`，「取不到」的论证只能落在 `infra/storage/postgres_memory_store.py:889` 那条时间检索硬写的 `memory_type='event'` 上，覆盖不到向量与 BM25 两条 lane——`plugins/default_memory/engine.py:1440-1448` 的 `_resolve_memory_types` 只在 `filters.kinds` 非空或 `intent == "procedure"` 时返回类型白名单，其余意图返回 None 即不加过滤，而 `bootstrap/db/models/memory.py:17` 的 `memory_type` 列是 `String(64)` 无 CHECK 约束。

推论：`MemoryProfileSnapshot` **不含 `journal` 字段**。快照是给 prompt 组装用的值对象，journal 没有任何组装期消费者，放进快照就是让预取去查一份没人读的数据。所以本 change 的组装期预取范围是四类（`long_term` / `pending` / `recent_context` / `history`）。spec 里的归属契约仍写五类——归属说的是"这些数据按租户不互串"，预取说的是"组装时取哪几份"，两者不是一回事。将来若真要按天回看，那是一条显式的按类型查询，属于另一个 change。

### ADR-9 自我认知的访问路径归 persona 那条缝，当前值存进画像专表

单体（markdown）不再作为画像层的第二条实现路径：五类画像读写只走按租户解析的专表存储，单机归到 `infra/storage/tenancy.py:22` 的 `DEFAULT_TENANT = "default"`。

`self` 这一格要分两件事说，原计划把它们混在一句「整体移出画像层」里：

**访问路径归 persona 缝。** 读写都只有 persona 那一个入口，两种后端走同一套语义：

- **读**：`SelfModelPromptBlock` 只读 `persona_snapshot.relationship_state`，删掉按模式二选一的 else 分支（`agent/core/prompt_block.py:162-173`，其中 `:171` 的 `ctx.memory.read_self()` 是全仓库最后一处 `ctx.memory` 读取，见 ADR-15）。该字段全仓库只有这一个消费者（定义 `agent/core/types.py:92`，赋值 `bootstrap/persona.py:60`），所以删分支不牵连别处。它也不进 `MemoryProfileSnapshot`——交付走 persona 快照。
- **写**：optimizer 统一走关系状态写缝（`proactive_v2/memory_optimizer.py:320-337`），该缝在 PG 与 SQLite 各一个实现，两者都同事务写审计事件。画像层的 `read_self` / `write_self`（`agent/memory_pg.py:135-139`）随之删除。
- **装配**：`bootstrap/app.py:649` 的 `if self.webchat_durable is not None:` 放宽，两种后端都装 resolver 与写缝。

**当前值存进画像专表的一行。** `PgRelationshipIO`（`bootstrap/persona.py:64-146`）现在打的是 `memory_items` 里 `memory_type='self'` 那一行，改为打 `tenant_memory_profile_sections` 里 `category='relationship_state'` 那一行；审计仍写 `persona_audit_events`，同事务不变。

理由是 ADR-13 的理由一：留在 `memory_items` 就仍在召回面上。`_resolve_memory_types`（`plugins/default_memory/engine.py:1440-1448`）在非 procedure 意图下不加类型过滤，`memory_type` 列又没有 CHECK 约束，所以一次普通召回可以把用户的自我认知整段当检索结果返回。挪进画像专表之后这条路结构性不存在。

**`tenant_persona_profiles.self_model` 不是当前值。** 它是 onboarding 提交时写死的固定值（`bootstrap/db/repository/persona_repo.py:135`），运行时 prompt 路径根本不读它——`resolve_persona_snapshot`（`bootstrap/persona.py:30-61`）只 SELECT `source` / `identity` / `personality_rules` 三列。它现在唯一的出口是 admin 读取接口（`persona_repo.py:250` 的 `_profile_to_dict`）。本 change 不改这个字段的语义，也不动那条 admin 读取路径；关系状态的当前值从此只在画像专表里。

**播种与迁移的分工因此要改。** 单机固定租户的 `relationship_state` 行在 workspace 初始化时从 `[agent.persona].self_model` 播种，仅在行不存在时写入——对应文件后端里 `bootstrap/init_workspace.py:16-27` 播种 `memory/SELF.md` 那一步。而迁移脚本 SHALL 把现存的 `SELF.md` 内容写进该行并覆盖播种值：播种值来自 config 的默认模板，文件里才是演化之后的当前值，只报告不搬会静默丢掉单机现存的自我认知内容。

快照为 None 时该块跳过注入，SHALL NOT 回退读共享文件。多租户下 None 意味着该租户没有 PersonaProfile，而这类租户本来就被 onboarding 门禁挡在对话之外（`bootstrap/chat_api.py:377-378`）；单机下由播种保证恒有该行（ADR-10）。

代价有两条：要给 SQLite 补 persona 表与画像表（ADR-10）；要推翻 `persona-relationship` 里「单体 / SQLite dev 模式 SHALL 继续沿用现有文件语义（行为不变）」那句，本 change 因此带该 capability 的 MODIFIED delta。

**不选：只统一读、写侧留两条入口。** 那样外部行为确实一致，但同一行数据长期有两个写入者、按不同分支被调用，改一边忘一边是静默失效而不是报错。

**不选：当前值继续留在 `memory_items`。** 少一次迁移，但召回污染照旧，且「画像与召回分表」这条不变量会留一个例外，而这个例外恰好是用户最私密的一格。

### ADR-10 SQLite 侧补四张表，画像实现与 PG 同接口

`memory2/store.py:69-82` 的 `memory_items` 与 PG 同形（含 `memory_type` / `happened_at` / `source_ref` / `extra_json`），但没有 `tenant_id` 列，也没有任何画像读写方法（该文件 42 个方法全在召回、FTS、向量侧）。ADR-13 之后画像不落 `memory_items`，所以这张表在本 change 里不动；SQLite 侧要新建四张：

- `tenant_memory_profile_sections` 与 `tenant_memory_profile_entries`（ADR-13 的画像专表）
- `tenant_persona_profiles` 与 `persona_audit_events`（persona 缝用；`persona_templates` 不建，单机没有模板面）

四张表在 PG 由 alembic 管、在 SQLite 由 `memory2/store.py` 的建表语句管，因此**同一份结构有两个出处**，是本 ADR 的主要代价：以后改列必须同时改两处，否则两种后端静默分叉。缓解办法是加一个跨后端结构一致性测试，比对两侧这四张表的列名集合、NOT NULL 集合与 CHECK 取值。

画像存储的 SQLite 实现与 PG 实现同接口（ADR-14 定义的那个接口，不是 `PgMemoryMarkdownStore` 现有的方法集），归属 `infra/storage/tenancy.py:22` 的 `DEFAULT_TENANT`。

单机固定租户要播种两处，都只在行不存在时写入：`tenant_persona_profiles` 一行（`identity` / `personality_rules` / `self_model` 取 `[agent.persona]`，即 `agent/config_models.py:144-147`；`source` 取 `custom`——该列的 CHECK 只允许 `('template','custom')`，见 `bootstrap/db/models/persona.py:87`），以及 `tenant_memory_profile_sections` 的 `relationship_state` 一行（内容同样取 `[agent.persona].self_model`，对应文件后端里 `bootstrap/init_workspace.py:16-27` 播种 `memory/SELF.md` 那一步）。播种之后 config 不再参与当前值。

**不选：让单机强制起 PG。** 那会让 `python main.py` 的零依赖本地跑法消失，Telegram 单机部署跟着要装数据库——等于把一次内部重构转嫁成部署负担。

### ADR-11 静态块缓存 scope 加租户维度，删掉逐块手工防御

`agent/core/prompt_block.py:318` 的 `cache_scope = str(ctx.workspace.expanduser().resolve())` 只有 workspace 路径，缓存键是 `(workspace, block.label, signature)`——**没有租户维度**。

画像三个块今天不出事，只因为它们是 `is_static = False`，走不到查缓存那步（`:326` 只对 static 块算 signature）。而 C9 已经为同一个坑手工补过一次：`IdentityPromptBlock.cache_signature`（`:117-122`）在快照存在时故意返回 None，注释写明"缓存 scope 是进程级 workspace 路径——缓存会跨 tenant 泄漏（fail-closed）"。

那种"每个块自己记得在 cache_signature 里防一次"的模式是脆弱的：把任何一个渲染租户内容的块改成 `is_static = True` 就会重新泄漏，且没有任何编译期或测试期信号。所以本 change 把租户并进 scope（`workspace + tenant_id`），并删掉 `IdentityPromptBlock` 那段手工 fail-closed——防御从"逐块自觉"变成"结构上不可能"。

代价：static 块的缓存命中率按租户数摊薄（同一 workspace 的身份/规则块每个租户各存一份）。这些块体量小、数量有限，且原本在有快照时就已经被手工禁缓存了，所以实际占用不升反降。

### ADR-12 画像快照在 `ContextRequest` 上必填，暴露所有无租户渲染

`ContextRequest.persona_snapshot` 是带默认值的可选字段，`PersonaSnapshot` 的 docstring 还把 None 定义成合法语义（`agent/core/types.py:84-85`：None → 各 block 走单体回退）。我照抄这个形状加 `profile_snapshot`，于是"调用方没传"在类型上合法，而真实后果是画像注入静默消失。

生产侧两个 render 调用点里，`agent/lifecycle/phases/prompt_render.py:87` 传了 persona 快照；`agent/lifecycle/phases/before_reasoning.py:78` 的 `_PromptWarmupModule` **两个都不传**，只为预热缓存而渲染一遍完整 prompt 并丢弃结果。它的副作用是覆盖 `ContextBuilder._last_debug_breakdown`（`agent/context.py:319`），让 admin/debug 的 source breakdown 显示一份"无租户 + 全局文件"的组装结果。

选定：`profile_snapshot` 与 `tenant_id` 在 `ContextRequest` 上必填，None 或空不再是合法输入。warmup 因此必须显式回答"预热用的是谁的画像"——做法是让它复用本轮真实租户的快照（`BeforeReasoningCtx` 与 `PromptRenderCtx` 需补 `tenant_id`），而不是再拿空形状渲染。

**不选：保持可选 + 只在 default 租户时从全局 store 兜底。** 那样 warmup 永远不传任何东西，也就永远不会被问到，跨租户静默失效的形状原样保留。

**不选：删掉 warmup。** 它的预热对首 token 延迟有作用，且这是独立于本 change 的性能决策；本 change 只要求它带上租户身份。

### ADR-13 画像层另立专表，与记忆召回分表

画像原本计划写进 `memory_items`，复用 `memory_type` 判别。改为另立两张专表。

**理由一：召回面的类型过滤不是结构性的。** `plugins/default_memory/engine.py:1440-1448` 的 `_resolve_memory_types` 只在 `filters.kinds` 非空或 `intent == "procedure"` 时返回类型白名单，其余意图返回 None，即不加过滤；`bootstrap/db/models/memory.py:17` 的 `memory_type` 列是 `String(64)`，无 CHECK 约束。所以画像行与召回行共表时，一次普通召回可以把长期画像整段当检索结果返回，而这恰好是本次要堵的那类泄漏。分表之后画像表不出现在任何召回 SQL 里，三条 lane（向量 / BM25 / LIKE）都打不到它。

**理由二：单行整块类与追加日志类的物理需求相反。** 整块类每租户恒定三行、频繁整行覆写；日志类只追加、按时间倒序读、体量随对话增长。共表意味着两套访问模式共用一份索引与分区策略，而 `memory_items` 在 PG 侧是按 `tenant_id` 分区的（`alembic/versions/a3d5c7e9f1b2_partition_memory_items.py`），画像写入并不需要分区。

两张表：

| 表 | category | 行形状 | 约束 |
|---|---|---|---|
| `tenant_memory_profile_sections` | `long_term` / `recent_context` / `relationship_state` | 单行整块，upsert | `UNIQUE (tenant_id, category)` |
| `tenant_memory_profile_entries` | `pending` / `history` / `journal` | 只追加 | `happened_at NOT NULL`、`UNIQUE (tenant_id, dedup_key)`、`INDEX (tenant_id, category, happened_at DESC)` |

两张表都带 `tenant_id` 列——SQLite 侧也带。这修掉了原计划的一处验证盲区：`memory2/store.py` 的 `memory_items` 没有 `tenant_id` 列，所以单机的"隔离"只等于"只有一个租户"，跨租户隔离断言在 SQLite 上验不出东西，只能全部押在 PG 后端上。画像专表两侧都有租户列，隔离断言在两种后端都能跑（造两个租户 id 即可）。

**`dedup_key` 取代 `app_configs` 承接幂等。** 原计划让所有需幂等的写入走 `_append_once_with_index`，它把去重键写进 `app_configs`。那条路有两个缺陷：那张表的主键是 `key` 单列 `String(128)`（`bootstrap/db/models/extras.py:20`），`tenant_id` 只是 `TenantMixin` 的非主键列，所以键是**跨租户全局**的，租户 B 的写入会被租户 A 留下的同名键静默判为重复（`return False`，无日志）；而 `core/memory/markdown.py:191-200` 生成的 `source_ref` 是 message id 的 JSON 数组，id 形如 `<session_key>:<seq>`（`session/store.py:1014`），三条以上消息就超 128 字符，PG 抛 22001，SQLite 不校验 VARCHAR 长度所以文件侧一直没暴露。新表的 `dedup_key` 是 TEXT、无长度上限、天然带租户维度，两个缺陷一起消失。

代价：PG 侧一个新 alembic 版本，SQLite 侧两条建表语句，同一份结构两个出处（与 ADR-10 的 persona 表同一个代价，用同一个跨后端一致性测试兜）。预取从一条 SELECT 变两条，仍在同一次数据库会话内，ADR-1 要求的「persona 与画像来自同一瞬间」不受影响。

**不选：一张表 + `category` 判别列。** 省一条 SELECT 与一份迁移，代价是 `happened_at` 对整块类无意义、对日志类必填，同一列两种语义；「每租户每类只一行」只能写成偏索引（`WHERE happened_at IS NULL`）；两套访问模式仍共用一份索引与分区策略。

**不选：继续用 `memory_items` 加类型。** 除了理由一与理由二，还要在分区表上开画像写入路径，而画像写入的量级完全用不上分区。

### ADR-14 画像存储接口按新表重设，文件时代的方法删除而不是重新实现

原计划是「SQLite 侧接口与 `PgMemoryMarkdownStore` 对齐」。改为按 ADR-13 的两张表重设接口，同时删掉 `MemoryProfileApi`（`core/memory/markdown.py:61-80`）里那些只在文件语义下成立的方法。

前提是 `PgMemoryMarkdownStore` 本来就不满足那个协议（见 Context 段）：协议 10 个方法它实现 6 个。「对齐」等于先给 PG 侧补 4 个方法，其中多数在表上没有意义。

删除清单与依据：

| 方法 | 处置 | 依据 |
|---|---|---|
| `read_self` / `write_self` | 删 | ADR-9，访问路径归 persona 缝 |
| `get_memory_context` | 删 | 生产零调用方（只剩 `core/memory/runtime.py:48-49` 的代理、`agent/memory.py:252` 的实现、一处注释与一个 docker 调试探针）；task 3.4 已把排版移进 `LongTermMemoryPromptBlock` |
| `has_long_term_memory` | 删 | 全仓库只有协议声明（`:80`）、`MarkdownMemoryStore:996` 与 `runtime.py:51` 的代理，零生产调用方 |
| `read_recent_history` | 删 | 它是 `read_history` 的重命名别名（`core/memory/markdown.py:986-987`）；`runtime.py:45-46` 的代理零生产调用方 |
| `backup_long_term` | 删 | 它把 `MEMORY.md` 复制成 `MEMORY.bak.md`（`core/memory/markdown.py:989-995`）。全仓库无任何读取方——唯一生产调用点是 `proactive_v2/memory_optimizer.py:283`，写后即弃。它的恢复手段是人手改文件，随文件后端一起消失 |
| `append_history` / `append_pending` / `clear_pending` | 删 | 零生产调用方；写入统一走 `*_once` 幂等路径，pending 的消费改由取走即删承担 |
| `snapshot_pending` / `commit_pending_snapshot` / `rollback_pending_snapshot`，以及文件侧的 `_recover_pending_snapshot` | 删 | 见下 |

**pending 的两阶段提交在表存储下塌缩。** 文件后端要靠原子重命名把待合并内容先藏起来才能做到崩溃可恢复：`agent/memory.py:204-218` 重命名、`:228-244` 合并回来、`:246-250` 处理崩溃残留。表存储下消费就是一条 `DELETE FROM tenant_memory_profile_entries WHERE tenant_id=:t AND category='pending' RETURNING content`，取走与删除同事务，回滚就是事务中止，崩溃残留无从产生。`MemoryOptimizer._optimize`（`proactive_v2/memory_optimizer.py:261-298`）那个 try / `except BaseException` / 双分支 commit-rollback 的骨架随之简化成一次事务。

**取走必须落在调用方的事务里**，否则上面那段塌陷不成立。`take_pending` 的会话参数因此是必填而不是可选：让它自己提交，删除就在合并之前落库，合并失败时 pending 已经没了——比它要取代的重命名方案更糟（重命名至少还能被 `_recover_pending_snapshot` 捡回来）。两侧各有一条用例把「中止后行仍在」钉住。代价写在这里：这笔事务横跨一次 LLM 调用，PG 侧那条连接在等待期间处于 idle-in-transaction，6.11 落地时按这个前提定超时与重试，别顺手把 `take_pending` 改成自提交。

留下的接口按两张表分两组，实测是 **六个方法**（少于原先估的「约 9 个」：`relationship_state` 复用整块组的读写，`take_pending` 一步取代原来的三步）：

- **整块组**：读一类、写一类。`long_term` 与 `recent_context` 由画像层直接读写；`relationship_state` 的读写由 persona 缝独占（ADR-9），画像层的存储实现提供方法但不暴露给组装侧。
- **日志组**：追加一行（带 `dedup_key`）、按时间读若干行、`pending` 取走即删。

两个写入方法（`write_section` / `append_entry`）收一个**可选** `session`：给了就写进调用方的事务且不提交，没给就自开一次借用并提交。「提交与否」在每个实现里只决定一次，所以不会出现某个写方法忘了提交、或某个本该并入调用方事务的写把别人的事务一起提交掉——6.6 的「画像行与 `persona_audit_events` 同事务」靠的就是这个参数。

存储实现只做 async。ADR-15 之后同步侧不再有画像读取调用点，因此不需要 ADR-1 之外的同步门面。SQLite 侧的 `memory2/store.py` 是同步 `sqlite3`，照仓库既有先例用 `asyncio.to_thread` 包（`core/memory/markdown.py:783-785` 对 `read_history` 就是这么做的），不在协议层为两种后端分叉出同步与异步两套签名。

**外部会话参数在协议层收成较泛的类型。** ADR-1 要求 persona 与画像出自同一次会话借用（两次借用之间有时间缝，并发 optimizer 写入会让同一轮拿到两个瞬间的拼贴），而 ADR-14 又要求 SQL 只有一份——两条叠在一起，只能让存储提供「在你借好的会话上跑」的方法，而不是自己再开一个。PG 侧传 `AsyncSession`，SQLite 侧传 `sqlite3.Connection`：这两种类型没有共同接口（`AsyncSession.execute` 是协程、`sqlite3.Connection.execute` 是同步并返回 Cursor），所以该参数在协议层只能收成较泛的类型，契约写在文档字符串里。**这是本 ADR 明确接受的一处代价**：类型检查帮不了「传错后端会话」这种情况，靠 6.5 那条两侧共用一份参数化清单的用例兜住。

**不选：把批量读拆成「存储只出 SQL 文本、解析器自己执行」。** 类型确实干净（`Text` 两种后端通用），但整块类与日志类的读取带字符预算与截断语义（文件版的 `read_history(max_chars)` 是取尾部），这些逻辑得留在存储里，只出 SQL 文本会把它们推到调用方。

**SQLite 实现另有两个同步方法名，这不是遗漏，别把它改回 async。** 单机只有一个租户、一条连接、一把 `threading.RLock`，而该实现的 async 方法每次都经 `asyncio.to_thread` 换线程。`threading.RLock` 只对属主线程可重入，所以「async 上下文管理器持锁 + 内部再调本实现的方法」这个形状会让事务里的第二条写入在另一个线程上等同一把锁——死锁。因此跨多条语句的事务（`6.6`：画像行 + 审计行）只能由调用方一次 `to_thread` 交进一个同步函数体，在里面 `with store.transaction() as db:` 拿连接、`write_section_sync(db, ...)` 写。这两个名字只存在于 SQLite 实现类，**不进 `MemoryProfileStoreApi`**（协议仍是那六个 async 方法）；PG 侧不需要等价物，`async_sessionmaker` 本来就是借用入口。**接受的不对称**：同一个后端类上有 async 协议方法与同步事务方法两套名字，代价是读代码的人需要本段解释为什么。

类名随之要改：`PgMemoryMarkdownStore` 里的 "Markdown" 在文件后端摘除之后是假的，`agent/memory.py` 的 `MemoryStore` 同理。PG 侧那个类保留为画像存储的实现（删掉上表那些方法、把留下的重定向到两张新表），改名为不含 "Markdown" 的名字；SQLite 侧新建一个同名同接口的实现。名字里不出现后端之外的限定词，避免再出现「名字描述的是被删掉的那一层」。

**不选：给 `PgMemoryMarkdownStore` 打补丁补齐协议。** 要保住的 10 个方法里 4 个在表上没有意义，补齐等于写 4 个空实现——现状那三个 pending 方法（`agent/memory_pg.py:176-188`）就是这么来的，而空实现不报错，只在 optimizer 侧表现为静默失效。

**不选：按原协议在两张新表上重写一遍。** 同样的接口会把文件语义带进表里，`backup_long_term` 与 `snapshot_pending` 只能再写一遍空实现。

### ADR-15 三条可加载组装路径共用段渲染器，`ContextBuilder` 不再持有画像存储

`ContextBuilder._build_system_prompt_result`（`agent/context.py:332-360`）已经是「收快照、返回段、不碰消息封装」那个函数，返回 `SystemPromptBuildResult`（`agent/core/prompt_block.py:279-283`，公开数据类）。主链自己就在用：`PromptAssembler.assemble` 在 `assembler.py:108` 调它，再在 `:155` 加消息封装。它是私有的，唯一调用方是 `assemble`。

**选定：提为公开的 `ContextBuilder.render_sections(request)`**，并把「哪些段进上下文帧、哪些进系统提示」的拆分（现在写在 `assembler.py:133-142`，判据是 `_CONTEXT_FRAME_SECTIONS`，`:49-53`）一并移进来，返回时两组分开。`assemble` 改为调这个公开方法，主链行为不变。插件侧只依赖一个窄协议：

```python
class ProfileSectionRenderer(Protocol):
    def render_sections(self, request: ContextRequest) -> SystemPromptBuildResult: ...
```

两条在范围内的旁路改为调它，删掉各自的画像读取：`plugins/proactive_flow/prompt.py:149-155` 的三次同步读、`:248-257` 的两个读取器、`:159-161` 三个重复段名；`plugins/drift_flow/runtime.py:597-602` 的同步读与 `:673-677` 的重复段。

这样做的直接收益是把一类失效从结构上消掉：同步调用方拿到未 await 的协程对象时，协程是真值，`str(x() or "")` 会把 `<coroutine object ...>` 送进提示，而不报错也不留空。task 2.5 已经实测到实物——真调 `build_runtime_context_message` 得到的帧正文里，`## self_model` / `## long_term_memory` / `## recent_context` 三段全是 `<coroutine object ...>`。共用渲染器之后插件侧根本不存在对存储的读取调用，协程无从产生。

**预取落点各一条，都在已有的 async 入口，不需要把同步方法翻成 async：**

- drift：`_build_runtime_context_message` 已是 `async def`（`plugins/drift_flow/runtime.py:589`）。
- 主动推送：三层同步（`plugins/proactive_flow/modules.py:21` → `plugins/default_proactive/runtime.py:530` → `plugins/proactive_flow/prompt.py:149`），在 `modules.py:19` 的 `async def run` 预取，挂 `AgentTickContext` 新字段往下带。

**唤醒不在本 change 范围内，因为它今天不可加载。** `plugins/wake_proactive/runtime.py:48` 无条件执行 `from session.embedding_store import MessageEmbeddingStore`，而 `session/embedding_store.py` 在本仓库任何分支上都不存在（`git log --all -- session/embedding_store.py` 零提交，该 import 自 `168fed4a3` 起就是坏的）。`agent/plugins/manager.py:1598-1607` 把由此产生的 `ImportError` 吞成一条 `logger.warning` + failed gate + `return None`，所以后端照常启动，wake-proactive 从未加载过。它的接入方式已经确定——`decide()`（`:342`）已是 `async def`，三个读取点（`:353`、`:391`、`:685`）都在其下游，预取一次挂 `WakeContext` 即可，无需翻 async；该结构本来就是「先取好文本再当参数传」（`build_messages(ctx=..., memory_text=...)`）——但补齐导入本身是另一件事，归另一个 change。**本 change 保留 2.4 静态扫描对 `plugins/wake_proactive/` 的覆盖**：静态规则读源码不需要该模块能导入，而它恰好是唤醒恢复加载能力时的看守，防止第四条带着自己的画像读取上线。

**装配**：主动推送与 drift 共用生产环境唯一的那个 `ContextBuilder` 实例（`bootstrap/tools.py:577-579` 建，`agent/looping/core.py:178` 用 `deps.context`），因此共用同一份 `SectionCache`；该缓存的 scope 已带租户维度（ADR-11），共用是安全的，且省掉重复渲染静态段。给 `ProactiveRuntimeScope`（`proactive_v2/runtime_scope.py:25` 旁）加一个渲染器字段，在 `proactive_v2/loop.py:163-170` 填充。唤醒将来恢复加载后走同一个字段，不需要另开装配口。

**连带：`ContextBuilder` 的 `memory` 参数删除。** `ctx.memory` 在全部九个块里只被读一次，在 `agent/core/prompt_block.py:171`，即 `SelfModelPromptBlock` 无快照时的回退分支。ADR-9 删 `read_self`、task 6.8 让单机也装配 resolver（快照恒非 None），这一行随之消失。于是 `bootstrap/tools.py:577-579` 不再传 `memory_runtime.markdown.store`，`bootstrap/wiring.py:42` 的工厂签名少一个参数，`agent/looping/core.py:169-178` 那段「有 markdown 就用它的 store、否则退回 memory_engine」的三元选择整段删除。`ContextBuilder` 从此不做 I/O。

**不选：旁路改调公开的 `ContextBuilder.render`。** `render`（`:294-330`）在段之外强制加消息封装，而封装是对话轮次形状：`[system, *history, context_frame?, user(current_message)]`（`agent/context.py:99-114`），`history` 与 `current_message` 都是 `ContextRequest` 必填字段。主动推送的开场消息不是用户说的话，当 `current_message` 喂进去会被盖上用户消息时间戳并可能触发渠道策略改写（`agent/context.py:94-97`）；唤醒只要系统提示，它在 `plugins/wake_proactive/runtime.py:403-429` 把拿到的消息拆开重组。两条都得传 `history=[]` 加一句假消息，然后把封装扔掉。

**不选：只抽一个「画像段」helper 函数。** 共用渲染但不共用顺序、缓存与 breakdown，两条组装管线并存，段名仍在两处声明。

**不统一的一处差异，显式记录。** 主链把 `long_term_memory` 与 `self_model` 放系统提示（`_CONTEXT_FRAME_SECTIONS` 只含 `active_skills` / `recent_context` / `retrieved_memory`），而主动推送把这三段全放上下文帧（`prompt.py:159-161` → `:188`），帧开头带一句「以下内容由系统提供，不是用户陈述……禁止在回复中引用、复述、展示本提醒本身」（`assembler.py:75`）。同名段在两条路径下给模型的位置与附带约束不同。`render_sections` 两组分开返回、各路径自己挑，因此本次保持各自现状；统一放置会改模型行为，需要跑评测判断好坏，属另一个 change。

**接上共用渲染器之后，帧里的段标题有一处文字变化。** 画像块是给系统提示那条路渲染的——那条路是正文拼接，标题由块自己给（`## Long-term Memory`、`## <名字> Self-Perception`）；旁路把同一段放进上下文帧，而 `build_context_frame_content` 按段名再加一层 `## long_term_memory`，两边一撞就是两个标题（4.5 落地后由 4.6 的用例撞出）。选定：去重规则放在共用的帧组装器里——段内容以 `## ` 开头时不再叠段名标题。于是旁路那两段的标题文字变成块自己的写法（`## long_term_memory` → `## Long-term Memory`），`recent_context` 不受影响（该块返回裸正文）。规则不放这里而放各旁路自己剥标题，就是 ADR-15 拒绝的「各路径再实现一遍」；主链不受影响——它进帧的三段里 `active_skills` 用 `# ` 一级标题、另两段是裸正文，实测主链帧相关用例零变化。

## Risks / Trade-offs

- **预取让每轮多两条 SELECT**，即使该轮某些块用不到 → 两条都是按 `(tenant_id, category)` 的窄查询，走唯一索引与时间索引；快照按 persona 快照同样的方式参与 prompt 缓存签名，未变更时不重复组装。
- **迁移目标选错会造成双份画像**：本地 PG 里 `dev`、`smoke_7afc7830` 与两个 `pilot-*` 租户已有 `memory_items` 的 self 行，全局 markdown 又有一份，新表再迁一次就是第三份 → 迁移脚本默认 dry-run 并打印目标租户当前各类的字符数（含新表已有内容与 `memory_items` 里的旧 self 行），人工确认后再执行。旧 self 行不删，它随召回面继续可见，属于 ADR-9 之后的一条待清理数据，清理动作另开 change。
- **漏改某条旁路会把内容写回共享文件**，而写回后不会再被读到，故障表现为「记忆静默丢失」而不是报错 → 用 spec 中「多租户路径无共享目录读写」那条静态扫描 Scenario 做成可执行测试。扫描范围 SHALL 覆盖 ADR-4 的 4 个独立根所在模块，**并覆盖三条旁路**：`plugins/proactive_flow/`、`plugins/drift_flow/`、`plugins/wake_proactive/`。原计划的扫描清单只有 `plugins/drift_flow/`，另两条不在内，而它们恰恰是同步调用 async 存储、最容易把协程对象塞进提示的地方。
- **测试断言面大**：断言 `workspace/memory/` 路径或 markdown 单例语义的用例，加上三条旁路与 `ContextBuilder` 构造签名的用例（清单见 proposal.md — Impact）→ 先改 `tests/memory_fakes.py:25` 这个共用替身，让其余用例跟着替身收敛，避免逐文件手改。
- **`content_hash` 跨进程不稳定**：全仓库 6 处 `str(hash(正文))`——`agent/memory_pg.py:232/252/295`、`bootstrap/persona.py:113/131`、`bootstrap/db/repository/persona_repo.py:153`——而 Python 的字符串 hash 按进程随机化、仓库未固定 `PYTHONHASHSEED`，所以任何靠它去重的路径在重放时都失效。**这 6 处在本 change 之后全部消失**，不是被修好而是被删掉：新表用 `(tenant_id, category)` 与 `(tenant_id, dedup_key)` 做键，不需要正文哈希（ADR-13、ADR-14）。原计划「迁移一律走 `_append_once_with_index`、禁止 `_append_entry`」那条强制令随之作废——`_append_once_with_index` 依赖的 `app_configs` 键有 128 字符上限且跨租户全局，两个缺陷见 ADR-13。
- **共用段渲染器会同时改变三条路径的提示内容** → 缓解有两条：`render_sections` 把系统提示段与上下文帧段分开返回，各路径保持现状放置，主动推送那三段的落点不变；主链只改调用名，`assemble` 的输入输出与顺序不动。验证方式是断言三条路径拿到的画像段内容一致，而不是断言提示全文一致。
- **唤醒插件从未成功加载，而缺陷被静默吞掉**：`agent/plugins/manager.py:1598-1607` 把插件 `ImportError` 降级成一条 `logger.warning` + failed gate + `return None`，后端照常起得来。这条错误处理本身是有意的（单个坏插件不该拖垮启动），但它的代价是「装了却从没跑过」的功能可以长期无人察觉——`plugin_packages/wake-proactive/package.toml` 声明 `provides = ["proactive.runtime"]`，而 `MessageEmbeddingStore` 的缺失自 `168fed4a3` 起就存在，主 spec、两份 roadmap 与全部测试都没有覆盖过该插件。本 change 只把它从组装路径范围里如实移出并保留静态扫描看守，不在修隔离的 change 里顺手激活一条从未运行过的推送路径。**failed gate 是否在 admin 控制台可见、要不要给「安装后长期加载失败」加一条启动期告警，属另一个 change。**
- **`ContextFactory` 的签名变化**：`bootstrap/wiring.py:22` 现在是 `Callable[[Path, Any], Any]`，删掉 `ContextBuilder` 的 `memory` 参数后变 `Callable[[Path], Any]`。`_CONTEXT_WIRING` 只有一个条目（`:41-43`），所以爆炸半径小，但这是一个公开类型别名的破坏性改动，配置里写了自定义 context wiring 的部署会在启动时报错而不是静默降级。
- **回滚**：多租户模式下画像层无 feature 开关可退回全局文件——退回即重新引入泄漏。回滚策略是代码回滚加一个 alembic downgrade：两张画像表是纯新增，`memory_items` 与 persona 三表都不动，downgrade 只 drop 新表，已迁入的画像数据丢弃但不影响旧行为。共享目录原文件按 spec 要求从未被改写，因此回滚后旧行为立即可用。

## Migration Plan

1. PG 侧加一个 alembic 版本建两张画像表；SQLite 侧建表语句加同样两张（ADR-13、ADR-10）。
2. 按 ADR-14 的接口实现 PG 与 SQLite 两份画像存储，删除 `MemoryProfileApi` 里文件时代的方法与 `PgMemoryMarkdownStore` 的对应实现。
3. 提公开 `ContextBuilder.render_sections`，定义 `ProfileSectionRenderer` 窄协议，删 `ContextBuilder` 的 `memory` 参数（ADR-15）。
4. 实现快照预取与 `MemoryProfileSnapshot`，接入画像块；`TurnContext` 补 `tenant_id`；三条旁路改用共用渲染器并在各自 async 入口预取。
5. 删除 `core/memory/runtime.py` 的无租户代理，逐个改造 ADR-4 的 4 个独立根。
6. 抽 `tenant_category_root`，blob_store 改用。
7. 迁移脚本：dry-run 输出目标租户现状 → 显式执行 → 重放一次验证幂等。
8. 建 `admin1` / `admin2` 常驻租户与 dev-only 切换入口，跑跨租户隔离断言（两种后端各跑一次，见 ADR-13）。
9. 门禁：`pyright --level error`（project 与 tests 两配置）+ `pytest -q -W error tests/`。

生产部署顺序与回滚同上：先跑 alembic upgrade 建表（无数据变化，可提前），再 dry-run 看归属，再迁 owner 租户。共享 markdown 文件在验证期内不删除。
