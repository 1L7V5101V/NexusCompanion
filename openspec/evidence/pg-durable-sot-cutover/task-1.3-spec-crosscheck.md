# task 1.3 spec 交叉核对

> 对读对象：本 change 两份 delta vs `openspec/specs/durable-control-plane/spec.md`（9 requirements）、`canonical-identity/spec.md`、`webchat-protocol-dev-loop/spec.md`（7 requirements）。结论：**零冲突；delta 扩展 2 条 MODIFIED（承载措辞对齐）**。

## durable-control-plane（被消费，零改动）

| requirement | 对读结论 |
|---|---|
| 入站接受事务原子性 / 幂等双键去重 | 本 change 的 T1 接线即其实现路径；WebChat 键 = 账号 + client_message_id 与 spec 措辞一致 |
| inbox 收束语义 | **接线点补充**：turn 终态收束后须调 `mark_inbox_processed`（accepted→processed，幂等）——归 task 3.x 实现 |
| 执行完成事务原子性 | `complete_turn_with_delivery` 逐条对应；失败终态不产 intent 与 transition_turn-only 路径一致 |
| delivery 状态机与 lease 认领 | 「`sent` 只能由 channel/provider 的成功确认（**或明确成功结果**）推进」——ADR-4 的 WS 写成功即"明确成功结果"，措辞直接覆盖，无冲突 |
| 重启重放只补投递不重新生成 final | 与 ADR-2 启动对账（中断 turn 收束 failed、不重新生成）互补不冲突：该条只管 intent 补投 |
| dead-letter 人工处置 / 模型完成与已送达可观测分离 / 查询租户隔离 | C2 已交付；本 change 新增 repo 方法（list_non_terminal_turns、重放帧读取）须保持 tenant_id 强制过滤（task 5.3 实现约束） |

## canonical-identity（被消费，零改动）

- 0-based 连续序号分配：T1/T2 的 canonical 取号逻辑不改（`accept_inbound`/`complete_turn_with_delivery` 内联实现，C2 已验证）。
- 重放帧表是**独立投影**，不消费 canonical 序号计数器（`webchat_replay_counters.next_seq` 独立取号），不触碰「规范消息序号连续」契约；canonical 消息行零新增字段。

## webchat-protocol-dev-loop（本 change 修改 4 条 requirement）

- 已 MODIFIED：client_message_id 幂等（durable 承接）、按 last_sequence 游标重连补拉（durable seq/补拉源）。
- **task 1.3 发现并补入 delta**：「慢消费者分级降级与 overload close」「连接生命周期与空闲回收」两条的场景文字含「重放 buffer」——durable 实现下该承载变为持久重放记录（task 1.2 表），行为契约不变（终态帧可补拉、不因队列满删除、断线不取消 turn）。已扩为 MODIFIED 并仅改承载措辞，场景名与 WHEN 保持原文（validate 通过）。
- 未触碰：帧协议契约与版本、服务端派生身份、dev-only 暴露门禁（hello 的 `latest_seq` 语义不变：连接建立时的当前最大 seq）。
