# Task-13 — memory retrieval BM25/hotness/RRF 改造 + 离线评测（memory-retrieval-bm25）

> 编号对应 PILOT_ROADMAP §5.9.10 第 13 项。状态标记复用 §8。独立质量能力（D6），不阻塞 C4/C5。

## 元数据

- **所属阶段**：主要里程碑 = P0（独立质量基线，可并行根之一）
- **§5.9 引用**：§4.1（当前 DefaultMemoryEngine 实现）、§4.4（default 引擎 Pilot 目标实现）、§5.9.10（独立性：13 不阻塞 4、5）、§10 DEFERRED BY EVIDENCE（Retrieval 增强开关）
- **§6 出口条件引用**：P0 出口「以独立质量 change 建立 default engine 的 raw query + dense semantic/hotness + BM25/hotness + RRF + top-k 记忆召回基线，并将两条 lane 的 hotness 作为长期保留的默认组成；该 change 不阻塞 P0.5/P1 的 WebChat 与认证安全闭环」
- **状态**：planned

## 目标

建立 default engine 召回基线 = **raw query + dense(semantic+hotness) + BM25（jieba + ParadeDB pg_search；BM25 归一化 + hotness 融合为 sparse final score）+ RRF + top-k**；保留 semantic / BM25 raw / normalized / hotness / dense final / sparse final / rrf score **分离**（不混为一个 score）；HyDE/query rewrite/reranker 三开关默认关闭 + 消融矩阵（A/B/C/D，§4.4）；离线评测数据集（Recall@k / MRR / nDCG / 注入命中率 / P95 / 成本 / 失败降级，§10 DEFERRED BY EVIDENCE）；RRF 输入排名来自 dense final 与 sparse final，**不直接比较 cosine/BM25/hotness 原始数值**；两条 lane 的 hotness 为长期保留默认组成。

## 输入

- 上游 change 产出：无（独立根，D6）
- roadmap 冻结决策：§4.4 冻结目标（default engine 回溯基线 + 实验开关）、§4.1 现状（SQLite FTS5 BM25 / PG 仅 summary ILIKE / 无 jieba/pg_search/独立 reranker）、§10 DEFERRED BY EVIDENCE（Retrieval 增强开关依据离线评测与 Pilot 消融结果）
- 现有代码锚点：`plugins/default_memory/engine.py`（DefaultMemoryEngine）、`memory2/query_rewriter.py`（外层检索管线）、`schema/`（PG schema）
- 依赖前置：无

## 输出

- 代码：jieba 接入 + ParadeDB pg_search 索引/迁移 + BM25 分数归一化 + hotness 融合 + RRF 配置 + 三实验开关（HyDE/rewrite/reranker）
- 评测：离线评测脚本 + 评测数据集（Recall@k/MRR/nDCG/注入命中率/P95/成本/失败降级）、消融矩阵脚本（A/B/C/D）
- 契约 fixture：score 字段分离 fixture（semantic/BM25 raw/normalized/hotness/dense final/sparse final/rrf score 逐一可断言）
- 测试/证据：score 字段断言测试、默认 A 基线可复现评测产出、默认路径测试（default-only 不隐式调用旧 answer HyDE）、RRF 输入断言、diff 范围检查、消融脚本运行

## 验收标准

- [ ] sparse lane 分数归一化后与 hotness 融合；保留 semantic / BM25 raw / normalized / hotness / dense final / sparse final / rrf score 分离（不混为一个 score） — 验证：score 字段断言
- [ ] 默认 A 基线可复现：Recall@k / MRR / nDCG / 注入命中率 / P95 / 成本 / 失败降级 — 验证：评测脚本产出 + `openspec/evidence/` 存档
- [ ] HyDE / rewrite / reranker 三开关默认关闭且不进入默认路径（default-only 不隐式调用旧 answer HyDE） — 验证：默认路径测试
- [ ] RRF 输入排名来自 dense final 与 sparse final，不直接比较 cosine/BM25/hotness 原始数值 — 验证：RRF 输入断言
- [ ] 不阻塞 C4/C5：无 WebChat/auth 变更（独立质量能力边界） — 验证：diff 范围检查
- [ ] 消融矩阵 A/B/C/D 可运行；所有实验保留 dense+BM25 双 lane hotness — 验证：消融脚本运行
- [ ] 本 task 不触碰 WebChat/auth/tool/RuntimeSnapshot — 验证：PR diff 范围检查

> 判定「真正完成」而非「执行过」：默认基线的评测数值可复现（脚本 + 原始输出存档）；「开关默认关闭」和「RRF 输入不混原始数值」是负向断言。

## 独立性边界（不与其他任务重复）

- 本任务拥有：召回链路改造、评测脚本/数据集、三实验开关、score 字段分离
- 本任务不触碰：WebChat/auth/tool/RuntimeSnapshot（D6 独立）；C14 的 engine 选择/binding 表（只提供 default engine 改造实现可被 C14 消费）
- 共享 seam 协议：D6 独立——不阻塞 C4/C5；C14 **不强依赖**本任务（D7 不要求先完成 BM25/ParadeDB/jieba/reranker migration），但 default engine 实现可消费本任务改造（§6.1 retrieval seam，可选）

## 依赖

- **左依赖（必须先完成）**：无（独立根）
- **右依赖（本任务前置于）**：无（不阻塞 C4/C5；C14 也不依赖它）
- **可并行**：C1 / C3 / C4 / C5 / C12

## 风险与需冻结决策

- §10 DEFERRED BY EVIDENCE「Retrieval 增强开关」：BM25/hotness/RRF 建基线；HyDE/rewrite/reranker 默认关闭，依据离线评测和 Pilot 消融结果决定是否开启。
- §4.4：hotness 为两条 lane 长期保留默认组成；实验不得移除。
- 风险：把不同数量纲的 score 混进 RRF → 验收第 4 条强制 RRF 输入为排名；默认路径隐式调用旧 answer HyDE → 验收第 3 条以测试阻断；评测不可复现 → 评测脚本 + 结果存档强制化。