# C18 租户化记忆画像层 — 架构风险与质量评审

产出技能：`risk-quality-reviewer`（配 `business-application-fitness` 参考），图源码用 Graphviz DOT。
评审对象：`openspec/changes/c18-tenant-scoped-memory/` 的四份规划产物（proposal / design / tasks / 两份 delta spec）。
评审日期：2026-10-12。评审基线：分支 `feature/c18-tenant-scoped-memory`，HEAD `0e41cb08e`。

---

## 状态（2026-10-12 计划修订之后）

本评审的正文是修订前那份计划的快照，未改写。修订已落进 `openspec/changes/c18-tenant-scoped-memory/`（新增 ADR-13 / 14 / 15，重写 ADR-1 / 3 / 4 / 8 / 9 / 10 与 Risks，任务数 34 → 47）。逐条状态：

| 编号 | 状态 | 落点 |
|---|---|---|
| F1 pending 空实现导致整理静默停摆 | 消解 | ADR-14 删掉三个 pending 方法，两阶段塌缩成一次事务；task 6.11 |
| F2 协程对象被 `str()` 进提示 | 消解 | ADR-15 共用段渲染器，插件侧不再有存储读取；spec 新增 Scenario；task 2.5 靶测试 |
| F3 `app_configs` 幂等键超 128 字符且跨租户 | 消解 | ADR-13 改用画像表自己的 `dedup_key`；task 7.2 |
| F4 画像行落在召回面上 | 消解 | ADR-13 分表；spec 新增 Requirement「画像与记忆召回分表存放」；task 8.3 |
| F5 护栏扫描范围漏两条旁路 | 已修 | task 2.4 |
| F6 提示模板把共享目录绝对路径交给模型 | **仍开放** | 本轮未处理，见下 |
| F7 单机现存 `SELF.md` 内容会丢 | 已修 | ADR-9 + task 7.1，迁入 `relationship_state` 并覆盖播种值 |
| F8 同租户并发写单行画像 | 消解 | 新表 `UNIQUE (tenant_id, category)` upsert |
| F9 六处 `str(hash())` 不稳定 | 已修 | 全部删除，不是修好；ADR-13 / ADR-14，task 6.4 / 6.6 / 6.7 |
| F10 5 个 Scenario 无对应任务 | 部分修 | 「三条组装路径同源」现有 task 4.5–4.7；其余待复核 |
| F11 eval 与 docker-debug 工具会坏 | 部分修 | `docker/debug/runtime_race_probe.py` 进了 task 6.3 / 9.1 清单；eval 侧未复核 |
| F12 测试断言数量低估 | 已修 | proposal — Impact 重写 |
| F13 spec 的单机判据与实现不一致 | 已修 | spec 里写明判据并与实现对齐 |

修订中新查出、正文里没有的两条：`PgMemoryMarkdownStore` 不满足 `MemoryProfileApi`（协议 10 个方法实现 6 个，接上就在 `core/memory/runtime.py:46` 抛 AttributeError）已记进 design — Context 与 ADR-14；tasks.md「六处装配点」重复计数并漏列 `plugins/wake_proactive/runtime.py:127` 已记进 ADR-4。

不在本 change 处理、由 owner 另开 change 的两条：主动推送与 drift 注入进程级全局身份常量（`agent/persona.py:8/14`），以及 `tenant_persona_profiles.self_model` 作为 admin 读取出口端的是 onboarding 固定值而非当前值。前者在 proposal — Non-Goals 记了一行边界。

仍开放且本轮未认领的一条：**F6**。`prompts/agent.py:38-41` 把共享 `memory/` 下四个 markdown 的绝对路径写进系统提示，`:152` 还指示模型 `read_file {workspace_path}/memory/HISTORY.md`；`prompts/background.py:35-36` 与 `:112-113` 同样列出 `SELF.md` 与 `HISTORY.md`。那个 `{workspace_path}` 来自 `build_agent_static_identity_prompt(workspace=ctx.workspace)`（`prompts/agent.py:24-25`、`agent/core/prompt_block.py:115`），而 `ctx.workspace` 是 `ContextBuilder` 构造时收到的进程级共享 workspace（`bootstrap/tools.py:577-579`）；多租户模式下文件工具的根却是 `<workspace>/tenants/<tenant_dirname>/workspace`（`agent/tools/path_resolver.py:48-54`）。于是提示交给模型的是一组它自己的工具沙箱会拒绝的绝对路径。画像层退出文件路径之后这些文件仍在磁盘上（迁移不删原件，spec 明确要求），所以这不是「路径失效」而是「路径指向一个内容曾经进过 prompt、现在读不到的位置」。

---

## 一、结论先说

这份计划把问题诊断对了，方向也选对了：把画像层从进程级共享文件搬到按租户解析的表存储，是唯一能真正闭合泄漏的做法。design.md 里 ADR-1、ADR-7、ADR-11、ADR-12 四条决策的取舍论证扎实，被否掉的备选也是真备选而不是稻草人。

但是**计划对自己要接上的那个实现类的行为判断有误，而这个误判会静默毁掉记忆整理功能**。另外，计划列出的改造点少了一整类调用方，护栏测试的扫描范围也盖不住它们。

三条最要紧的：

1. `PgMemoryMarkdownStore` 不是「已写好、只差实例化」。它的三个待合并事实方法是空实现，返回值类型也跟文件版不一致。接上去之后，记忆整理器每轮都拿到空的待合并内容，而待合并行永远不会被清掉。故障表现是日志里一行「记忆和 pending 均为空，跳过优化」，不报错。见 F1。
2. 计划只给主对话那条提示组装路径准备了租户快照。主动推送、drift、唤醒推送三条路径各自直接调画像接口，而且是同步调用。换成异步的 PG 实现之后，这三处会把协程对象转成字符串注进提示词。这三个文件在 proposal 的影响清单和 tasks 里都没出现。见 F2。
3. 幂等写入用的那个去重注册表，主键只有 key 一列，跨租户共享；而 key 的内容是一个消息 ID 的 JSON 数组，长度会超出 `app_configs.key` 的 128 字符上限。任务 7.2 要求迁移的所有幂等写入都走这条路。见 F3。

