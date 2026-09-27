# C13 memory retrieval BM25/hotness/RRF 改造 + 离线评测

> 对应任务计划：`openspec/openspec-tasks-bundle/task-13-memory-retrieval-bm25.md`（PILOT_ROADMAP §5.9.10 第 13 项）。
> 输入的已冻结决策（§4.4 default 引擎召回目标、§4.1 当前实现基线、§10 DEFERRED BY EVIDENCE 检索增强开关、D6 独立性边界）不在此重复论证，design.md 逐条引用。

## Why

default engine 是 Pilot 的召回质量基线，但当前 sparse lane 只有「SQLite FTS5 BM25（keyword_score 为未归一化 raw 值）+ PG summary ILIKE 保底」：BM25 raw 分数与 hotness、dense 语义分数量纲不同，keyword lane 的 RRF 排名直接沿用 lane 顺序（BM25 raw 排名），未与 hotness 融合；`answer` intent 的 HyDE-style hypothesis 无显式开关、默认路径隐式调用（每次 answer 检索固定 2 次 light LLM 调用）；query rewrite 与 reranker 无接口无开关；全链路没有可复现的离线评测，无法依据 §10 DEFERRED BY EVIDENCE 决定是否开启检索增强。

本 change 建立 §4.4 冻结的目标召回基线：**raw query + dense(semantic+hotness) + BM25（jieba + FTS5/ParadeDB pg_search；BM25 归一化 + hotness 融合为 sparse final score）+ RRF + top-k**，score 字段全程分离，三个实验开关默认关闭，并交付可复现的离线评测与 A/B/C/D 消融矩阵。

## What Changes

- **sparse lane 改造**（`memory2/`）：
  - `memory2/tokenizer.py` — jieba 查询分词（`jieba.lcut_for_search` + 停用词/单字过滤 + ASCII token；jieba 不可用时回退既有 regex bigram），作为 keyword lane 统一 term 来源；
  - `memory2/sparse_lane.py` — BM25 query-local 归一化（`raw/(raw+K)`）+ hotness 融合为 `sparse_final`（`(1-αs)·bm25_normalized + αs·hotness`）；LIKE 保底结果用 term 命中率作归一化基底（`bm25_raw=None`）；
  - `memory2/retriever.py` — keyword lane 命中补齐 `bm25_raw` / `bm25_normalized` / `hotness` / `sparse_final` / `sparse_source` 字段；RRF 输入排名改为 **dense 侧按 `_score_debug.final`（dense final）、sparse 侧按 `sparse_final`**，不再直接比较 cosine/BM25/hotness 原始数值；RRF K 与 keyword 权重改为可配置（默认维持现状 60 / 0.5）。
- **ParadeDB pg_search 接入**（`infra/storage/postgres_memory_store.py`）：运行时探测 `pg_search` 扩展 → 可用则幂等建 bm25 索引并实现 `keyword_search_bm25`（jieba terms + raw query）；不可用或查询失败 → 降级既有 `keyword_search_summary`。不做 alembic 硬迁移（避免非 ParadeDB PG 启动崩溃），部署要求记入 design.md（ADR-4）。
- **三实验开关（默认全关）**（`plugins/default_memory/config.py` `[retrieval.experimental]`）：
  - `hyde_enabled`（默认 false）— 关闭时 `_query_answer` 不再发起旧 answer HyDE 的 light LLM 调用；开启时行为与现状一致；
  - `query_rewrite_enabled`（默认 false）— 开启时 engine 内 fail-open 查询改写钩子（light provider）；
  - `reranker_enabled`（默认 false）— 开启时 `memory2/reranker.py`（Reranker 协议 + light LLM listwise 实现）对 RRF 截断后候选重排，fail-open 回 RRF 顺序。
