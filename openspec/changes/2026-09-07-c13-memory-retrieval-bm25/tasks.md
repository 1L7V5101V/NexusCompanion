# C13 memory retrieval BM25/hotness/RRF — 实施任务

> 对应 `openspec/openspec-tasks-bundle/task-13-memory-retrieval-bm25.md`；证据统一落 `openspec/evidence/c13-memory-retrieval-bm25/`。
> 管理闭环：任务 checkbox → `openspec status` → evidence → 仅在有 evidence 时更新 task-13 / PILOT_ROADMAP_PROJECT_CHECKLIST 状态。
> 独立性边界（D6）：本 change 不触碰 WebChat/auth/tool/RuntimeSnapshot；不阻塞 C4/C5。

## 1. change 文档

- [x] 1.1 proposal/design/specs/tasks 四件套落盘。验证：`openspec validate` 通过；ADR-1..ADR-7 覆盖 task-13「风险与需冻结决策」全部条目

## 2. sparse lane：jieba 分词 + BM25 归一化 + hotness 融合（ADR-1）

- [x] 2.1 `memory2/tokenizer.py`：`tokenize_query()`（jieba `lcut_for_search` + 停用词/单字过滤 + ASCII token 保底 + regex fallback）；`memory2/retriever.py` 的 keyword lane term 提取切换到该模块。验证：单元测试（中英混合分词、jieba 缺失回退）
- [x] 2.2 `memory2/sparse_lane.py`：`normalize_bm25(raw, k)`（raw/(raw+K)）、`fuse_sparse_final(normalized, hotness, alpha)`、LIKE 保底命中率归一化；keyword lane 命中补齐 `bm25_raw`/`bm25_normalized`/`hotness`/`sparse_final`/`sparse_source` 字段。验证：公式与单调性单元测试
- [x] 2.3 `[retrieval.sparse]` 配置节（`normalization_k` 默认 4.0、`hotness_alpha` 默认 0.2）：`plugins/default_memory/config.py` dataclass + TOML 解析 + `render_default_memory_config` 渲染三处同步。验证：`tests/test_default_memory_plugin_config.py` 扩展

## 3. RRF 输入改为 lane final 排名（ADR-2/3/6）

- [x] 3.1 `memory2/retriever.py`：dense 侧排名键 = `_score_debug.final`（fallback `score`）；sparse 侧排名键 = `sparse_final`（fallback lane 原顺序）；`_lane_ranks` 记录每侧 rank；`rrf_score` 保留融合前原始值
- [x] 3.2 `[retrieval.rrf]` 配置节（`k`=60、`keyword_weight`=0.5、`hotness_beta`=0.05）；默认值与现状兼容
- [x] 3.3 注入排序改为 RRF/reranker 返回序（`_select_injection_sections` 不再按 `score` 重排覆盖 RRF 序；阈值与分区配额不变）。验证：注入序 = RRF 序测试
- [x] 3.4 测试 `tests/test_memory2_rrf_inputs.py`：raw 值序与 final 序相反场景下 RRF 序随 final 序；异构 raw 数值（BM25 巨值 vs cosine）不跨 lane 比较。验证：pytest 通过

## 4. score 分离契约 fixture（验收 1）

- [x] 4.1 `tests/fixtures/memory_score_fields.json`：semantic/bm25_raw/bm25_normalized/hotness/dense_final/sparse_final/rrf_score 七字段契约（lane、类型、不变式）
- [x] 4.2 `tests/test_memory2_sparse_lane.py` + `tests/test_memory_score_fields_contract.py`：公式断言、字段存在断言、fixture 与代码字段交叉校验。验证：pytest 通过

## 5. ParadeDB pg_search 接入（ADR-4）

- [x] 5.1 `infra/storage/postgres_memory_store.py`：`pg_search` 运行时探测（`pg_available_extensions`/`pg_extension`，结果缓存）+ 幂等 `CREATE INDEX IF NOT EXISTS ... USING bm25` + `keyword_search_bm25`（jieba terms，`@@@` + `pdb.score()`，异常返回空列表）
- [x] 5.2 降级路径：不可用/失败 → 既有 `keyword_search_summary`；fake connection 单测覆盖探测分支、SQL 构造、降级分支。验证：`tests/test_postgres_memory_bm25.py` 通过

## 6. 三实验开关默认关闭（ADR-5，验收 3）

- [x] 6.1 `[retrieval.experimental]` 配置节（`hyde_enabled`/`query_rewrite_enabled`/`reranker_enabled` 默认 false）+ 渲染同步
- [x] 6.2 HyDE 开关：`hyde_enabled=false` 时 `_query_answer` 跳过 hypothesis 生成（light provider 零调用、`trace["hyde_hypotheses"]=[]`）；`true` 行为与现状一致
- [x] 6.3 query rewrite 钩子：fail-open 改写（失败/超时 → 原 query），trace 记录 `query_rewrite_applied`；默认关闭零调用
- [x] 6.4 `memory2/reranker.py`：`Reranker` 协议 + `LightLLMReranker`（listwise，fail-open）；Retriever optional 注入，RRF 截断后应用；默认不注入
- [x] 6.5 负向测试 `tests/test_default_memory_experiments_off.py`：默认配置 answer intent 零 light LLM 调用；reranker 未注入时 RRF 序保留；rewrite 关闭无改写。验证：pytest 通过

## 7. 离线评测与消融矩阵（ADR-7，验收 2/6）

- [x] 7.1 `eval/memory_retrieval/`：`dataset.py`（固定种子合成数据集）、`embedder_stub.py`（确定性 hashed bag-of-words，无网络）、`metrics.py`（Recall@k/MRR/nDCG@k/注入命中率/P95/成本/失败降级）
- [x] 7.2 `run_eval.py`：默认 A 基线运行，输出 JSON + markdown
- [x] 7.3 `run_ablation.py`：A/B/C/D 消融矩阵（全部保留双 lane hotness），输出对比表
- [x] 7.4 评测冒烟测试 `tests/test_memory_retrieval_eval.py`。验证：pytest 通过

## 8. 证据与状态（§8 证据驱动）

- [x] 8.1 `openspec/evidence/c13-memory-retrieval-bm25/`：A 基线评测 JSON+markdown、消融矩阵结果、pytest 输出、pyright 输出存档
- [x] 8.2 全量回归：`pytest -q -W error tests/` 对齐 main 基线（1312 passed + 1 既有环境性失败）；`pyright` 对齐基线（36 既有错误）
- [x] 8.3 diff 范围检查：变更文件不包含 WebChat/auth/tool/RuntimeSnapshot 路径（验收 5/7）
- [ ] 8.4 merge 后按 §8 更新 task-13 状态与 `PILOT_ROADMAP_PROJECT_CHECKLIST.md`（仅 evidence 齐全时）