这三条都不是「实现时小心点」能兜住的，它们要么改设计，要么加任务。

---

## 二、评审方法与证据基线

评审按「计划里的每一句结构性断言，回到定义处核对」来做，不从单个用法倒推。核对方式：

- 读了画像层的协议定义 `core/memory/markdown.py:61-80`、文件实现 `agent/memory.py`、PG 实现 `agent/memory_pg.py` 全文。
- 读了数据层定义：PG 侧 `bootstrap/db/models/memory.py` 与 alembic 迁移（含分区重建那份）、SQLite 侧 `memory2/store.py` 的建表语句、persona 三表模型与 C9 迁移。
- 全仓库扫了画像层的构造点、单例取用点、方法调用方、以及五个文件名的直接引用，逐个标注是运行时路径还是离线工具。
- 读了已落地的护栏测试 `tests/memory/test_no_shared_profile_io.py` 全文，核对它的扫描范围与禁用规则。

已实现的部分（tasks 里标 `[x]` 的 1.x、2.x、3.1、3.3、3.4、3.6）与 `afa14b6d4` 那次提交对得上，本次评审不重复核对它们的正确性，只看规划产物与代码现状的差。

---

## 三、发现清单

严重度分四档：**阻断**＝照计划做完会静默丢数据或功能停摆；**高**＝计划的目标达不到，或留下未覆盖的泄漏面；**中**＝质量属性受损但有 workaround；**低**＝进度与可维护性风险。

似然、影响、置信度分列，不合并。

| ID | 严重度 | 似然 | 影响 | 置信度 | 一句话 | 计划里的归属 |
| --- | --- | --- | --- | --- | --- | --- |
| F1 | 阻断 | 高（接上就发生） | 记忆整理停摆，待合并事实永久堆积 | 高 | PG 实现的三个 pending 方法是空的，返回类型也不同 | 无任务覆盖 |
| F2 | 阻断 | 高 | 提示词里出现协程对象字符串 | 高 | 三条同步消费者不在改造清单内 | proposal 影响清单与 tasks 均缺 |
| F3 | 阻断 | 高 | 迁移写入报错；或第二租户写入被静默丢弃 | 高（机制）／中（触发窗口大小） | 去重注册表主键跨租户共享，且 key 会超长 | 任务 7.2 强制走这条路 |
| F4 | 高 | 中 | 召回结果变化，提示词预算被重复内容吃掉 | 高 | 五类画像进 `memory_items` 后对关键词召回可见 | ADR-8 的「结构性保证」只对时间检索成立 |
| F5 | 高 | 高 | 漏改的旁路表现为记忆静默丢失 | 高 | 静态扫描的 7 个文件加 1 个目录盖不住活的装配点 | 任务 2.2 的护栏 |
| F6 | 高 | 高（单机）／已存在（多租户） | 模型读到永不再更新的归档文件 | 高 | 提示词模板硬写了共享文件路径，计划没碰它 | proposal 影响清单缺 `prompts/` |
| F7 | 高 | 高（存量单机升级） | 存量安装积累的自我认知内容丢失 | 高 | 播种从 config 装默认值， evolved 的 `SELF.md` 只报告不导入 | 任务 6.2 与 7.1 的接缝 |
| F8 | 中 | 中 | 同租户重复行，读取取最新那条，旧行成孤儿 | 高 | 单行画像的 upsert 是先查后写，没有唯一约束兜底 | spec 只有跨租户并发场景 |
| F9 | 中 | 高 | 唯一索引去不了重，迁移重放依赖注册表 | 高 | `str(hash())` 的点位比 design 记的多两处 | design.md Risks 已识别但不全 |
| F10 | 中 | 高 | 带未验证的 SHALL 条款归档 | 高 | 15 个 Scenario 里 5 个没有实现任务 | tasks 与 delta spec 的对照 |
| F11 | 中 | 高 | 召回基线改不动也验不了 | 高 | eval 与 docker/debug 工具会跟着失效，计划未表态 | Non-Goals 未提 |
| F12 | 低 | 高 | 进度低估 | 中 | 「至少 9 处」测试断言实为 15 处以上 | proposal 影响清单 |
| F13 | 中 | 低（需不一致的配置） | 泄漏原样保留，且该后端上无法修 | 高 | spec 按「后端或认证」判单机，其余隔离机制按「后端且认证」判多租户 | spec 的模式定义句 |

---

## 四、逐条详述

### F1 PG 实现的 pending 三方法是空实现，记忆整理会静默停摆

**事实。** 文件版的两阶段提交是真的：`agent/memory.py:204-218` 的 `snapshot_pending()` 用 rename 把 `PENDING.md` 原子移走并返回其文本——**这个 rename 本身就是「消费」动作**，移走之后待合并内容就不在了；`:220-226` 的 `commit_pending_snapshot()` 删掉快照，`:228-244` 的 `rollback_pending_snapshot()` 把快照合并回文件。返回类型是 `str`。

PG 版这三个方法是 `pass`，返回类型标注 `-> None`（`agent/memory_pg.py:170-181`）。

**后果。** 记忆整理器 `proactive_v2/memory_optimizer.py:264` 写的是 `pending = self._memory.snapshot_pending()`，整个 `_optimize`（`:261-298`）里没有任何一处调 `clear_pending()`——它靠 rename 的副作用完成消费。接上 PG 实现之后：