- **score 分离契约 fixture**：`tests/fixtures/memory_score_fields.json` — semantic / BM25 raw / normalized / hotness / dense final / sparse final / rrf score 七字段契约（单一来源，测试逐一断言）。
- **离线评测**（`eval/memory_retrieval/`）：确定性合成数据集 + 确定性 stub embedder（hashed bag-of-words，无网络无 API key）+ 指标（Recall@k / MRR / nDCG@k / 注入命中率 / P95 / 成本计数 / 失败降级）+ `run_eval.py` / `run_ablation.py`（A/B/C/D 消融矩阵）。
- **测试**：score 字段断言、RRF 输入断言（排名来自 lane final、不比较异构原始数值）、默认路径负向测试（三开关默认关闭、answer intent 零 LLM 调用）、pg_search 探测/降级 fake 测试、评测脚本冒烟。
- **证据**：`openspec/evidence/c13-memory-retrieval-bm25/` — 默认 A 基线评测产出（JSON+markdown）、消融矩阵运行结果、pytest/pyright 输出。

## Capabilities

### New Capabilities

- `memory-retrieval-baseline`：default engine 召回基线 = raw query + dense(semantic+hotness) + BM25/hotness + RRF + top-k；sparse lane 分数归一化后与 hotness 融合；semantic / BM25 raw / normalized / hotness / dense final / sparse final / rrf score 分离；RRF 输入排名只来自 dense final 与 sparse final；HyDE/query rewrite/reranker 三开关默认关闭且不进入默认路径；hotness 为双 lane 长期保留默认组成；离线评测与消融矩阵可复现。

### Modified Capabilities

- 无（既有 memory 相关行为仅在 keyword lane 内叠加归一化与融合字段；dense lane 的 M4H-3 契约——lane 内 α=0、post-RRF β 乘性增强——保持不变）。

## Non-Goals（明确不做）

- **不触碰 WebChat/auth/tool/RuntimeSnapshot**（D6 独立性边界）：无 `infra/channels/`、`bootstrap/chat_api.py`、认证、工具注册、TenantRuntimePlan 变更；C14 的 engine catalog/binding 不在本 change（只保证 default engine 改造可被 C14 消费）。
- **不改 rachael 引擎**：rachael 有独立检索链路，不复用本 change 的 sparse lane。
- **不做 alembic 硬迁移**：pg_search bm25 索引采用运行时探测 + 幂等建索引 + 失败降级（ADR-4）。
- **不默认开启任何实验开关**：HyDE/rewrite/reranker 默认关闭（§10 DEFERRED BY EVIDENCE），开启与否由离线评测与 Pilot 消融结果决定。
- **不接入真实对话数据评测**：离线评测使用确定性合成数据集（隐私 + 可复现）；真实数据评测归 Pilot 运行期（§10）。
- **不冻结 SLO 数字**：P95 等只记录观测值，不做阈值断言（对齐 C12 的 SLO 红线约束）。

## Impact

- **代码**：新增 `memory2/tokenizer.py`、`memory2/sparse_lane.py`、`memory2/reranker.py`、`eval/memory_retrieval/`；修改 `memory2/retriever.py`（keyword lane 字段 + RRF 输入 + 可配置 RRF 参数 + optional reranker hook）、`infra/storage/postgres_memory_store.py`（keyword_search_bm25 + pg_search 探测）、`plugins/default_memory/config.py`（新配置节 + 渲染同步）、`plugins/default_memory/engine.py`（HyDE 开关、rewrite 钩子、reranker 接线）。
- **配置**：`plugins/default_memory/config.local.toml` 新增 `[retrieval.sparse]`、`[retrieval.rrf]`、`[retrieval.experimental]` 三节（默认值冻结在代码 dataclass）；主 `config.toml` 不变。
- **依赖**：无新增第三方依赖（jieba 已在 requirements）。
- **测试**：新增 `tests/test_memory2_sparse_lane.py`、`tests/test_memory2_rrf_inputs.py`、`tests/test_default_memory_experiments_off.py`、`tests/test_postgres_memory_bm25.py`、`tests/test_memory_score_fields_contract.py`、`tests/test_memory_retrieval_eval.py`。
- **兼容性**：默认行为变化仅两处且均为目标基线本体——keyword lane 排名从 BM25 raw 顺序改为 sparse_final 排序（含 hotness），answer intent 默认不再发起 HyDE LLM 调用（省 2 次 light 调用）；`recall_memory` 工具契约、注入块格式、store 契约不变。
