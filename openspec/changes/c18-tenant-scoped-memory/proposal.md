## Why

多租户模式（`auth.enabled` ∧ `storage.backend = "postgres"`）下，记忆画像层——`MEMORY.md` / `SELF.md` / `PENDING.md` / `HISTORY.md` / `RECENT_CONTEXT.md` / `journal/`——仍然是**进程级共享的全局文件**，而 prompt 组装直接读它。于是一个刚完成 onboarding 的租户会在自己的系统提示里读到其他身份（含 owner）的长期画像与近期摘要，并把本租户的对话轮次写回同一份文件。

2026-10-11 在本地 PG+auth 实例上实机复现：新建租户 `pilot-ozkl1p66s4xk` 的第一轮开场白输出「上次我们停在《全金属外壳》那儿」，其思考块原文为 `Recent context mentions ongoing threads about war movies (Full Metal Jacket)`，来源是全局 `RECENT_CONTEXT.md` 中标注 `until 2026-07-18` 的压缩摘要；随后追问「我是做什么研究的」，该租户直接背出全局 `MEMORY.md` 的原文，思考块写明 `The injected "Long-term Memory" section says: ## 用户事实...`。该租户自己的 workspace 目录当时为空。反向污染同时成立：它的对话轮次已被写回全局文件的 `## Recent Turns` 段。

这是 C1 身份隔离与 C7 工具隔离已建立的租户边界，在记忆侧尚未闭合的那一块；也是 `pg-durable-sot-cutover` 归档时写明「prompt 组装直读 canonical 归后续 change」而一直没被认领的那一步——本 change 认领它。

## What Changes

- 多租户模式下，记忆画像层以 PostgreSQL 为唯一读写面，落在**新建的两张画像专表**上（`tenant_memory_profile_sections` 存单行整块类、`tenant_memory_profile_entries` 存追加日志类），与记忆召回分表：画像行不出现在任何召回 SQL 里（design ADR-13）。
- **BREAKING** 画像层不再有文件后端。`<workspace>/memory/` 下的 markdown 文件与 `journal/` 目录退出运行时读写路径，只作为迁移输入与归档留存；单机模式（SQLite 后端或认证关闭）不再走文件，而是按固定租户走同一套表存储接口。SQLite 侧要新建同样那两张画像表——`memory2/store.py` 现有的 `memory_items` 没有 `tenant_id` 列，本 change 不动它。
- **BREAKING** 画像存储接口按新表重设。`agent/memory_pg.py` 的 `PgMemoryMarkdownStore` 不满足 `core/memory/markdown.py:61-80` 的 `MemoryProfileApi`（协议 10 个方法它实现 6 个），且它的三个 pending 方法是空实现；接口按两张表的行形状重新定义为约 9 个方法，文件时代的方法（`backup_long_term` / `get_memory_context` / `has_long_term_memory` / `read_recent_history` / `snapshot_pending` 一族）删除而不是在新表上重新实现（design ADR-14）。
- prompt 组装的两个画像块（`agent/core/prompt_block.py` 的 memory 与 recent_context）只读当前租户；`TurnContext` 补 `tenant_id` 字段。
- 三条可加载组装路径共用一个段渲染器。`ContextBuilder._build_system_prompt_result` 提为公开的 `render_sections`，插件侧只依赖一个窄协议；主动推送与 drift 两条旁路删掉各自的画像读取，改为在已有的 async 入口预取快照后调它。连带删除 `ContextBuilder` 的 `memory` 构造参数——`ctx.memory` 在全部九个块里只被读一次，而那处随 ADR-9 消失，`ContextBuilder` 从此不做 I/O（design ADR-15）。
- 自我认知那一格的**访问路径**归 persona 那条缝、**当前值**存进画像专表的一行（`category='relationship_state'`），两种后端走同一套语义：prompt 块只读 persona 快照（删掉按模式二选一的分支），optimizer 只经关系状态写缝写入（PG 与 SQLite 各一实现，都同事务写审计）。为此 SQLite 侧要补 `tenant_persona_profiles` 与 `persona_audit_events` 两张表，单机固定租户的行在初始化时从 `[agent.persona]` 播种；PG 侧的播种点是 onboarding 提交（`bootstrap/db/repository/persona_repo.py`），目标表随之从 `memory_items` 换成画像专表。
- 4 个启动期单例引用根改为按租户路由：`bootstrap/app.py:666`（memory optimizer）、`:738`（dashboard）、`:775`（proactive，其 `scope.memory` 被 `plugins/drift_flow/factory.py:114` 与 `plugins/wake_proactive/runtime.py:127` 两处下游消费）、`bootstrap/tools.py:597`（maintenance 绑会话生命周期）。原计划记的「六处」把 `app.py:775` 与 `drift_flow/factory.py:114` 当成两个根（实为同一对象同一链）、漏列了 wake 那一处，并且 `bootstrap/tools.py:577-579` 那处随 `ContextBuilder` 删 `memory` 参数而消失（design ADR-4）。
- 一次性迁移：把现存全局 markdown 内容写入指定 owner 租户的画像行，幂等可重放；`SELF.md` 的内容迁入 `relationship_state` 行并覆盖播种值（播种值来自 config 默认模板，文件里才是演化后的当前值）。迁移完成后全局文件只作为迁移输入与归档留存，不再是事实源。
- 本地验收工具（不是产品能力）：两个常驻测试租户 `admin1` / `admin2`、dev-only 的租户切换入口、一条跨租户隔离断言。

