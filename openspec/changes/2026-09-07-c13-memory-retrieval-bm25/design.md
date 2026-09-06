# C13 memory retrieval BM25/hotness/RRF — 设计

> 对应 `openspec/openspec-tasks-bundle/task-13-memory-retrieval-bm25.md`；冻结决策来源：PILOT_ROADMAP §4.1（当前基线）、§4.4（default 引擎目标实现）、§5.9.10 第 13 项、§10（DEFERRED BY EVIDENCE）。

## 1. 当前基线（§4.1 引用，实现前已核实）

- dense lane：`store.vector_search` 返回 `score = (1-α)·semantic + α·hotness`（`hotness_alpha` 默认 0，lane 内热度混合已置零），`_score_debug = {semantic, hotness, final}` 恒在；热度经 Retriever RRF 融合后按 `rrf_score × (1 + β×hotness)` 乘性增强（β 默认 0.05，双 lane 一视同仁）。该契约由 `tests/test_memory2_retrieval_baseline.py` 与 `tests/test_recall_memory_tool.py::test_retriever_post_rrf_hotness_boost_reorders_close_hits` 锁定，本 change 不破坏。
- sparse lane：SQLite `keyword_search_bm25`（FTS5 trigram，`keyword_score = -bm25 raw`，未归一化）→ 无结果时 `keyword_search_summary`（OR-LIKE，`keyword_score = term 命中数`）；PG 仅 `keyword_search_summary`（summary ILIKE，`kw_score = 命中数`）。
- keyword lane 的 RRF 排名直接沿用 lane 返回顺序（= BM25 raw 排名），未融合 hotness。
- `answer` intent 每次 2 次 light LLM HyDE hypothesis 调用，无开关。
- jieba 已在依赖中（`requirements.txt`、`pyproject.toml`），rachael 插件已有 jieba + FTS5 先例。

## 2. ADR（冻结决策）

### ADR-1 sparse lane 分数链：bm25_raw → query-local 归一化 → 与 hotness 融合为 sparse_final

§4.4：「BM25 lane 先做 query-local score normalization，再与 hotness 融合为 sparse final score」。

- 归一化函数：`bm25_normalized = raw / (raw + K)`，`K` 可配置（`[retrieval.sparse].normalization_k`，默认 4.0）。选择 `raw/(raw+K)` 而非 min-max：单命中结果集 min-max 退化（max-min=0），且 BM25 raw 无上界，饱和式归一化对分布稳健、单调、query-local。
- 融合：`sparse_final = (1-αs)·bm25_normalized + αs·hotness`，`αs` 可配置（`[retrieval.sparse].hotness_alpha`，默认 0.2）。§4.4 要求 hotness 是 sparse lane 的**默认组成**，故 αs 默认 > 0；公式形式与 dense lane 既有 `final = (1-α)·semantic + α·hotness` 同构。
- LIKE 保底结果无 BM25 raw：`bm25_raw = None`，`bm25_normalized = min(1.0, term 命中数 / len(terms))`，`sparse_source = "like_fallback"`；BM25 命中 `sparse_source = "bm25"`。fallback 与 BM25 不互比 raw 值，只以各自 normalized 进入同一融合公式。
- hotness 计算复用 `memory2.store._hotness_score`（频度 × 半衰期时间衰减，emotional_weight 拉长半衰期），与 dense lane 同一公式。
- **score 字段分离**（验收 1）：keyword lane 命中补齐 `bm25_raw` / `bm25_normalized` / `hotness` / `sparse_final` / `sparse_source`（顶层观测字段）；dense lane 命中已有 `_score_debug.{semantic,hotness,final}`；RRF 产物额外带 `rrf_score`（融合前原始 RRF 值）与 `_lane_ranks = {"dense": r?, "sparse": r?}`。任何环节不得把异构分相加或互相覆盖；`score` 字段语义保持现状（dense 命中 = dense final；keyword-only 命中 = sparse_final，取代原 `keyword_score` 回填）。
- 契约 fixture：`tests/fixtures/memory_score_fields.json` 逐一声明七字段（semantic / bm25_raw / bm25_normalized / hotness / dense_final / sparse_final / rrf_score）的出现 lane、类型与不变式，测试对 fixture 契约断言。

### ADR-2 RRF 输入排名只来自 lane final（验收 4 负向断言）

§4.4：「RRF 的输入排名应分别来自 dense final score 与 sparse final score，而不是直接比较 cosine、BM25 和 hotness 的原始数值」。

