## Context

动机与实测证据见 `proposal.md` — Why，行为契约见 `specs/tenant-memory-profile/spec.md`。这里只记约束现状。

当前画像层的读路径是一条代理链：`core/memory/runtime.py:36-49` 的 `read_long_term` / `read_self` / `read_recent_context` / `get_memory_context` 全部转发到同一个 `markdown.store` 实例；该实例在 `bootstrap/memory.py:170`（主进程）与 `:246`（dashboard）各建一次，构造时把目录固化成 `workspace / "memory"`（`agent/memory.py:50-51`）；`agent/looping/core.py:169-174` 在 AgentLoop 构造期缓存它并作为 `memory_profile` 注入 `ContextBuilder`（`agent/context.py:226-237`），每轮组装 `TurnContext` 时原样透传（`:335-346`）。AgentLoop 是每进程一个，循环内靠 `msg.tenant_id` 区分租户，不存在 per-tenant AgentLoop。

两个已存在、可以直接复用的机制：

- **per-turn 不可变快照**。`bootstrap/persona.py:resolve_persona_snapshot` 的契约是「turn 入口在 tenant lane 内调用一次，取该 tenant 的 PersonaProfile 与 RelationshipState 组成不可变快照随 ContextRequest 走 prompt 组装」。这正是画像层缺的那个形状。
- **租户 category 根**。`agent/tools/path_resolver.py:52` 与 `bootstrap/attachments/blob_store.py:71-85` 已经用 `<workspace>/tenants/<tenant_dirname(tenant_id)>/<category>` 这套布局，`tenant_dirname`（`path_resolver.py:109`）是公开 helper。

约束：`core/memory/markdown.py:61-80` 的 `MemoryProfileApi` 协议全部同步；`agent/memory_pg.py` 的 `PgMemoryMarkdownStore` 全部 async（`read_long_term:62`、`read_self:135`、`read_recent_context:127` 等），且全仓库无任何实例化点。prompt 块侧 `agent/core/prompt_block.py:168/185/216` 是同步调用。

## Goals / Non-Goals

**Goals:**

- 五类画像的唯一事实源落到按租户解析的表存储（PG 用 PG 表，单机用 SQLite 表），读取按租户、写入按租户，共享目录退出运行时路径。
- 同步的 prompt 块协议不被改成 async，改动不外溢到会话内容与召回面。
- 单机与多租户共用同一套按租户解析的画像接口，单机归到固定租户；不再维护第二份文件实现。

**Non-Goals:**

- 不在多租户模式下建立 per-tenant 的 markdown 目录。选了 PG 之后，「把根目录信息搬到 admin1 的文件夹」这件事变成「把根目录内容迁进 admin1 的 PG 行」，磁盘上不会出现 `tenants/<admin1>/memory/`。
- 不改 `PersonaProfile` / `RelationshipState` 的快照与固定语义。
- 不做画像层的双写（PG + 文件）兼容期。

## Decisions

### ADR-1 每轮预取不可变画像快照，而不是把 prompt 路径改 async

同步/异步错位有三种解法。

**选定：预取快照。** turn 入口在租户 lane 内一次性把五类画像读成不可变对象，随 ContextRequest 传给 prompt 组装；两个画像块从快照同步读，`MemoryProfileApi` 协议与所有同步调用点不动。这样做的额外收益：五类一次批量查询，比逐块各查少四次往返；并且天然满足 `persona-relationship` 已经要求的「正在进行中的 turn 继续使用其组装时的快照，不中途替换」。

**不选：把 prompt 组装路径改 async。** 要连带改 `agent/context.py` 的组装入口和 drift、proactive、optimizer、dashboard 四条旁路的调用点，回归面覆盖整个 prompt 组装，而收益只是省一层快照。

**不选：给 PG 实现加同步门面。** AgentLoop 已经在事件循环内，同步方法里再阻塞等待异步会自锁；`asyncio.run` 在运行中的 loop 里不可用；改用独立线程加独立 loop 又会让 SQLAlchemy 异步 engine 跨事件循环使用。要绕开就得再维护一套同步连接池，等于双份存储层。

### ADR-2 新建 `MemoryProfileSnapshot`，不扩 `PersonaSnapshot`

把五类画像塞进现有 `PersonaSnapshot` 会让 `persona-relationship` 的「PersonaProfile 提交后固定」语义被误读成画像层也固定——画像是每轮可变的。两者并列挂在 `TurnContext` 上，各自独立演进。`TurnContext`（`agent/core/prompt_block.py:24-33`）补 `tenant_id` 字段，用于快照缺失时的定位与审计。

### ADR-3 删掉 `core/memory/runtime.py` 的无租户代理方法，改为显式收 tenant_id

`read_long_term` / `read_self` / `read_recent_context` / `get_memory_context` 这四个代理方法签名里没有租户，是泄漏的直接来源，保留它们就等于保留一个「随手一调就读全局」的入口。改为按租户解析：调用方要么已经持有租户上下文，要么拿不到画像。