- 提示块静态缓存的 scope 加租户维度（现在只有 workspace 路径），并删掉 C9 为此在身份块里手写的一次 fail-closed。
- `ContextRequest` 的 `tenant_id` 与画像快照改为必填，None 不再是合法输入；`before_reasoning` 的预热渲染因此必须带租户身份。

### Non-Goals

- 不改记忆召回与检索契约（`memory-retrieval-baseline`、`memory-engine-catalog` 范围内）。本 change 只动画像层；画像另立专表之后，`memory_items` 的向量 / BM25 / LIKE 三条检索面一行代码都不动。
- 不改 PersonaProfile 的快照与固定语义，也不改 RelationshipState 的单写者与审计语义；改的只是 RelationshipState 当前值的存放位置。
- 不把会话内容改成 canonical 流直读；turn 内部仍消费 session view。
- 不删除磁盘上的历史 markdown 文件，也不提供租户化之后的 markdown 双写兼容层。
- 不引入生产可用的租户切换能力，切换入口限本地 dev-only 并说明为何不得进生产。
- 不统一「同名段放系统提示还是放上下文帧」这处路径间差异：主链把 `long_term_memory` / `self_model` 放系统提示，主动推送把同名的段放上下文帧并附带「禁止在回复中引用」的约束。改放置会改模型行为，需要跑评测判断，另开 change。
- 不修主动推送与 drift 两条路径注入进程级全局身份常量的问题（`plugins/proactive_flow/prompt.py:43-44`、`plugins/drift_flow/runtime.py:838-839` 插的是 `agent/persona.py:8/14` 的 `NEXUS_IDENTITY` / `PERSONALITY_RULES`，而非该租户的 PersonaProfile）。本 change 关掉的是画像泄漏；身份租户化另开 change。
- 不清理 `memory_items` 里遗留的 `memory_type='self'` 旧行。它们在 ADR-9 之后不再被读写，但仍对召回可见，清理动作另开 change。
- 不修唤醒插件不可加载的缺陷，也不因此把它的组装路径纳入本 change 的可验证范围。`plugins/wake_proactive/runtime.py:48` 无条件导入 `session/embedding_store`，而该模块在本仓库任何分支上都不存在（自 `168fed4a3` 起就是坏的），插件加载器把由此产生的 `ImportError` 吞成一条 warning 与 failed gate（`agent/plugins/manager.py:1598-1607`），所以 wake-proactive 从未加载过。补齐它等于在一个修隔离的 change 里激活一条从未运行过的推送路径，另开 change。本 change 只保留 `tests/memory/test_no_shared_profile_io.py` 对 `plugins/wake_proactive/` 的静态扫描覆盖，作为该插件恢复加载能力时的看守——它一旦能跑，就必须接入同一个段渲染器，否则第四条会带着泄漏上线。