- dense 侧排名键：`_score_debug.final`（缺失时 fallback `score`；α=0 时 final==semantic，与现状兼容）。
- sparse 侧排名键：`sparse_final`（缺失时保持 lane 原顺序，兼容外部 fake store）。
- 排名只产生 rank 整数进入 `1/(k+rank)`；cosine、BM25 raw、hotness 数值**永不**跨 lane 比较。测试构造「raw 值序与 final 序相反」的双 lane 场景断言 RRF 序随 final 序。
- post-RRF β 乘性增强保留（现有已验证契约）：sparse_final 内的 hotness 影响 sparse 侧 rank，β 在融合后再做一次统一形状调整；两者语义不同（rank 决定 vs 融合后微调），并存不冲突，documented tradeoff。
- RRF 参数可配置：`[retrieval.rrf].k`（默认 60）、`[retrieval.rrf].keyword_weight`（默认 0.5）、`[retrieval.rrf].hotness_beta`（默认 0.05，即现 `_POST_RRF_HOTNESS_BETA`）；默认值下行为与现状兼容（除 sparse 侧 rank 键变化）。

### ADR-3 dense lane 契约不变

M4H-3 已把 dense lane 内热度混合置零（α=0）并移到 post-RRF β。本 change 不改 dense lane 默认值：`hotness_alpha` 仍默认 0，`vector_search` 纯 cosine 契约不变；dense final = `_score_debug.final` 在 α>0 时自然含热度（store 已实现），是「dense semantic/hotness → dense final score」目标的既有载体。§4.4 的「hotness 作用于 dense lane」由 post-RRF β（默认路径）+ α（可配置）两个机制承载，均为长期保留默认组成。

### ADR-4 ParadeDB pg_search：运行时探测 + 幂等建索引 + 失败降级，不做硬迁移

- 探测：查询 `pg_available_extensions WHERE name='pg_search'`（可用）与 `pg_extension`（已安装）；每次 store 初始化探测一次，结果缓存。
- 可用时：幂等尝试 `CREATE INDEX IF NOT EXISTS ... USING bm25 (id, summary) WITH (key_field='id')`（索引名固定，异常捕获降级为 ILIKE 路径并记日志）；`keyword_search_bm25` 以 `summary @@@ :query` + `pdb.score()` 排序，query 由 jieba terms 构造（空格 OR 连接）。
- 不可用/失败时：`keyword_search_bm25` 返回空列表，调用方降级既有 `keyword_search_summary`（与 SQLite FTS5 不可用时的降级路径同构）。
- **不做 alembic 迁移**：硬迁移会在无 pg_search 的标准 PG（含本地 Windows 便携 PG、当前生产 PG 镜像）上启动崩溃；探测式接入保证单一代码路径在两种 PG 上都能启动。
- 部署要求（P0/P1 部署阶段执行，不在本 change）：生产 PG 需 ParadeDB 镜像或手工安装 pg_search 扩展后重建 store。本地 Windows 便携 PG 无 ParadeDB 二进制，pg_search 路径以 fake connection 单测覆盖（SQL 构造、探测分支、降级分支），真机验证归部署阶段记录。

### ADR-5 三实验开关（验收 3）：默认关闭 + 默认路径负向测试

§4.4：「HyDE-style hypothesis、query rewrite 和 reranker 都先做成默认关闭的实验开关，不进入默认路径」；「当前 answer intent 已有 HyDE 行为，但目标实现必须增加显式开关，并确保 default-only Pilot 不因兼容旧逻辑而隐式调用」。

- 配置面：`[retrieval.experimental] hyde_enabled / query_rewrite_enabled / reranker_enabled`，默认全 false；`render_default_memory_config()` 同步渲染（setup wizard 复用，保持三处同步：dataclass 默认值、TOML 解析、渲染函数）。
- **HyDE**：`hyde_enabled=false` 时 `_query_answer` 直接跳过 `_gen_hypothesis`，`aux_queries=[]`，`trace["hyde_hypotheses"]=[]`；light provider 零调用。`true` 时行为与现状逐字节一致。
- **query rewrite**：开启时 engine 在检索前用 light provider 做 fail-open 改写（改写失败/超时/空结果 → 原 query）；`_query_context` 与 `_query_answer` 共用入口；trace 记录 `query_rewrite_applied`。默认关闭时零 LLM 调用。
- **reranker**：`memory2/reranker.py` 定义 `Reranker` 协议（`async rerank(query, items) -> items`）与 `LightLLMReranker`（light provider listwise 重排：输入 RRF 截断后候选 id+summary，输出 id 序，解析失败/超时 → 原顺序 fail-open）。Retriever 增加可选 `reranker` 参数（构造注入），在 RRF 截断后应用；引擎仅在 `reranker_enabled=true` 时注入。启用后最终注入排序以 reranker 结果为准（满足 §4.4「不能继续无意间按 dense score 覆盖排序」——注入筛选保持稳定同序处理，见 ADR-6）。
- 负向测试：默认配置下 answer intent 检索对 light provider 零调用（记录调用次数断言 0）；reranker 未注入时 RRF 顺序原样保留；rewrite 关闭时无改写调用。