- `pending` 恒为 `None`。
- `:266` 的 `if not current_memory and not pending:` 对新租户恒成立，直接打日志「记忆和 pending 均为空，跳过优化」然后 return。
- 对已有长期画像的租户，会用 `None` 当新增事实去调一次 LLM 合并，白白消耗一次调用，且待合并行永远留在表里，下次再来一遍。

**与计划冲突处。** proposal.md:11 说这个类是「已写好但全仓库无任何实例化点」，本 change 让它「变为该层的正式实现」。design ADR-10 与任务 6.4 都说 SQLite 实现要「接口与 `PgMemoryMarkdownStore` 对齐」——把空实现对齐过去，等于把缺陷复制到第二个后端。

**为什么容易漏。** 方法签名存在、类型能过、pyright 不报错。只有跑一次真实的整理流程才看得出来。

**验证方法。** 在 PG 后端上写一条端到端用例：塞两条待合并事实，跑一次 `_optimize`，断言（a）长期画像里出现了这两条事实的内容，（b）`memory_items` 里 `memory_type='pending'` 的行数为 0。这条用例今天在两种后端上都会红。

**修法方向。** PG 侧需要一个等价语义的消费动作，两个选项：一是在同一事务里把 pending 行标记成已消费（改 `status` 或加一列），读取时只取未消费的；二是把 snapshot 语义改成「事务内读出并删除，失败则整事务回滚」，靠数据库事务替代文件版的 rename+rollback。第二个选项更贴近现有调用方的形状，也不需要新列。无论选哪个，SQLite 实现要跟着同语义，并有一条跨后端一致性用例兜住。

---

### F2 三条同步消费者不在改造清单内，会把协程对象注进提示词

**事实。** 除了主对话的提示块，还有三处直接调画像接口，且都是同步调用：

- `plugins/proactive_flow/prompt.py:248-257`：`_read_long_term_text` 与 `_read_self_text`，写法是 `str(memory.read_long_term() or "")`，供 `build_system_prompt` 使用。
- `plugins/drift_flow/runtime.py:598-602`：`raw = str(memory.read_long_term() or "").strip()`，构建 runtime context frame。
- `plugins/wake_proactive/runtime.py:1024-1026`：`_read_memory` 经 `getattr` 拿到 `read_long_term` 再同步调用，结果被 `plugins/wake_proactive/prompt.py:45` 当作「固定 MEMORY.md」嵌进提示。

PG 实现这些方法全是 `async def`（`agent/memory_pg.py:62`、`:127`、`:135`）。

**后果。** 协程对象是 truthy 的，`or ""` 兜不住，`str()` 出来是 `<coroutine object PgMemoryMarkdownStore.read_long_term at 0x...>`。这串文本会进提示词。同时产生 "coroutine was never awaited" 的 RuntimeWarning——在 `pytest -W error` 下会让测试失败，在生产里只是一行日志。

**与计划冲突处。** spec 的 Requirement「prompt 组装只读当前租户画像」明确写了「该约束 SHALL 同时覆盖主对话、主动推送与 drift 三条组装路径」，还有一个 Scenario 叫「三条组装路径同源」。但 design 的 ADR-1 只解决提示块那条路径的同步异步错位（靠 `TurnContext` 携带快照），ADR-4 的六处改造点里 `plugins/drift_flow/factory.py:114` 只是把 `scope.memory` 换成按租户解析，没有回答「解析出来的是异步对象，同步调用方怎么办」。`plugins/proactive_flow/` 与 `plugins/wake_proactive/` 在 proposal 的影响清单和 tasks 里一次都没出现。

**护栏为什么逮不到。** 任务 2.2 的扫描测试在文件开头（`tests/memory/test_no_shared_profile_io.py:6-7`）写明「不禁 `read_long_term` 这类改名后仍然合法的调用」，只禁全局装配形态的符号。这个取舍本身有道理（避免立一条以后要开例外的宽规则），但代价就是这类调用方全在护栏之外。

**验证方法。** 一条用例：以 PG 后端装配主动推送路径，构建一次系统提示，断言其中不含 `coroutine object` 字样、且含该租户的长期画像内容。drift 与唤醒推送各一条。

**修法方向。** 三条路径要各自表态，不能默认。两个可选形状：一是让它们也消费 ADR-1 那个预取快照（要求它们能拿到本轮的 `ContextRequest`，drift 与主动推送是否走同一条组装入口需要先确认）；二是把这三处改成 `await`（`drift_flow/runtime.py:598` 所在的函数本身已经是 async，改造成本最低；`proactive_flow/prompt.py` 的 `build_system_prompt` 是否 async 需要确认）。选哪个都行，但要在 design 里写下来，并在 tasks 里立条目。

---

### F3 幂等键注册表跨租户共享，且 key 会超出列长

**事实（两条独立问题叠在同一个函数上）。** `agent/memory_pg.py:260-302` 的 `_append_once_with_index`：

- 去重键是 `f"consolidation_writes:{source_ref}:{kind}"`（`:268`）。
- 查重写法是 `sess.get(AppConfigModel, (dedup_key,))`（`:270`），按 key 单列查。
- `app_configs` 的主键就是 `key` 一列（`bootstrap/db/models/extras.py:20`），`tenant_id` 是 `TenantMixin` 带进来的普通列，不在主键里。
- `source_ref` 的实际内容是「本次整理窗口内所有消息 ID 的 JSON 数组」（`core/memory/markdown.py:191-200`），消息 ID 的形状是 `f"{session_key}:{seq}"`（`session/store.py:1014`）。
- journal 那条的 `kind` 是 `f"journal:{date_str}"`（`core/memory/markdown.py:256`）。

