# task 1.1 控制面/canonical API 对账表

> 对账对象：`bootstrap/db/repository/control_plane_repo.py`、`canonical_repo.py`、`bootstrap/delivery_worker.py`、`bus/events.py`、`bootstrap/auth/identity.py`（2026-09-29 main）。
> 结论：**7 项已覆盖（零改动消费）、4 个缺口各有明确落点，未决项 0，design 无需变更**。

## 已覆盖（本 change 直接消费，零改动）

| # | 需求面 | 承接 API | 核对结论 |
|---|--------|----------|----------|
| 1 | WebChat 键 durable 去重（account+client_message_id） | `IngressRepository.accept_inbound`（L284-455） | 恰好一类幂等键校验 + 部分唯一索引 `on_conflict_do_nothing`；`content`/`metadata` 直写入 canonical user message |
| 2 | 重复注入重放原 ack | 同上 → `AcceptInboundResult(duplicate=True)`（L484-516） | 回查既有 dedupe key → inbox → canonical sequence → turn_id，**重放 accepted 所需的 sequence/turn_id/message_id 全量返回** |
| 3 | queued turn 落行 | `accept_inbound(create_turn=True)`（L429-439） | turn 行状态 `queued`，锚定 inbox_record_id；turn_id 随结果返回 |
| 4 | turn 生命周期 CAS | `TurnControlRepository.transition_turn`（L567-596） | `expected_status` CAS + error dict；turn 状态 CHECK（models L208）：`queued/in_progress/completed/interrupted/failed/cancelled`——覆盖 in_progress 起点、failed/cancelled 非终态收束 |
| 5 | T2 执行完成事务 | `TurnControlRepository.complete_turn_with_delivery`（L598-693） | final assistant message（canonical 取号）+ turn `completed`（引用 final_message_id）+ pending intent 单事务；`expected_status="in_progress"`；投递幂等键默认 `msg:<message_id>`；失败终态不产 intent ✓（intent 仅在此方法创建） |
| 6 | delivery 状态机 | `DeliveryRepository`（L834-1008+）+ `OutboundDeliveryWorker` | claim_batch（lease 认领）/heartbeat（DB 时钟同源）/record_attempt_sent（仅租约在握推进 sent）/record_attempt_failed（退避→dead_letter）；worker 泛型 `send_callback` 注入即用，`DeliveryEnvelope` 已含 message_id/turn_id/channel/target/payload |
| 7 | tenant→conversation 映射与 REST 重建 | `CanonicalIdentityRepository.get_conversation_by_tenant` + `CanonicalMessageRepository.fetch_messages(after_sequence, limit)` / `latest_sequence` | fetch_messages 按 sequence 升序 + after_sequence 游标 = REST 重建/补拉直接可用；latest_sequence fail-closed 语义适配 hello 游标；auth 路径 `WebChatIdentity.conversation_id` = canonical UUID（`bootstrap/auth/identity.py` L51） |

## 缺口（各有落点，无设计变更）

| # | 缺口 | 影响 | 落点 |
|---|------|------|------|
| G1 | 无「列非终态 turn」扫描方法（仅 get_turn 单查） | 启动对账需要按 `status IN (queued, in_progress, ...)` 扫描 | task 5.3：TurnControlRepository 增 `list_non_terminal_turns()`（design ADR-2 已声明启动扫描行为） |
| G2 | 重放帧记录表不存在 | durable 重放无落点 | task 1.2 alembic 迁移（design ADR-3） |
| G3 | 控制面 turn_id 贯穿：channel 接受后的 `pg_turn_id/inbox_id/sequence` 需传到执行完成点 | loop 的 `OutboundMessage.control_turn_id` 是 loop 内部 control turn id，与本行 id 是两个概念（不冲突、不合并） | task 3.x：经 `InboundMessage.metadata`（`nexus_pg_turn_id` 等保留键）携带至完成点接线 |
| G4 | durable 分支条件未编码 | dev 回退身份的 conversation_id=`"local"`（非 UUID）不可走 durable | task 2.x：分支 = identity.conversation_id 为合法 UUID ∧ PG 控制面可用；否则 legacy（ADR-6），fail-closed 不混跑 |

## 备注

- `accept_inbound` 的 `work_items` 可选参数本 change 不使用（interactive turn 不经 T1 挂后台工作项）。
- `TurnControlRepository.record_tool_call/finish_tool_call`（L707-762）供 task 6.1 E10 tool_call 记录点与 C7 `tool_calls` 行承接；E10 事件与 C7 `tool_audit_events` 审计流并存（design ADR-7）。
- delivery `target_chat_id` 承载 conversation 维度路由（WebChat 投递适配器按 conversation 找在线连接，chat_id 仅作日志/展示维度）——适配器实现（task 4.1）以 `tenant_id + conversation_id` 为准。
