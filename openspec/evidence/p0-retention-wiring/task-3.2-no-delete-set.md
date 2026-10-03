# task-3.2 / 3.3 — 不可删除集（显式守卫）

日期：2026-10-03

## 不可删除集（ADR-2）与其守卫方式

不可删除集不是"注释约定"，而是**结构上不在任何删除清单**：sweeper 只对四个实体的
仓储方法发出删除调用（`webchat_replay_frames` / `tool_audit_events` /
`admin_audit_events` / `work_attempts`）+ 凭据摘要 UPDATE；其余实体没有任何删除代码路径。

| 实体 | 归属 | 守卫 |
| --- | --- | --- |
| `outbound_delivery_intents`（含 `dead_letter`） | 不删 | 无删除代码；redrive/ignore 是人工处置（DeliveryRepository） |
| `canonical_messages` / `canonical_conversations` / `inbox_records` / `turns` | 不删（账号生命周期议题） | 无删除代码 |
| `attachments` | C6 自有生命周期（`retention_deadline`） | 无删除代码（避免两条腿删同一实体） |
| `background_work_items`（含非终态） | 不删（lease/recovery 语义） | work_attempts 删除只动审计流行，FK `fk_work_attempts_work_item_id`（RESTRICT）本身也阻止孤儿化 |
| 活跃凭据（未撤销/未过期） | 不触碰 | 清除谓词要求 revoked ∧ expired ∧ 超宽限三者同时成立 |

## 测试证据

`tests/retention/test_pg_sweeps.py::test_no_delete_set_survives_full_round`：
经真实 ingress 造 canonical message + inbox + turn（dedupe 同事务）+ 远超任何窗口的
死信投递意图（400 天前），全部 backdate 后跑**完整一轮**——五表行数逐表断言不变，
且报告中不出现 `outbound_delivery_intents` 目标。
（spec「死信投递意图不被删除」「业务消息不在保留期执行范围内」）
