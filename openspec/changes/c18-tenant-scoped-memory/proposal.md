## Why

多租户模式（`auth.enabled` ∧ `storage.backend = "postgres"`）下，记忆画像层——`MEMORY.md` / `SELF.md` / `PENDING.md` / `HISTORY.md` / `RECENT_CONTEXT.md` / `journal/`——仍然是**进程级共享的全局文件**，而 prompt 组装直接读它。于是一个刚完成 onboarding 的租户会在自己的系统提示里读到其他身份（含 owner）的长期画像与近期摘要，并把本租户的对话轮次写回同一份文件。

2026-10-11 在本地 PG+auth 实例上实机复现：新建租户 `pilot-ozkl1p66s4xk` 的第一轮开场白输出「上次我们停在《全金属外壳》那儿」，其思考块原文为 `Recent context mentions ongoing threads about war movies (Full Metal Jacket)`，来源是全局 `RECENT_CONTEXT.md` 中标注 `until 2026-07-18` 的压缩摘要；随后追问「我是做什么研究的」，该租户直接背出全局 `MEMORY.md` 的原文，思考块写明 `The injected "Long-term Memory" section says: ## 用户事实...`。该租户自己的 workspace 目录当时为空。反向污染同时成立：它的对话轮次已被写回全局文件的 `## Recent Turns` 段。

这是 C1 身份隔离与 C7 工具隔离已建立的租户边界，在记忆侧尚未闭合的那一块；也是 `pg-durable-sot-cutover` 归档时写明「prompt 组装直读 canonical 归后续 change」而一直没被认领的那一步——本 change 认领它。

## What Changes

- 多租户模式下，记忆画像层以 PostgreSQL 为唯一读写面。`agent/memory_pg.py` 的 `PgMemoryMarkdownStore`（构造时已收 `tenant_id`）从「已写好但全仓库无任何实例化」变为该层的正式实现。
- **BREAKING** 画像层不再有文件后端。`<workspace>/memory/` 下的 markdown 文件与 `journal/` 目录退出运行时读写路径，只作为迁移输入与归档留存；单机模式（SQLite 后端或认证关闭）不再走文件，而是按固定租户走同一套表存储接口。SQLite 侧的 `memory_items` 表已有 `memory_type` / `happened_at` / `source_ref` / `extra_json` 且与 PG 同形（`memory2/store.py:69-82`），缺的只是这份实现，不是表。
- prompt 组装的两个画像块（`agent/core/prompt_block.py` 的 memory 与 recent_context）只读当前租户；`TurnContext` 补 `tenant_id` 字段。
- 自我认知那一格整体移出画像层，读写都归 persona 那条缝，两种后端走同一套语义：prompt 块只读 persona 快照（删掉按模式二选一的分支），optimizer 只经关系状态写缝写入（PG 与 SQLite 各一实现，都同事务写审计）。为此 SQLite 侧要补 `tenant_persona_profiles` 与 `persona_audit_events` 两张表，单机固定租户的行在初始化时从 `[agent.persona]` 播种。
- 六处启动期单例引用改为按租户路由：`bootstrap/app.py:666`（memory optimizer）、`:738`（dashboard）、`:775`（proactive loop）、`bootstrap/tools.py:579`（ContextBuilder 注入）、`:597`（maintenance 绑会话生命周期）、`plugins/drift_flow/factory.py:114`（drift 经 `scope.memory`）。
- 一次性迁移：把现存全局 markdown 内容写入指定 owner 租户的 PG 行，幂等可重放；迁移完成后全局文件只作为迁移输入与归档留存，不再是事实源。
- 本地验收工具（不是产品能力）：两个常驻测试租户 `admin1` / `admin2`、dev-only 的租户切换入口、一条跨租户隔离断言。

- 提示块静态缓存的 scope 加租户维度（现在只有 workspace 路径），并删掉 C9 为此在身份块里手写的一次 fail-closed。
- `ContextRequest` 的 `tenant_id` 与画像快照改为必填，None 不再是合法输入；`before_reasoning` 的预热渲染因此必须带租户身份。

### Non-Goals

