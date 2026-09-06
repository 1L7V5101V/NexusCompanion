# memory-retrieval-baseline 增量规格

## Purpose

定义 default engine 召回基线（raw query + dense semantic/hotness + BM25/hotness + RRF + top-k）的可机检契约：score 字段分离、RRF 输入排名来源、实验开关默认态、离线评测可复现与失败降级。

## ADDED Requirements

### Requirement: sparse lane 分数归一化后与 hotness 融合

BM25 lane 的分数 SHALL 先做 query-local 归一化（`bm25_normalized = bm25_raw / (bm25_raw + K)`），再与 hotness 融合为 sparse final score（`sparse_final = (1-αs)·bm25_normalized + αs·hotness`）。LIKE 保底命中 SHALL 以 term 命中率作为归一化基底且 `bm25_raw` 为 `None`。归一化 SHALL 单调且 query-local，不跨查询比较。

#### Scenario: BM25 命中的分数链

- **WHEN** keyword lane 经 BM25 返回命中
- **THEN** 每个命中携带 `bm25_raw`、`bm25_normalized`、`hotness`、`sparse_final`、`sparse_source` 字段，且 `bm25_normalized == bm25_raw / (bm25_raw + K)`、`sparse_final == (1-αs)·bm25_normalized + αs·hotness`

#### Scenario: LIKE 保底命中的分数链

- **WHEN** keyword lane 降级为 summary OR-LIKE 返回命中
- **THEN** 命中的 `bm25_raw` 为 `None`，`sparse_source` 为 `like_fallback`，`sparse_final` 由命中率基底与 hotness 融合得出

### Requirement: score 字段全程分离

检索链路 SHALL 分别保留 semantic、`bm25_raw`、`bm25_normalized`、hotness、dense final、sparse final、`rrf_score` 七类字段；SHALL NOT 将异构分数相加、互相覆盖或合并为单一 score。dense 命中的 dense final 与 `_score_debug.{semantic, hotness, final}` 契约保持不变。

#### Scenario: 七字段逐一可断言

- **WHEN** 对同一 query 执行双 lane 检索并融合
- **THEN** 融合产物中 dense 命中携带 `_score_debug` 与 `rrf_score`，keyword 命中携带 sparse 五字段与 `rrf_score`，任一字段缺失或异构相加即契约失败

### Requirement: RRF 输入排名只来自 lane final

RRF 的 dense 侧输入排名 SHALL 来自 dense final score，sparse 侧输入排名 SHALL 来自 sparse final score；SHALL NOT 直接比较 cosine、BM25 raw 或 hotness 的原始数值。融合后 SHALL 保留融合前原始 `rrf_score` 与每侧 rank 记录。

#### Scenario: raw 值序与 final 序相反时 RRF 随 final 序

- **WHEN** 构造两批候选使 raw 数值序与 lane final 序相反
- **THEN** RRF 排名与融合分随 lane final 序，而非 raw 数值序

#### Scenario: 异构原始数值不跨 lane 比较

- **WHEN** keyword 候选的 BM25 raw 值远大于 dense 候选的 cosine 值
- **THEN** RRF 融合分仅由各侧 rank 决定，BM25 raw 与 cosine 的数值差不影响融合分

### Requirement: 实验开关默认关闭且不进入默认路径

HyDE-style hypothesis、query rewrite、reranker 三个实验开关 SHALL 默认关闭；默认路径 SHALL NOT 因兼容旧逻辑隐式调用 HyDE（answer intent 关闭时 light provider 零调用）。开启 reranker 时，最终注入排序 SHALL 以 reranker 结果为准；reranker 失败 SHALL fail-open 回 RRF 顺序。

#### Scenario: 默认配置 answer intent 零 LLM 调用

- **WHEN** 以默认配置（三开关关闭）执行 answer intent 检索
- **THEN** light provider 调用次数为 0，`trace["hyde_hypotheses"]` 为空列表

#### Scenario: 开关显式开启恢复实验路径

- **WHEN** 显式设置 `hyde_enabled = true`
- **THEN** answer intent 恢复 hypothesis 生成并记录于 trace

### Requirement: hotness 为双 lane 长期保留默认组成

dense 与 BM25 两条 lane 的 hotness SHALL 作为长期保留的默认组成：dense 侧经 lane 内 α（默认 0）与 post-RRF β 乘性增强承载，sparse 侧经 `αs`（默认 > 0）融合承载；实验配置与消融矩阵 SHALL NOT 提供移除双 lane hotness 的默认选项。

#### Scenario: 消融实验保留 hotness

- **WHEN** 运行消融矩阵任一配置（A/B/C/D）
- **THEN** sparse 融合 αs 与 post-RRF β 保持启用，无「移除 hotness」变体

### Requirement: 离线评测可复现

default engine 召回基线 SHALL 提供离线评测：确定性数据集与确定性 embedder（无外部 API 依赖）下产出 Recall@k、MRR、nDCG@k、注入命中率、P95 延迟、成本计数与失败降级行为；默认 A 基线数值 SHALL 可由脚本重跑复现并存档 evidence。

#### Scenario: A 基线重跑数值一致

- **WHEN** 在同一版本代码上重跑默认 A 基线评测
- **THEN** 除计时类指标外，Recall@k、MRR、nDCG、注入命中率与成本计数与存档 evidence 一致

### Requirement: 检索失败降级保底

dense lane embedding 失败或向量检索失败时，检索 SHALL 降级到 keyword lane 保底并记录降级行为；评测 SHALL 量化失败降级下的召回退化。

#### Scenario: embed 失败时 keyword 保底

- **WHEN** 评测中注入 embed 异常
- **THEN** 检索仍经 keyword lane 返回结果，评测记录降级后的 Recall@k 与失败计数