## Capabilities

### New Capabilities

- `tenant-memory-profile`: 记忆画像层五类内容（`long_term` / `pending` / `recent_context` / `history` / `journal`）的租户归属规则、与记忆召回分表的存放边界、按后端的唯一事实源、三条可加载组装路径经同一个段渲染器只读本租户、从全局 markdown 迁移的幂等要求，以及单机模式按固定租户走同一套接口的边界。自我认知那一格的归属规则不在本 capability 内，归 `persona-relationship`；但它的当前值与五类画像同住一张专表。

### Modified Capabilities

- `persona-relationship`: 「PG 当前值存储、恢复与配置边界」这条 Requirement 现写明 PersonaProfile / RelationshipState 以 PostgreSQL 为规范 source of truth，且「单体 / SQLite dev 模式 SHALL 继续沿用现有文件语义（行为不变）」。本 change 要把它改成：两种后端各用所属库内的同名表、走同一套读写与审计语义，文件语义退出；`[agent.persona]` 从"单体兼容默认"收窄为"单机固定租户的初始 seed"。RelationshipState 当前值的存放位置也从 `memory_items` 的 `memory_type='self'` 行改到画像专表的 `relationship_state` 行——单写者与同事务审计的语义不变，变的只是那张表。

判断依据：`canonical-identity` 的「旧单体数据隔离」只约束旧单体 **SQLite** 的导入与回退路径，文本不因本 change 改变，不建 delta。`persona-relationship` 的「跨 tenant 不串」也不变，变的是它的存储边界条款（上一条）。记忆画像层本身在现有 19 份主 spec 中零命中（`RECENT_CONTEXT` / `markdown` 全库检索无结果），属新增契约。

## Impact

**代码**：`agent/memory_pg.py`、`agent/memory.py`、`core/memory/markdown.py`（含 `MemoryProfileApi` 协议与 `MarkdownMemoryStore` 子类）、`core/memory/runtime.py`、`agent/context.py`、`agent/core/prompt_block.py`（含 `SectionCache` 的 scope、身份块的手工 fail-closed、`SelfModelPromptBlock` 的回退分支）、`agent/prompting/assembler.py`、`agent/looping/core.py`、`agent/lifecycle/types.py`、`agent/lifecycle/phases/before_reasoning.py`、`agent/lifecycle/phases/prompt_render.py`、`bootstrap/memory.py`、`bootstrap/persona.py`、`bootstrap/tools.py`、`bootstrap/app.py`、`bootstrap/proactive.py`、`bootstrap/wiring.py`（`ContextFactory` 签名）、`bootstrap/init_workspace.py`（单机播种）、`bootstrap/db/repository/persona_repo.py`（onboarding 播种目标表）、`proactive_v2/memory_optimizer.py`（pending 两阶段塌缩）、`proactive_v2/runtime_scope.py` 与 `proactive_v2/loop.py`（渲染器字段）、`plugins/proactive_flow/prompt.py`、`plugins/default_proactive/runtime.py`、`plugins/drift_flow/*`；`agent/tools/path_resolver.py` 需把租户 category 根解析抽成公共 helper（`bootstrap/attachments/blob_store.py:71-85` 目前复制了同款逻辑）。另需一个 SQLite 侧的画像存储实现，与 PG 实现同接口。`plugins/wake_proactive/*` **不改**——该插件今天不可加载，理由与归属见 Non-Goals。

