## C13 memory retrieval A 基线
| 指标 | 值 || --- | --- || recall@1 | 0.3133 |
| recall@3 | 0.3433 |
| recall@5 | 0.3733 |
| mrr | 1.0000 |
| ndcg@1 | 1.0000 |
| ndcg@3 | 0.4810 |
| ndcg@5 | 0.4807 |
| injection_hit_rate | 1.0000 |
| p95_latency_s | 0.0106 |
| avg_latency_s | 0.0071 |
| n_queries | 40 |
| retrieved_total | 105 |
| injected_total | 105 |
| gold_items_total | 132 |
| embed_calls | 40 |
| llm_calls | 0 |

## embed 失败退化（keyword-only）
| 指标 | 差额 || --- | --- || keyword_only | {'recall@1_delta': 0.04166666666666663, 'recall@5_delta': 0.0, 'mrr_delta': 0.0625} |