**后果一：key 超长。** `app_configs.key` 是 `String(128)`。前缀 `consolidation_writes:` 占 21 字符，后缀 `:journal:2026-10-11` 占 19 字符，中间是一个 JSON 数组，每个元素形如 `"chat:xxx:12"` 带引号和逗号。三条消息就已经把 128 用穿了。PostgreSQL 对超长写入 `varchar(128)` 会抛 `22001 value too long`。文件版今天不出事，是因为它把这个键写进 SQLite（`agent/memory.py:186` 那条路径），而 SQLite 不强制 VARCHAR 长度。这是一处「接口对齐但存储约束不对齐」的差异，只有在 PG 上才炸。

**后果二：注册表跨租户。** 就算 key 不超长，`sess.get` 按主键单列查，意味着租户 A 写过的 `(source_ref, kind)` 会让租户 B 的同键写入直接 `return False`，内容不落库、不报错。消息 ID 里含 session_key，跨租户撞键的概率不高，但这个注册表本身是进程级共享的，租户注销时也无人清理——`data-retention` 那条线要按租户删的话，这张表得单独考虑。

**与计划冲突处。** proposal.md:46 写「`app_configs` 承接合并幂等键」，没有提这两点。design.md:146 的 Risk 条目正确指出了 `content_hash` 不稳定，给出的缓解办法是「迁移与所有需要幂等的写入一律走 `_append_once_with_index`」，任务 7.2 把这条写成了硬性禁止（**禁止**用 `_append_entry`）。也就是说，计划把唯一的幂等手段押在了一个自身有两个缺陷的函数上。

**验证方法。** 两步。第一步：构造一个含 5 条消息的整理窗口，在 PG 上跑一次 journal 写入，看是否抛 22001。第二步：两个租户用同一个 `source_ref` 各写一次，断言两边都有行。

**修法方向。** 去重键要改成短且租户内唯一的形状，比如 `sha256(tenant_id + source_ref + kind)` 取前 32 位十六进制，并把 `tenant_id` 显式拼进被哈希的原文。更彻底的做法是给 `app_configs` 的主键加上 `tenant_id`（这需要一次 alembic 迁移，与 proposal 声称的「PG 侧 alembic 零迁移」冲突，要重新表态）。如果 F9 的修法把 `content_hash` 换成稳定哈希，那么唯一索引 `(tenant_id, content_hash, memory_type)` 本身就能承担去重，注册表可以退化成只服务 journal 那种「同源不同日」的场景，压力小很多。

---

### F4 五类画像进 `memory_items` 之后，关键词召回会看到它们

**事实。** `memory_items.memory_type` 是无约束的 `VARCHAR(64)`：`bootstrap/db/models/memory.py` 里没有 CheckConstraint，两份迁移里也没有 CHECK。召回侧只有时间检索那一条硬写了类型——`infra/storage/postgres_memory_store.py:889` 的 `list_events_by_time_range` 带 `memory_type='event'`。向量、BM25、LIKE 三条检索接受一个可选的类型过滤参数，而默认引擎在 `answer` 与 `context` 两种意图下传的是 `None`：`plugins/default_memory/engine.py:1440-1448` 的 `_resolve_memory_types` 只在 `filters.kinds` 有值或意图是 `procedure` 时才返回白名单。BM25 索引建在 `(id, summary)` 上，覆盖全表行，不区分类型（`infra/storage/postgres_memory_store.py:1537-1540`）。

**后果。** 画像层的 `history` 与 `journal` 是一条事件一行、量最大的两类。它们进了 `memory_items` 之后，对关键词召回可见。于是同一段内容可能被注入两次：一次经画像提示块，一次经召回结果。提示词预算被吃掉，召回质量基线漂移。

**与计划冲突处。** design ADR-8（design.md:88）写的是「`recall_memory` 的时间检索在 `infra/storage/postgres_memory_store.py:889` 硬写 `memory_type='event'`，journal 行取不到」，并据此推出「只做记录、不被记忆路径消费已经由类型白名单结构性保证」。这句话对时间检索成立，对关键词与向量检索不成立。而 proposal.md:24 的 Non-Goal 写的是「不改记忆召回与检索契约……不动 `memory_items` 的向量/BM25 检索面」——事实上写进去就会动到。

**需要说清的一点。** 这个暴露不是全新的：`memory_type='self'` 的行今天已经在表里（`bootstrap/db/repository/persona_repo.py:143-153`、`bootstrap/persona.py:96-131`），也一样对关键词召回可见。所以现状已有先例，本 change 是把暴露面从一个类型扩到六个类型、从少量行扩到按事件增长的行集。

**验证方法。** 在 PG 上插一条可识别的 journal 行，跑一次 `intent='answer'` 的召回，看它是否出现在结果里。再跑一次现有的召回评测（`eval/locomo`、`eval/longmemeval`），对比加画像行之前的分数。这需要先解决 F11。

**修法方向。** 三个选项，任选其一但要在 design 里写明：一是给召回的类型过滤加一个默认排除集（把六类画像排除掉），这动的是召回侧代码，与 Non-Goal 冲突，要把 Non-Goal 改掉；二是画像行不写 `summary` 而写另一个不参与 BM25 索引的列，代价是索引与查询都要改；三是画像层不共用 `memory_items`，另立一张表，代价是「PG 侧 alembic 零迁移」这条要推翻。第三个选项在架构上最干净，也最符合「一条实现路径」的取向。

---

### F5 静态扫描的覆盖面盖不住活的装配点

**事实。** 已落地的 `tests/memory/test_no_shared_profile_io.py:17-26` 扫描 7 个文件加 `plugins/drift_flow` 一个目录，禁用四条正则：`build_markdown_memory_runtime(`、`.markdown.(store|maintenance)`、`MemoryStore(`、`ctx.memory.read_self(`。

以下活路径不在扫描范围内：