**不选：用 contextvar 隐式携带租户。** 隐式上下文正是 `tenant-tool-isolation` 要收敛掉的「进程级共享可变上下文 + 参数可覆盖」模式，同一进程内两个租户的 turn 交错时，隐式传播链上任一处漏 `copy_context()` 就会串。显式传参难看但可静态检查。

### ADR-4 六处启动期单例引用改为「按租户解析」的入口

`bootstrap/app.py:666`（optimizer）、`:738`（dashboard）、`:775`（proactive）、`bootstrap/tools.py:579`（ContextBuilder）、`:597`（maintenance 绑会话生命周期）、`plugins/drift_flow/factory.py:114`（drift 经 `scope.memory`）现在都在启动期抓住同一个 store 实例长期持有。改为持有「租户 → 画像读写入口」的解析器，在各自真正处理某个租户数据的时刻解析。其中 optimizer 与 proactive 本来就按会话逐个处理，租户上下文在手上，改动是把它传下去。

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

**日期：用表上已有的 `happened_at` 列。** `memory_items` 从初始迁移起就有 `happened_at TIMESTAMPTZ`（`bootstrap/db/models/memory.py:25`、`alembic/versions/47460ba069a5_initial.py:104`），分区模板里也带着它（`alembic/versions/a3d5c7e9f1b2_partition_memory_items.py:157`）。所以「一条一行 + 日期占列」不需要新迁移。

真正的缺口是实现没填这一列：`_append_entry`（`agent/memory_pg.py:240-258`）只写了 `tenant_id` / `id` / `memory_type` / `summary` / `content_hash` / `status` / `created_at` / `updated_at`，日期于是被降级拼进 `summary` 前缀成 `**2026-10-11** <正文>`（`:119`）。本 change 补上：journal 与 history 写入时事件日期落 `happened_at`，`summary` 不再重复携带日期——同一事实只存一处。

`created_at` 不能替代它：迁移历史 journal 时 `created_at` 等于迁移当天，而事件日期来自 `journal/YYYY-MM-DD.md` 的文件名，两者必须分开。

索引现状与代价：`memory_items` 上只有 `ix_memory_items_content_hash`（tenant_id, content_hash, memory_type 唯一）与 `ix_memory_items_type_status`（tenant_id, memory_type, status），`happened_at` 无索引。按天查询走 `tenant_id + memory_type='journal'` 已能把扫描范围限定到该租户的 journal 行集，再对 `happened_at` 过滤。本 change 不为此新加索引——journal 目前两侧都没有读接口（markdown 侧同样只有 `append_journal`，见 `agent/memory.py:112`），它不进 prompt，加索引属于无消费者的优化。

### ADR-9 自我认知的读写都归 persona 那条缝，画像层不再管 self

单体（markdown）不再作为画像层的第二条实现路径：五类画像读写只走按租户解析的表存储，单机归到 `infra/storage/tenancy.py:22` 的 `DEFAULT_TENANT = "default"`。

`self` 这一格整体移出画像层，读写都归 persona 那条缝，两种后端走同一套语义：

- **读**：`SelfModelPromptBlock` 只读 `persona_snapshot.relationship_state`，删掉按模式二选一的 else 分支（`agent/core/prompt_block.py:162-173`）。该字段全仓库只有这一个消费者（定义 `agent/core/types.py:92`，赋值 `bootstrap/persona.py:60`），所以删分支不牵连别处。
- **写**：optimizer 统一走关系状态写缝（`proactive_v2/memory_optimizer.py:320-337`），该缝在 PG 与 SQLite 各一个实现，两者都同事务写审计事件。画像层的 `read_self` / `write_self`（`agent/memory_pg.py:135-139`）随之删除。
- **装配**：`bootstrap/app.py:649` 的 `if self.webchat_durable is not None:` 放宽，两种后端都装 resolver 与写缝。

快照为 None 时该块跳过注入，SHALL NOT 回退读共享文件。多租户下 None 意味着该租户没有 PersonaProfile，而这类租户本来就被 onboarding 门禁挡在对话之外（`bootstrap/chat_api.py:377-378`）；单机下由播种保证恒有该行（ADR-10）。

代价有两条：要给 SQLite 补 persona 表（ADR-10）；要推翻 `persona-relationship` 里「单体 / SQLite dev 模式 SHALL 继续沿用现有文件语义（行为不变）」那句，本 change 因此带该 capability 的 MODIFIED delta。

**不选：只统一读、写侧留两条入口。** 那样外部行为确实一致，但同一行数据长期有两个写入者、按不同分支被调用，改一边忘一边是静默失效而不是报错。

### ADR-10 SQLite 侧补 persona 两张表与画像实现

