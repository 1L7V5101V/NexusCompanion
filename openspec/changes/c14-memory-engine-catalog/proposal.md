## Why

M-P1 剩余 capability 中 C9/C10 已落地，C14（memory engine catalog/binding + WebChat selector）是 P1 出口条件「WebChat 提供 memory engine selector」的最后一块：没有它，租户仍只能使用进程级配置（`config.memory.engine`）决定的单一引擎，租户无法在允许目录内选择 `default` 或 `rachael`，§5.9.16 的 memory engine slot（required、每 tenant 恰一个 active、切换只影响后续 work）完全未落地。依赖前置 C1（tenant identity）/C4（WebChat 前端）/C5（auth）/C8（RuntimeSnapshot + TenantRuntimePlan seam + binding_policy 元数据）均已归档；C8 明确把「tenant binding durable 存储 + tenant_policy_revision 提升归 C14」，`agent/tools/catalog.py` 也把「多引擎并存时的 engine_binding 过滤」留给本 change。与 C13（BM25/ParadeDB/jieba/reranker 召回质量改造）按 D7 解耦，互不依赖。

## What Changes

- **新增 `tenant_memory_engine_bindings` / `tenant_memory_engine_events` 两表 + Alembic 迁移**：每 tenant 恰一行 active binding（`tenant_id` 主键 = 单 active 约束）记录 `engine_id` + `tenant_policy_revision`（BIGINT，从 0 起）；事件表按 `(tenant_id, engine_id, action, revision)` 追踪 initial/switch 历史。纯 expand-only。
- **新增 memory engine catalog（代码冻结常量）+ binding 服务**：catalog 按 §5.9.16 固定表声明 `default`（default_on，初始绑定）与 `rachael`（opt_in，已安装可选实现）；服务提供 `ensure_binding`（幂等建初始绑定，provisioning 与懒补齐共用）、`get_active_engine`、`switch`（校验 + revision +1 + 事件）、只读目录/状态描述。inspector（default-memory inspector）不属于目录，是 admin observability contribution。
- **新增运行时 per-work 引擎解析**：work start（snapshot lease 作用域内）解析一次 tenant 的 active engine，写入 work-scoped 状态并显式穿线——`TurnState.memory_engine` → `RetrievalRequest.engine_binding`（检索管线按绑定走单引擎路径，进行中 work 不换引擎）+ `ToolExecutionContext.memory_engine`（经 `tool_kwargs()` 注入，memory 工具按租户 active engine 分发）。多引擎并存时 memory 工具去重为同名分发器（修复双引擎 `recall_memory` 重复注册启动崩溃），C7 租户目录按 active engine 的 tool_profile 过滤 engine 注入工具；TurnCommitted/ConsolidationCommitted 由 active engine 独占处理（event bus 每引擎包装门控），切换不迁移/不删除旧引擎数据。
- **新增用户面端点（`bootstrap/chat_api.py`，PG durable + auth 模式装配）**：`GET /api/memory/engines`（服务端允许目录 + 能力/ready 状态 + 当前 active）、`PUT /api/memory/engines/active`（提交选择；服务端校验目录成员、opt-in 允许、engine readiness；客户端字段只是设置请求，不参与授权）。
- **新增 WebChat selector UI**：聊天界面内嵌轻量引擎选择器（展示目录项能力/状态/当前 active，提交切换，不可用项禁选），仅 PG durable + auth 模式出现。
- **不触碰**：召回质量链路（C13：`DefaultMemoryEngine`/`Retriever` 的 dense/keyword/RRF 内部零改动）、RuntimeSnapshot 生成与 lease 机制（C8，只消费 snapshot/lease 时机）、auth 端点语义（C5，只消费 session/principal）、WebChat 协议本体（C4）；`tenant_id + engine_id` 命名空间下的既有存储不动（不迁移、不合并、不删除）。

## Capabilities

### New Capabilities

- `memory-engine-catalog`: memory engine 插件目录/绑定（服务端允许目录、单 active 约束、初始 `default`、revision 提升语义、切换生效时机、数据隔离与可追踪、inspector admin-only、与 C13 解耦）+ WebChat selector（目录读取与选择提交）。

### Modified Capabilities

<!-- 无 SHALL 级语义变更：tenant-tool-isolation 的租户目录规则本就声明「engine 注入
工具随引擎注册自然进入 registry（启用该引擎的租户可见）」，本 change 使该规则在
多引擎并存下按 active engine 生效；runtimesnapshot-secrets 的 TenantRuntimePlan
seam 语义不变（engine_binding 字段首次有了生产写入方）。 -->

## Impact

- **代码**：`alembic/versions/`（新迁移）、`bootstrap/db/models/memory_engine.py`（新）、`bootstrap/db/repository/memory_engine_repo.py`（新）、`bootstrap/memory_binding.py`（catalog + binding 服务，新）、`bootstrap/memory.py`（多引擎构建去重 + 事件门控包装）、`agent/tools/meta/register.py`（同名分发器）、`agent/tools/context.py`（`memory_engine` 可信字段）、`agent/tools/catalog.py`（engine 工具按 active engine 过滤）、`agent/lifecycle/types.py`（`TurnState.memory_engine`）、`agent/core/passive_turn.py`（ToolExecutionContext 戳 + RetrievalRequest 穿线）、`agent/retrieval/protocol.py` + `agent/retrieval/default_pipeline.py`（`engine_binding` 单引擎路径）、`agent/looping/core.py`（work-start 解析）、`bootstrap/app.py`（装配）、`bootstrap/chat_api.py`（端点）、`frontend/chat/src/`（selector）、`config.example.toml`（多引擎示例 + 开关注释）。
- **测试**：`tests/memory_engines/`（新：目录只读性、未授权切换负向、revision 提升、单 active 约束、生效时机、数据隔离/可追踪、权限、多引擎注册分发）；依赖范围检查（无 BM25/ParadeDB/jieba/reranker 依赖）；全量回归。
- **部署**：生产 `config.toml` 的 `[memory] engine` 需含 `default`（初始绑定引擎必须 ready）；切换建议经新端点/selector 执行；存量租户首次访问由懒补齐建初始绑定（`default`），如需继续使用 `rachael` 由用户/管理员经 selector 切换（一次性操作，不迁移数据）。