| 位置 | 形态 | 为什么护栏看不见 |
| --- | --- | --- |
| `agent/looping/core.py:169,174` | AgentLoop 构造期缓存 `markdown_memory.store` 注入 ContextBuilder | 文件不在扫描列表；且写法是别名 `markdown_memory.store`，两条正则都匹配不上 |
| `agent/looping/core.py:243-247` | `getattr(self._markdown_memory, "maintenance", None)` | 字符串形式取用，正则无法匹配 |
| `agent/looping/core.py:936-945` | `self._markdown_memory.maintenance.consolidate(...)` | 由 dashboard 的 HTTP 端点 `POST /api/dashboard/sessions/{key}/consolidate`（`bootstrap/dashboard_api.py:1127-1147`）触发 |
| `bootstrap/dashboard_api.py:957` | `app.state.memory_store = memory_store or MemoryStore(workspace)` | 文件不在扫描列表 |
| `bootstrap/init_workspace.py:150` | `MemoryStore(workspace)` | 文件不在扫描列表 |
| `bootstrap/app.py:775` | 把整个 `self.memory_runtime` 门面传给 ProactiveLoop | 文件在扫描列表内，但传的是门面对象，没有 `.markdown.` 字面量，正则匹配不上 |
| `proactive_v2/memory_optimizer.py:265-355` | 长期画像与自我认知的唯一生产写入方 | 文件不在扫描列表 |
| `plugins/proactive_flow/prompt.py`、`plugins/wake_proactive/runtime.py` | 见 F2 | 文件不在扫描列表 |
| `infra/channels/qq_channel.py:692-694` | `_trace_actor_name` 直接从磁盘读 `workspace/memory/SELF.md`，不经任何 store | 文件不在扫描列表；ADR-9 把自我认知移交给 persona 之后，这个读方会永远读那份冻结的文件 |
| `agent/config.py:807-817` | 把 `~/.nexus/workspace/memory/<VAR_NAME>` 当配置占位符的密钥回退来读 | 文件不在扫描列表；它读的是共享 memory 目录下的任意文件名 |

**后果。** design.md:144 的 Risk 条目把「漏改某条旁路会把内容写回共享文件」的缓解手段押在这条静态扫描上。按现在的范围，上表任何一行漏改都不会被拦下，而故障表现正是 design 自己写的「记忆静默丢失」。

**与计划的关系。** 任务 2.2 的实测记录显示这条护栏已经逮到过一处 proposal 漏列的装配点（`tools.py:597`），说明它有效；问题只在范围。

**修法方向。** 把扫描倒过来做：扫全仓库 `*.py`，用一份显式的离线工具允许清单排除 `tests/`、`eval/`、`docker/debug/`、`scripts/`。允许清单里的每一项写清为什么可以豁免。同时补一条正则覆盖门面传递形态（例如禁 `memory_runtime` 整体作为实参传入 proactive/loop 构造），以及一条覆盖 `getattr(..., "maintenance")` 这类字符串取用。

---

### F6 提示词模板把共享文件路径交给了模型

**事实。** `prompts/agent.py:25` 把 `workspace_path` 绑成 `str(workspace.expanduser().resolve())`，`:38-41` 据此列出四个路径：`memory/MEMORY.md`、`memory/SELF.md`、`memory/HISTORY.md`、`memory/RECENT_CONTEXT.md`；`:152` 进一步指示模型「宏观时间线浏览：`read_file {workspace_path}/memory/HISTORY.md`」。`prompts/background.py:32-36,112-113` 对后台与子代理提示词做了同样的事，列了 `SELF.md` 与 `HISTORY.md`。

文件工具的根按模式解析：`agent/tools/filesystem.py:44-54` 的 `_effective_root` 在多租户模式下取 `resolver.file_root(tenant_id, fallback)`，也就是 `<workspace>/tenants/<目录名>/workspace`（`agent/tools/path_resolver.py:48-54`）；单机模式回退到构造期的 `allowed_dir`。越出根的绝对路径会被拒（`agent/tools/filesystem.py:40`）。

**后果分两种模式。**

- 多租户：提示词给的是共享根下的绝对路径，落在租户工作区之外，`read_file` 会拒绝。这条指示今天就已经是坏的，属于既有缺陷。
- 单机：根就是共享工作区，绝对路径在根内，`read_file` 会成功。而按 spec 的要求，这些文件迁移后保持原样、永不改写（「迁移后原文件不被改写」）。于是模型每次都会读到一份冻结的归档，并被提示词告知这是「历史日志，支持 grep 搜索」。这是静默的陈旧读。

**与计划冲突处。** spec 的 Requirement「记忆画像不再有共享文件后端」写的是「进程级共享 workspace 下的记忆目录 SHALL NOT 出现在运行时读写路径上」。提示词让模型用通用文件工具去读那个目录，就是一条运行时读取路径，只是不经画像层代码。proposal 的影响清单里没有 `prompts/`。

**验证方法。** 单机模式起服务，发一轮让模型去读 `HISTORY.md` 的对话，看它读到的内容是不是迁移前的旧文本。

**修法方向。** 两个提示词模板里的画像文件路径要一并摘掉。宏观时间线浏览那条指示改成走召回工具或显式的按类型查询接口，不要指向文件。这件事要跟 F4 一起定，因为两者都关系到「历史内容以后从哪儿读」。

---

### F7 存量单机安装升级时会丢掉已演化的自我认知

**事实。** 现在的播种路径在文件侧：`bootstrap/init_workspace.py:16-27` 的 `_EMPTY_FILES` 与 `_build_text_files` 会在文件不存在时创建四个空 markdown，并把 `memory/SELF.md` 写成 `get_default_self_md(persona)`——内容来自 `[agent.persona]`。这个函数在每次服务启动时都会跑（`main.py:110` 的 `serve()`）。

计划要加的播种路径在表侧：任务 6.2「workspace 初始化时为单机固定租户播种一行 `tenant_persona_profiles`，`self_model` 取 `[agent.persona]`，仅在行不存在时写入」。任务 7.1 明确写「`SELF.md` 不在画像迁移范围内，它的内容交给 6.2 的播种路径（已有播种行则不覆盖），脚本只报告不搬」。