- 不改记忆召回与检索契约（`memory-retrieval-baseline`、`memory-engine-catalog` 范围内）。本 change 只动画像层，不动 `memory_items` 的向量/BM25 检索面。
- 不改 PersonaProfile 与 RelationshipState 的快照、锁定与单写者语义（`persona-relationship` 已覆盖）。
- 不把会话内容改成 canonical 流直读；turn 内部仍消费 session view。
- 不删除磁盘上的历史 markdown 文件，也不提供租户化之后的 markdown 双写兼容层。
- 不引入生产可用的租户切换能力，切换入口限本地 dev-only 并说明为何不得进生产。

## Capabilities

### New Capabilities

- `tenant-memory-profile`: 记忆画像层五类内容（`long_term` / `pending` / `recent_context` / `history` / `journal`）的租户归属规则、按后端的唯一事实源、prompt 组装只读本租户、从全局 markdown 迁移的幂等要求，以及单机模式按固定租户走同一套接口的边界。自我认知那一格不在本 capability 内，归 `persona-relationship`。

### Modified Capabilities

- `persona-relationship`: 「PG 当前值存储、恢复与配置边界」这条 Requirement 现写明 PersonaProfile / RelationshipState 以 PostgreSQL 为规范 source of truth，且「单体 / SQLite dev 模式 SHALL 继续沿用现有文件语义（行为不变）」。本 change 要把它改成：两种后端各用所属库内的同名表、走同一套读写与审计语义，文件语义退出；`[agent.persona]` 从"单体兼容默认"收窄为"单机固定租户的初始 seed"。

判断依据：`canonical-identity` 的「旧单体数据隔离」只约束旧单体 **SQLite** 的导入与回退路径，文本不因本 change 改变，不建 delta。`persona-relationship` 的「跨 tenant 不串」也不变，变的是它的存储边界条款（上一条）。记忆画像层本身在现有 19 份主 spec 中零命中（`RECENT_CONTEXT` / `markdown` 全库检索无结果），属新增契约。

## Impact

**代码**：`agent/memory_pg.py`、`agent/memory.py`、`core/memory/markdown.py`、`core/memory/runtime.py`、`agent/context.py`、`agent/core/prompt_block.py`（含 `SectionCache` 的 scope 与身份块的手工 fail-closed）、`agent/looping/core.py`、`agent/lifecycle/types.py`、`agent/lifecycle/phases/before_reasoning.py`、`agent/lifecycle/phases/prompt_render.py`、`bootstrap/memory.py`、`bootstrap/persona.py`、`bootstrap/tools.py`、`bootstrap/app.py`、`bootstrap/proactive.py`、`plugins/drift_flow/*`；`agent/tools/path_resolver.py` 需把租户 category 根解析抽成公共 helper（`bootstrap/attachments/blob_store.py:71-85` 目前复制了同款逻辑）。另需一个 SQLite 侧的画像存储实现，落在 `memory2/` 已有的 `memory_items` 表上，与 PG 实现同接口。

**数据**：`memory_items` 新增五类 `memory_type` 行的写入方，`app_configs` 承接合并幂等键。PG 侧无新表、alembic 零迁移（persona 三表与 `memory_items` 都已在 head）。SQLite 侧 `memory2/store.py` 的建表语句要加 `tenant_persona_profiles` 与 `persona_audit_events` 两张——同一结构从此有两个出处（PG 走 alembic、SQLite 走建表语句），需一个跨后端列集合一致性测试兜住。

**测试**：至少 9 处断言 `workspace/memory/` 路径或 markdown 单例语义的用例需同步——`tests/test_runtime_smoke.py:312`、`tests/test_channel_clients.py:1084`、`tests/persona/test_prompt_blocks.py:28-34`、`tests/memory_fakes.py:25`、`tests/test_memory_store_questions.py`、`tests/test_memory_optimizer.py`、`tests/test_memory_engine_contract.py:366/832/932`、`tests/c8/test_lease_coverage.py`。

**最大结构性风险**：`core/memory/markdown.py:61-80` 的 `MemoryProfileApi` 协议全部是同步方法，而 `PgMemoryMarkdownStore` 全部方法是 async，prompt 侧是同步调用。这个错位的取舍是 design.md 的 ADR-1。
