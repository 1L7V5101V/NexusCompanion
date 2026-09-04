# Task-02 — durable control plane：ingress/inbox + turn/work + outbox/delivery（durable-control-plane）

> 编号对应 PILOT_ROADMAP §5.9.10 第 2 项。状态标记复用 §8。

## 元数据

- **所属阶段**：主要里程碑 = P0.5；P-1 产出事务边界 design；P1 公网 durable source of truth 收口
- **§5.9 引用**：§5.9.6（durable state 与 restart recovery）、§5.9.9（inbox/turn/tool/work/outbox/delivery 实体）、§5.9.11（ingress/canonical message/outbox/delivery transaction）、§10 DECIDED（Ingress/outbox/delivery）
- **§6 出口条件引用**：P0.5 出口「durable inbox/acceptance + final/outbox + delivery ack 状态机」「模型生成完成与 channel sent/failed 语义分离」；P1 出口（公网 durable source of truth）
- **状态**：planned

## 目标

按 §5.9.11 落地三个明确事务边界：**入站接受事务**（inbox/dedupe + canonical user message + queued turn/work 原子提交）、**执行完成事务**（final assistant message + turn terminal + outbox intent 原子提交）、**独立 delivery ack**（delivery worker 单独记录 `pending/attempting/sent/failed/dead_letter`）；at-least-once + 稳定幂等键；durable 承诺范围 = final message/turn/tool 终态，**delta 不承诺**；「模型生成完成 ≠ channel 已送达」可观测、可补拉/可重投。

## 输入

- 上游 change 产出：C1 输出（canonical 表 + per-conversation sequence + identity resolver）
- roadmap 冻结决策：§5.9.11 三事务边界与幂等键（Telegram source message id / WebChat `client_message_id` 为强制幂等键）；§5.9.6 durable/recovery；§5.9.9 实体；§10 DECIDED Ingress/outbox/delivery
- 现有代码锚点：`bootstrap/passive_worker.py`（Passive lane / MessageBus）、`agent/control/runtime.py`（turn control）、`bus/queue.py`（进程内队列）
- 依赖前置：C1（canonical 表 + sequence）

## 输出

- DB schema：`inbox_records` / `message_deduplication_keys` / `turns` / `tool_calls` / `background_work_items` / `outbound_delivery_intents` / `delivery_attempts` 表 + 迁移；repository 事务封装
- 代码：delivery worker（独立记录状态/attempt/provider receipt/error/时间戳）；幂等键处理；`complete_inbound` 等价状态语义
- 测试/证据：事务回滚测试（无半写入）、状态流转测试、重启重放测试、幂等重复注入测试、指标/日志分离检查
- 契约 fixture：幂等键契约（WebChat `client_message_id` / Telegram source id 双键去重）

## 验收标准

- [ ] 入站接受事务原子性：inbox + dedupe + canonical user message + queued turn/work 同提交/同回滚 — 验证：事务回滚测试断言无半写入
- [ ] 执行完成事务原子性：final assistant message + turn terminal + outbox intent 同提交 — 验证：事务测试
- [ ] delivery 状态机 `pending/attempting/sent/failed/dead_letter` + attempt + provider receipt — 验证：状态流转测试覆盖全路径
- [ ] `sent` 仅由 channel/provider ack 或明确成功结果推进 — 验证：无 ack 不进 sent 的负向测试
- [ ] 重启后只重试未确认的 intent，不重新生成 assistant final message — 验证：重启重放测试断言无重复 final
- [ ] 重复注入测试：同 `client_message_id`（WebChat）/ 同 source identity + source message id（Telegram）不产生第二条 — 验证：幂等测试双键覆盖
- [ ] 「模型完成 ≠ 已送达」可观测：delivery worker 独立记录与 final message 分离；用户可见 delivery failure 可从 canonical final message 补拉或管理员重投 — 验证：指标/日志分离断言 + dead-letter 重投测试
- [ ] 本 task 不触碰 canonical identity 映射表（C1）与 admission 调度策略（C3） — 验证：PR diff 范围检查

> 判定「真正完成」而非「执行过」：每条均需可复现测试证据（`openspec/evidence/` 脚本 + 原始输出），特别是「重启重放不重复」「无 ack 不进 sent」两条负向语义。

## 独立性边界（不与其他任务重复）

- 本任务拥有：`inbox_records` / `message_deduplication_keys` / `turns` / `tool_calls` / `background_work_items` / `outbound_delivery_intents` / `delivery_attempts` 表与三事务边界
- 本任务不触碰：canonical identity 映射表（C1）、admission lane/overload 策略（C3）、WebChat 协议本体与前端（C4）、Telegram binding 表（C10）
- 共享 seam 协议：借 C1 的 canonical conversation + sequence；为 C4（E1）提供 durable inbox/outbox；为 P1GATE（D2）提供公网 durable 承诺；为 C12（E10）提供 work/turn/tool/delivery id

## 依赖

- **左依赖（必须先完成）**：C1（canonical 表 + sequence）
- **右依赖（本任务前置于）**：C4（E1：dev WebChat 闭环需要 durable inbox/outbox）、C10（E7：跨端同步需 durable inbox + canonical message stream）、P1GATE（D2：公网 delivery/recovery 承诺）
- **可并行**：C3（admission 与 durable 表 schema 不同表，可并行）

## 风险与需冻结决策

- §10 OPEN FOR P-1 SPEC「Exact schema/DDL」：inbox/turn/tool/work/outbox/delivery 精确字段在 P-1 design/spec 冻结。
- §10 DECIDED「Ingress/outbox/delivery」：三事务边界不可合并；`sent` 必须由 ack 推进；delta 不进入恢复承诺。
- 风险：重启后「未知 delivery 状态」-> 保留 `pending` 重试而非假定 `sent`（§5.9.11）；防止把「生成完成」与「已送达」混在一起（验收第 7 条专门阻断）。