**后果。** 一台已经在跑的单机实例，它的 `SELF.md` 早就被记忆整理器改写过（`proactive_v2/memory_optimizer.py:354` 是唯一生产写入方）。升级之后：6.2 发现表里没有行，于是从 config 装一份**默认**自我认知进去；磁盘上那份演化过的内容，7.1 只打印报告，不导入。用户积累的自我认知就这么没了，而且没有任何报错。

按 ADR-10 的论证，单机路径正是为了不逼 Telegram 部署装数据库才保留的，也就是说这条路径对应真实在跑的安装。

**与计划冲突处。** persona-relationship 的 delta spec 写「`[agent.persona]` SHALL 只承担单机固定租户的初始 seed 角色」。对全新安装这句话成立；对存量安装，「初始 seed」的输入应该是磁盘上那份演化过的当前值，不是 config 里的出厂值。

**验证方法。** 准备一个 `SELF.md` 已被改写过的 workspace，跑一次升级流程，断言表里那行的 `self_model` 等于文件内容而不是 config 默认值。

**修法方向。** 播种的输入改成「文件存在且非默认则取文件，否则取 config」，并且播种之后把文件重命名成 `.migrated` 之类的形态留档，避免 F6 那条陈旧读。任务 7.1 从「只报告」改成「导入并留档」。同时 `_EMPTY_FILES` 里的四个画像文件应当停止创建——它们是画像层退出运行时路径的一部分，任务 6.7 只提到 `bootstrap/memory.py:170` 与 `:246`，漏了 `init_workspace.py`。

---

### F8 同租户并发写会产生重复的单行画像

**事实。** `agent/memory_pg.py:206-238` 的 `_upsert_content` 是先 `SELECT ... LIMIT 1` 再决定 UPDATE 还是 INSERT。表上唯一的约束是 `ix_memory_items_content_hash`，覆盖 `(tenant_id, content_hash, memory_type)`（`alembic/versions/a3d5c7e9f1b2_partition_memory_items.py:118-121`），而 `content_hash` 写的是 `str(hash(content))`（`agent/memory_pg.py:232`）——按进程随机化，见 F9。`(tenant_id, memory_type)` 上没有唯一约束。读取侧 `_read_content`（`:186-204`）用 `ORDER BY updated_at DESC LIMIT 1`。

**后果。** 同一租户的两个 turn 交错写长期画像时，两边都可能 SELECT 到空、各自 INSERT 一行。之后读取只取最新那条，旧行留在表里成为孤儿——而且因为 F4，孤儿行对关键词召回仍然可见。

**与计划冲突处。** spec 有 Scenario「并发双租户写不互串」，覆盖的是两个租户之间。没有同租户两个 turn 之间的并发场景。design 的 Risks 也没提。

**验证方法。** 一条用例：同一租户并发发起两次 `write_long_term`，断言表里 `memory_type='long_term'` 只有一行。

**修法方向。** 把 F9 的哈希改成稳定的之后，`(tenant_id, content_hash, memory_type)` 的唯一索引仍然挡不住「同类型不同内容」的并发插入。要么给单行类型加一个 `(tenant_id, memory_type)` 的部分唯一索引（`WHERE memory_type IN ('long_term','recent_context','self')`，需要一次迁移），要么把 `_upsert_content` 改成 `INSERT ... ON CONFLICT DO UPDATE` 并配一个能承接冲突的唯一约束。

---

### F9 `str(hash())` 的点位比 design 记的多两处

**事实。** design.md:146 记的是 `agent/memory_pg.py:252` 与 `bootstrap/persona.py:113/130`。实际用内置 `hash()` 算 `content_hash` 的点位有六处：

- `agent/memory_pg.py:232`（`_upsert_content`）、`:252`（`_append_entry`）、`:295`（`_append_once_with_index`）
- `bootstrap/persona.py:113`、`:131`
- `bootstrap/db/repository/persona_repo.py:153`（onboarding 播种 `memory_type='self'` 那条）

`PYTHONHASHSEED` 在 Dockerfile、docker-compose.yml、`.github/workflows/`、pytest 配置、scripts 里都没有固定，全仓库只在 openspec 文档和 `plugins/rachael/core.py:1398` 的一行注释里出现过。

**已有的正确做法。** `memory2/store.py:153-155` 的 `_content_hash()` 用 `sha256` 取前 16 位，SQLite 侧的 `upsert_item`（`:478`）与 PG 侧的 `infra/storage/postgres_memory_store.py:316,413` 都在用它。也就是说，仓库里已经有一个跨后端共用的稳定哈希实现，画像层这六处没有用。

**后果。** 唯一索引跨进程去不了重，迁移重放要靠 F3 那个自身有缺陷的注册表；同时 F8 的并发重复行也失去了最后一道兜底。

**修法方向。** 六处一律改用 `memory2/store.py` 的 `_content_hash`。这是一次收敛，不是新增机制：改完之后两种后端、召回面与画像层用同一个哈希定义。

---

### F10 spec Scenario 与 tasks 的可追溯性缺口

delta spec 一共 15 个 Scenario（tenant-memory-profile 11 个，persona-relationship 4 个）。逐个对到 tasks：