**数据**：**PG 侧新增两张画像表，需要一个 alembic 版本**（原计划写的「PG 侧无新表、alembic 零迁移」已随 ADR-13 作废），两张表都带 `tenant_id` 列。SQLite 侧 `memory2/store.py` 的建表语句要加四张：画像两张 + `tenant_persona_profiles` + `persona_audit_events`。同一份结构从此有两个出处（PG 走 alembic、SQLite 走建表语句），需一个跨后端一致性测试兜住列名集合、NOT NULL 集合与 CHECK 取值。`memory_items` 与 `app_configs` 都不再承接画像写入——`app_configs` 原本要承接合并幂等键，改用新表自己的 `dedup_key` 列（那张表的主键是 `key` 单列 `String(128)`、`tenant_id` 非主键，既有长度上限又跨租户全局，见 ADR-13）。全仓库 6 处 `str(hash(正文))` 随之全部删除。

**测试**：断言 `workspace/memory/` 路径或 markdown 单例语义的用例，加上因 `ContextBuilder` 构造签名变化、两条旁路改用共用渲染器、以及 6.3 删除协议方法而受影响的用例。**实测口径**：`grep` 统计引用了被删方法（`read_self` / `write_self` / `snapshot_pending` / `commit_pending_snapshot` / `rollback_pending_snapshot` / `clear_pending` / `append_pending` / `append_history` / `backup_long_term` / `get_memory_context` / `has_long_term_memory` / `read_recent_history`）的测试文件共 **14 个**，按引用数排序：`tests/memory_fakes.py`（23，被删方法的主要替身承载点，先改它让其余收敛）、`tests/test_memory_store_questions.py`（19）、`tests/test_consolidation_service.py`（17）、`tests/test_logic_modules.py`（9）、`tests/test_memory_optimizer.py`（8）、`tests/test_lifecycle_phases.py`（6）、`tests/test_support_modules.py`（4，含 `:753` 那条已知的红）、`tests/test_turn_pipelines.py`（3）、`tests/test_internal_modules.py`（2）、`tests/test_agent_core_p4_prompt_block.py`（1）、`tests/persona/test_prompt_blocks.py`（1）、`tests/memory/test_profile_snapshot_isolation.py`（1）、`tests/memory/test_no_shared_profile_io.py` 与 `tests/memory/test_no_coroutine_in_prompt.py`（本轮新增的两条靶）。另需同步的还有 `tests/test_runtime_smoke.py:312`、`tests/test_channel_clients.py:1084`、`tests/test_memory_engine_contract.py:366/832/932`、`tests/c8/test_lease_coverage.py`（断言 workspace/memory 路径那类），以及三个 debug 脚本：`docker/debug/runtime_race_probe.py:117-126`（实现了三个将被删除的协议方法）、`docker/debug/run_optimizer_once.py:55` 与 `docker/debug/run_consolidation_then_optimizer.py:150`（各调一次 `store.read_self()`）。原计划记的「至少 9 处」低估了大约一倍，且完全没列后两个脚本——这是同一个「测试面被低估」判断的第二轮复现，所以本段改成给可复跑的 grep 口径，而不是给一个会过期的数字。

**最大结构性风险**：同步调用方拿到未 await 的协程对象时不报错——协程是真值，`str(x() or "")` 会把 `<coroutine object ...>` 送进提示。旁路现在正是这个形状（`plugins/proactive_flow/prompt.py:248-257`、`plugins/drift_flow/runtime.py:597-602`，以及今天不可加载的 `plugins/wake_proactive/runtime.py:1024-1026`），`proactive_v2/memory_optimizer.py:261-298` 也是。**已实测到实物**：真调 `build_runtime_context_message` 得到的帧正文里，`## self_model` / `## long_term_memory` / `## recent_context` 三段全是 `<coroutine object ...>`；输出存 `openspec/evidence/c18-tenant-scoped-memory/guardrail-scan-hits.txt`。ADR-1 的预取快照解决主链，ADR-15 的共用段渲染器解决旁路：插件侧不再存在对存储的读取调用，协程无从产生。`core/memory/markdown.py:61-80` 的协议与 `PgMemoryMarkdownStore` 的方法集不匹配（10 个里实现 6 个）是同一个根因的另一面——它从来没被实例化过，所以从未被检查过。