`memory2/store.py:69-82` 的 `memory_items` 与 PG 同形（含 `memory_type` / `happened_at` / `source_ref` / `extra_json`），但没有 `tenant_id` 列，也没有任何画像读写方法（该文件 42 个方法全在召回、FTS、向量侧）。五类画像需要一个 SQLite 实现，接口与 `PgMemoryMarkdownStore` 对齐。

自我认知那一格还要 SQLite 侧的 persona 表：`tenant_persona_profiles` 与 `persona_audit_events` 两张，`persona_templates` 不需要（单机没有模板面）。这两张表在 PG 由 alembic 管、在 SQLite 由 `memory2/store.py` 的建表语句管，因此**同一份结构有两个出处**，是本 ADR 的主要代价：以后改列必须同时改两处，否则两种后端静默分叉。缓解办法是加一个跨后端结构一致性测试，比对两侧 persona 表的列集合与 CHECK 取值。

单机固定租户的 `tenant_persona_profiles` 行在 workspace 初始化时从 `[agent.persona]`（`agent/config_models.py:144-147`）播种，`source` 取 `custom`——该列的 CHECK 只允许 `('template','custom')`（`bootstrap/db/models/persona.py:87`），复用 `custom` 不需要改约束，PG 侧零新迁移。播种只在行不存在时发生，之后 config 不再参与当前值。

**不选：让单机强制起 PG。** 那会让 `python main.py` 的零依赖本地跑法消失，Telegram 单机部署跟着要装数据库——等于把一次内部重构转嫁成部署负担。

## Risks / Trade-offs

- **预取让每轮多一次 PG 读**，即使该轮某些块用不到 → 单条批量查询取五类；快照按 persona 快照同样的方式参与 prompt 缓存签名，未变更时不重复组装。
- **迁移目标选错会造成双份画像**：本地 PG 里 `dev`、`smoke_7afc7830` 与两个 `pilot-*` 租户已有 `memory_items` 的 self 行，全局 markdown 又有一份 → 迁移脚本默认 dry-run 并打印目标租户当前五类的字符数，人工确认后再执行。
- **漏改某条旁路会把内容写回共享文件**，而写回后不会再被读到，故障表现为「记忆静默丢失」而不是报错 → 用 spec 中「多租户路径无共享目录读写」那条静态扫描 Scenario 做成可执行测试，扫描范围覆盖六个启动期引用点所在模块。
- **测试断言面大**：至少 9 处断言 `workspace/memory/` 路径或 markdown 单例语义（清单见 proposal.md — Impact）→ 先改 `tests/memory_fakes.py:25` 这个共用替身，让其余用例跟着替身收敛，避免逐文件手改。
- **`content_hash` 跨进程不稳定，普通追加路径不幂等**：`agent/memory_pg.py:252` 与 `bootstrap/persona.py:113/130` 都用 `str(hash(正文))`，而 Python 的字符串 hash 按进程随机化、仓库未固定 `PYTHONHASHSEED`。因此唯一索引 `ix_memory_items_content_hash`（tenant_id, content_hash, memory_type）跨进程去不了重，迁移重放会插出重复行 → 迁移与所有需要幂等的写入一律走 `_append_once_with_index`（靠 `app_configs` 的稳定 dedup key），并在 tasks 里显式禁止迁移使用 `_append_entry`。
- **SQLite 上没有租户列，隔离断言不能在单机上验**：`memory2/store.py` 的 `memory_items` 无 `tenant_id`，单机固定租户的"隔离"只等于"只有一个租户"。跨租户隔离断言（含 admin1/admin2 那条）必须在 PG 后端上跑，SQLite 侧只验画像读写接口本身。
- **回滚**：多租户模式下画像层无 feature 开关可退回全局文件——退回即重新引入泄漏。回滚策略是代码回滚，PG 中已写入的画像行留存无害，共享目录原文件按 spec 要求从未被改写，因此回滚后旧行为立即可用。

## Migration Plan

1. 实现快照预取与 `MemoryProfileSnapshot`，接入三个画像块；`TurnContext` 补 `tenant_id`。
2. 删除 `core/memory/runtime.py` 的无租户代理，逐个改造六处启动期引用点。
3. 抽 `tenant_category_root`，blob_store 改用。
4. 迁移脚本：dry-run 输出目标租户现状 → 显式执行 → 重放一次验证幂等。
5. 建 `admin1` / `admin2` 常驻租户与 dev-only 切换入口，跑跨租户隔离断言。
6. 门禁：`pyright --level error`（project 与 tests 两配置）+ `pytest -q -W error tests/`。

生产部署顺序与回滚同上：先 dry-run 看归属，再迁 owner 租户。共享 markdown 文件在验证期内不删除。

## Open Questions

- journal 要不要补一个读回接口。`happened_at` 列已经在（ADR-8），缺的只是读方法本身，以及真要做按天大范围扫描时才需要的一个索引。现状两侧都没有读方法，本 change 不建；将来若 drift 或 admin 面需要按天回看事件，另开 change。