| Scenario | 实现任务 | 状态 |
| --- | --- | --- |
| 并发双租户写不互串 | — | **无** |
| 新租户画像从空开始 | — | **无**（2.3 覆盖的是「开场白不引用他人话题」，不是「五类均为空」） |
| 新租户开场白不引用他人话题 | 2.3 | 已落地，绿 |
| 三条组装路径同源 | — | **无**（F2 正好落在这里） |
| 提示块缓存不跨租户命中 | 3.6 | 已落地，绿 |
| 预热渲染不得污染调试视图 | 3.7 | 未实现 |
| 画像缺失不回退共享位置 | 2.3 | 已落地，绿 |
| 运行时路径无共享目录读写 | 2.2 | 已落地，范围不全（F5） |
| 单机模式写入落表不落文件 | — | **无**（6.4 只测 SQLite 实现的读写接口，不断言文件未被改动） |
| 两种装配共用同一读接口 | 6.4 部分 | 弱覆盖（没有跨后端同接口的断言） |
| 双租户各自整理 | — | **无**（4.3 覆盖的是缺失归属时 fail-closed） |
| 缺失租户归属时跳过 | 4.3 | 未实现 |
| 重放不产生重复条目 | 7.2 | 未实现 |
| 未指定归属时拒绝 | 7.1 | 未实现 |
| 迁移后原文件不被改写 | 7.3 | 未实现 |
| persona：重启恢复 | — | **无** |
| persona：单机 dev 重启恢复 | — | **无**（6.2 与 6.5 都不含重启） |
| persona：config 不承载 tenant 当前值 | 6.2 | 未实现 |
| persona：两种后端写入留同一形态的审计 | 6.3 | 未实现 |

**后果。** `openspec validate --strict` 检查的是产物结构与措辞，不检查 Scenario 是否有对应测试。这份 change 可以在五个 SHALL 场景从未被执行过的情况下归档。F1 与 F2 恰好都落在没有任务覆盖的场景里——这不是巧合：没有测试的地方，正是缺陷能藏住的地方。

**修法方向。** 给上表标「无」的 5 条各立一个任务，并在任务描述里写明它落实哪个 Scenario。归档前跑一遍这张表。

---

### F11 eval 与 docker/debug 工具会跟着失效，计划未表态

**事实。** 以下离线工具直接依赖文件画像层：

- `eval/longmemeval/runtime.py:104-118`：调 `init_workspace`、覆写 `SELF.md`、把 `markdown.maintenance` 包进 `_ConsolidationAdapter`。
- `eval/locomo/run.py:191-212,278,336`、`eval/personamem/run_one_case.py:74,91`、`eval/personamem/run.py:224,240`：读 `SELF.md` 进 trace 日志。
- `docker/debug/run_optimizer_once.py:37-55`、`run_consolidation_then_optimizer.py:119-150`、`context_probe.py:43`、`runtime_race_probe.py:117-126`。

**后果。** 文件后端摘除之后这些工具会坏。坏本身可以接受（它们是离线工具），但要紧的是 F4 需要靠召回评测来验证，而评测工具正好在这一批里。计划既没有把它们列进影响清单，也没有在 Non-Goals 里说明「本 change 不负责修 eval」。

**修法方向。** 在 proposal 的 Impact 里加一段离线工具清单，逐个表态：修、暂留坏、还是删。至少要保住 F4 验证需要的那一条评测路径。

---

### F12 测试断言面被低估

proposal.md:48 写「至少 9 处」。全仓库扫描另找到以下会受影响的用例：`tests/test_turn_pipelines.py:80,83,89`、`tests/test_support_modules.py:716,897`（任务 3.2 只提到 `:753` 一处）、`tests/test_agent_self_check.py:27,94-162`、`tests/test_bootstrap_wiring_p2.py:526-572`、`tests/admission/test_priority.py:15,60,70`、`tests/c8/test_lease_coverage.py:137,298,322`、`tests/test_dashboard_undo_readiness.py:142`。合计 15 处以上。

**后果。** 进度估算偏低，不影响正确性。任务 9.1 的验证方式（跑全量 pytest）会兜住，不会漏改到发布。

---

### F13 spec 判「单机模式」的条件与仓库其余隔离机制不一致

**事实。** spec 的定义句是「单机模式（存储后端为 SQLite **或**认证关闭）SHALL 走同一套按租户解析的接口，租户为固定的那个」——用的是「或」。

仓库里其余的租户隔离机制用的是「且」：`bootstrap/tools.py:457-463` 与 `bootstrap/audit.py:78` 都把 `multi_tenant` 算成 `auth.enabled ∧ backend == "postgres"`，`agent/tools/path_resolver.py:4-6` 的模块注释也是这么写的。

启动期没有任何一处拒绝「认证开启但后端是 SQLite」这个组合。`AuthConfig.enabled` 是个默认 False 的普通布尔字段（`agent/config_models.py:198-207`），不带校验。仓库里唯一形状相似的 fail-fast 是 `bootstrap/telegram_durable.py:53-70`，而它只管 `[channels.telegram].pilot_identity_binding` 这一个开关，在 `bootstrap/app.py:457-459` 被调用。

`create_auth_runtime` 的引擎来自 `config.storage.postgres_url`（`bootstrap/auth/runtime.py:123-133`），与 `config.storage.backend` 无关。所以只要 `postgres_url` 填了、`backend` 留在 `sqlite`，这个组合能启动。

**后果。** 在这种配置下 `multi_tenant` 算出来是 False，按 ADR-9 与 ADR-10 画像层会把所有租户归到 `DEFAULT_TENANT`。而认证是开着的，登录进来的是彼此不同的真实租户。同时 SQLite 侧的 `memory_items` 根本没有 `tenant_id` 列（`memory2/store.py:68-83`），任务 6.1 也只补 persona 两张表、不补租户列。于是所有认证租户共用同一份画像——本 change 要闭合的泄漏原样保留，而且在这个后端上补不了，除非先加租户列。

**与计划的关系。** design.md:147 提到了 SQLite 没有租户列这件事，但只当作测试限制来写（「隔离断言不能在单机上验」），没有当作配置组合的风险。

**似然为什么标低。** 这个组合需要一份自相矛盾的配置：认证开着、`postgres_url` 填了、`backend` 却留在 `sqlite`。正常部署不会这么配。但它能启动、不报错、故障表现是静默泄漏，这三条加起来值得处理。