### ADR-6 注入排序：RRF 顺序为默认顺序

§4.1 已知问题：「当前注入阶段又按 score 排序和阈值过滤，因此可能覆盖 RRF 排序；后续应明确以 RRF 结果为默认顺序」。本 change 将 `_select_injection_sections` 的排序键从 `item["score"]` 改为 RRF 产物顺序（items 已按 RRF/reranker 序返回，注入按返回序稳定遍历，仅类型阈值过滤 + 分区配额不变）。阈值语义不变：procedure/preference 与 event/profile 的 score_threshold 仍作用于各 item 的 `score`（dense=final / keyword-only=sparse_final）。这是 §4.4「RRF 候选 → top-k → 注入」目标链路的直接推论。

### ADR-7 离线评测：确定性合成数据集 + stub embedder，无网络依赖（验收 2、6）

- 数据集：`eval/memory_retrieval/dataset.py` 固定种子生成（中文+英文混合，~120 记忆条目 / ~40 查询，含 graded relevance 0-3、paraphrase 干扰项、时间线索查询）；合成而非真实对话——隐私（不入库真实用户内容）+ 可复现（种子固定，任何机器重跑数值一致）。
- embedder：`eval/memory_retrieval/embedder_stub.py` 确定性 hashed bag-of-words（jieba 分词 + sha1 桶映射，256 维，L2 归一化），与 `Embedder` 同接口（async `embed`）；不调外部 API，评测零成本、零网络。语义检索质量因此等价词面检索——这是有意为之的离线基线边界，衡量的是**管线**（归一化/融合/RRF/开关）而非 embedding 模型质量；真实 embedding 质量评测归 Pilot 运行期（§10 DEFERRED BY EVIDENCE）。
- 指标（`metrics.py`）：Recall@k、MRR、nDCG@k（graded relevance）、注入命中率（`build_injection_block` 产出包含相关条目的查询占比）、P95 延迟（per-query 检索耗时）、成本（embed 调用数、LLM 调用数计数）、失败降级（注入 embed 异常后重跑，记录 keyword-only Recall@k 退化幅度）。
- 消融矩阵（`run_ablation.py`）：A=默认（dense+BM25/hotness+RRF）；B=A+HyDE（stub light provider 生成假设文本）；C=A+query rewrite；D=A+reranker（确定性 stub reranker）。所有实验保留双 lane hotness（开关矩阵不含 hotness 移除项，§4.4）。输出 JSON + markdown 表存档 `openspec/evidence/c13-memory-retrieval-bm25/`。
- P95 只记录观测值，不做阈值断言（对齐 C12「SLO 红线不进代码」约束）。

## 3. 测试映射（验收标准 → 测试）

| task-13 验收标准 | 验证载体 |
| --- | --- |
| 1. sparse 归一化+hotness 融合；七字段分离 | `tests/test_memory2_sparse_lane.py`（公式/单调/字段存在）+ `tests/test_memory_score_fields_contract.py`（fixture 契约） |
| 2. 默认 A 基线可复现 | `eval/memory_retrieval/run_eval.py` 产出存档 `openspec/evidence/c13-memory-retrieval-bm25/` + `tests/test_memory_retrieval_eval.py` 冒烟 |
| 3. 三开关默认关闭不进默认路径 | `tests/test_default_memory_experiments_off.py`（负向：零 LLM 调用/顺序保留） |
| 4. RRF 输入来自 lane final | `tests/test_memory2_rrf_inputs.py`（raw 序 ≠ final 序场景） |
| 5/7. 不触碰 WebChat/auth/tool/RuntimeSnapshot | PR diff 范围检查（本 change 文件清单即边界） |
| 6. 消融矩阵可运行、双 lane hotness 保留 | `eval/memory_retrieval/run_ablation.py` 运行产出 |

## 4. 风险

- keyword lane 默认排序变化（BM25 raw 顺序 → sparse_final 序）：αs=0.2 时 hotness 可微调 keyword 侧排名，属目标基线本体（§4.4）；RRF K=60 下 rank 变化对融合分影响温和；评测 A 基线提供改造前后可比数据（改前数据由 `keyword_enabled` + 关闭 sparse 融合的 legacy 分支不可得——以评测数据集上的 A 配置数值为准）。
- pg_search 代码路径本地不可真机验证：fake conn 单测覆盖 + 失败降级兜底；真机验证归部署阶段（ADR-4）。
- answer intent 关闭 HyDE 属默认行为变化（省 2 次 light LLM 调用/次检索）：由验收 3 强制要求；`hyde_enabled=true` 可完全恢复旧行为。