**验证方法。** 起一次 `auth.enabled=true` + `storage.backend="sqlite"` + 有效 `postgres_url` 的实例，建两个账号，看两边读到的长期画像是不是同一份。

**修法方向。** 两件事，都很小。一是把 spec 那句「或」改成与仓库其余部分一致的「且」，或者反过来显式写明这两种判据不同以及为什么。二是加一条启动期校验：`auth.enabled=true` 时要求 `storage.backend="postgres"`，不满足则 fail-fast，报错文本可以照抄 `telegram_pilot_prereq_error` 的形状。

---

## 五、计划里站得住的部分

评审只报缺陷会失真，以下几条是实测核对后确认站得住的：

- **ADR-1 的选型。** 预取不可变快照而不是把提示路径改 async，三个被否掉的备选（整体改 async、加同步门面、另立解析器）各自的技术理由都成立。「同步门面会自锁」这条尤其准确：AgentLoop 已在事件循环内，`asyncio.run` 不可用，独立线程加独立 loop 会让 SQLAlchemy 异步 engine 跨事件循环。同一次数据库会话内取回两份快照、避免两个时刻拼贴的论证也对。
- **ADR-3 拒绝 contextvar。** 理由引的是 `tenant-tool-isolation` 要收敛的模式，与本仓库既有方向一致。显式传参可静态检查，这个取舍正确。
- **ADR-7 不给测试台开指定租户 ID 的口子。** `_new_tenant_id()`（`bootstrap/db/repository/provisioning_repo.py:386-388`）固定产出 `pilot-<12 位随机>`，加覆盖入口等于在 1:1 身份链旁边开一条人工命名路径。用 `display_name` 承 label、在测试台清单里记映射，是把代价放在正确的地方。
- **ADR-11 已落地且做对了。** 把租户并进缓存 scope、删掉 `IdentityPromptBlock` 里手写的 fail-closed，是把防御从「逐块自觉」变成「结构上不可能」。任务 3.6 的实测记录显示三条用例都绿，包含「同租户仍命中」和「无租户不进缓存」两条反向保护，说明没有把缓存做废。
- **ADR-12 把可选字段改必填。** 这是把静默失效转成类型错误的正确做法。计划还准确指出了预热渲染覆盖 `_last_debug_breakdown`（`agent/context.py:319`）这个副作用。
- **design.md:146 已经识别出 `str(hash())` 不稳定。** 这是一个非常不明显的缺陷，能主动写进 Risks 说明取证是认真的。F9 只是补全点位。
- **任务 2.2 的实测记录逮到了 proposal 漏列的 `tools.py:597`。** 先写红测试再实现的做法已经在产生回报。

---

## 六、未证实的假设与需要实测的点

按「猜测标注并当场撤回」的要求，以下几条我没有实测，只给判据：

1. **F3 的 key 超长触发窗口。** 机制确认（`varchar(128)` 主键 vs 无长度上限的 JSON 数组），但具体几条消息触发取决于 `session_key` 的实际长度。判据：在 PG 上插一条 key 长度为 129 的 `app_configs` 行，看是否抛 22001。
2. **F2 的 drift 与主动推送是否能复用 ADR-1 的快照。** 我没有确认这两条路径是否经 `ContextBuilder` 组装。`plugins/proactive_flow/prompt.py` 有自己的 `build_system_prompt`，看起来是独立组装；判据是查它的调用方是否持有 `ContextRequest`。
3. **`bootstrap/app.py:649` 的门禁放宽是否牵连其他装配。** `if self.webchat_durable is not None:` 这个条件同时守着 persona resolver 与关系状态写入接口。ADR-9 要求放宽到两种后端都装。我没有核对 `webchat_durable` 为 None 时下游还有多少处依赖它非空。判据：把条件改掉后跑全量 pyright。
4. **F4 的召回影响量级。** 需要跑评测才能给数字，而评测工具本身受 F11 影响。

另外有一条**低风险但值得记下的观察**：`bootstrap/app.py:989` 的 `AttachmentBlobStore(Path(self.workspace), multi_tenant=True)` 把 `multi_tenant` 硬写成 True，而 `bootstrap/tools.py:459` 与 `:730` 是按 `auth.enabled ∧ backend=postgres` 算出来的。ADR-5 要动 blob_store，顺手核对一下这处不一致。

多租户模式的判据核对过一处、撤回一处。核对无误的部分：`bootstrap/tools.py:457-463` 与 `bootstrap/audit.py:78` 的写法与 proposal.md:3 的定义一致，都是 `auth.enabled ∧ backend == "postgres"`。**撤回**：我先前根据 `agent/config_models.py:38` 的一行注释，判断「认证开启但后端是 SQLite」这个组合会在启动期被拒。读了定义之后发现那行注释说的是 `[channels.telegram].pilot_identity_binding` 这一个开关的前置条件，不是全局校验；仓库里没有针对 `auth.enabled` 与 `storage.backend` 的启动期校验。所以这个组合是可达的，后果已立为 F13。

---

## 七、建议的下一步

按依赖顺序，详细动作与验收判据见同目录 `remediation-plan.md`，关系图见 `risk-map.dot`。

先定三件事，再往下写代码：

1. **F1 与 F3 决定 `PgMemoryMarkdownStore` 能不能当正式实现用。** 如果补两个缺陷的成本高于重写，就重写；不要因为「已经写好了」而接上去。
2. **F4 决定画像层住哪张表。** 这条会影响 F8 的约束设计和 F11 的评测需求，越早定越好。
3. **F2 决定三条旁路各自的形状。** 定完之后 tasks 才知道要加几条。

以上三条都会改动 design.md 的结论，建议走一次 `openspec-update-change` 把 ADR 补齐，再动代码。
