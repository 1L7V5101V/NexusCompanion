# 已知丢失窗口记录（Known Lost Windows）

> P0 出口条件（task-03 / PILOT_ROADMAP §6）：「进程内 queue/task 已知丢失窗口有明确记录」。
> 本文档逐项登记重启时**不会恢复**的进程内状态、原因与恢复边界。
> §5.9.6「不丢消息」的验收范围是 canonical inbound/final message 与可观察终态，
> **不包括**内存中的 token delta、错过的 proactive tick 或旧 sleep timer。
> durable 控制面（inbox/turn/tool/work/outbox/delivery）归 C2 落地后，本表对应行
> 将从「丢失」迁移为「按恢复语义处置」。变更进程内队列/任务结构时必须同步本表
> （`tests/admission/test_lost_windows_doc.py` 断言覆盖项存在）。

| # | 进程内对象 | 重启行为 | 原因 | 恢复边界 / 处置 |
| --- | --- | --- | --- | --- |
| 1 | `bus/queue.py::MessageBus._inbound`（global interactive ready queue，容量 128） | 丢失 | `asyncio.Queue` 纯进程内存；durable inbox 归 C2 | 入站消息由 channel 层重试语义兜底（Telegram provider redelivery、WebChat 客户端 `client_message_id` 重发）；durable acceptance transaction 落地前，已出队未处理项不可恢复 |
| 2 | `bus/queue.py::MessageBus._outbound` + outbound 订阅分发 | 丢失 | 同上 | 出站 final message 在 turn 终态中有记录（turn audit），但不自动重发；durable outbox 归 C2 |
| 3 | `bootstrap/passive_worker.py` per-tenant lane 队列（per-tenant pending 16） | 丢失 | lane 队列是 `asyncio.Queue` 进程内存 | 未消费消息同 #1；执行中 turn 的 turn 记录为非终态，由启动恢复扫描标记 `cancelled`（`agent/admission/recovery.py::TurnAuditRecoverySource`） |
| 4 | `ConversationRuntime` 内存面：`_active_by_thread`、`_results`、`_history`、`_subscribers`、事件流 | 丢失 | 进程内存；turn 记录本体已持久化（turn audit） | 订阅断开；重连/重查走 `read_turn` 持久记录；非终态 turn 由恢复扫描收束为 `cancelled` |
| 5 | `core/memory/markdown.py::MarkdownMemoryMaintenance` per-session 维护意图（per-kind ≤1） | 丢失 | 维护意图是进程内 deque 槽位 | **可从 durable state 重算**：consolidation/refresh 的触发条件（pending 消息数、`last_consolidated`）持久在 session store，下一个 turn 提交或下轮维护扫描重新触发；意图丢失无数据损失 |
| 6 | `proactive_v2` Proactive tick / `MemoryOptimizerLoop` 下一轮 timer | 丢失（不补发） | §6.1 B：runtime scheduler tick 按当前时间重算下一次调度，不逐个补发错过的 tick | 启动后按状态重新计算下次 tick；错过的 tick 属 `intentionally skipped` 等价语义（recompute-on-next-tick） |
| 7 | `agent/tools/shell.py` 后台任务 registry（`background_task_id`） | 丢失 | 进程内 registry；子进程随进程组终止 | 重启后后台任务状态不可查询（capability 盘点 §6.1 C 已记录）；不自动重放 shell 命令（禁止无确认重放） |
| 8 | `infra/channels/web_chat_channel.py` 进程内重放 buffer + outbound 队列 | 丢失 | dev v0 重放 buffer 是进程内存；PG durable sequence 归 C1/C2 | 客户端重连后 `last_sequence` 超出 buffer → `replay_required` 不可满足时按协议错误处理；canonical final message 落库后由 C2 补拉机制兜底 |
| 9 | `RestartCoordinator` 观测状态、`InboundMessage.metadata` 临时标记、各 channel 内存 session 状态 | 丢失 | 进程内存 | 无业务数据损失；交付/订阅状态由对应 durable 机制（C2）或 channel 重试兜底 |

## 外部副作用 tool 的 unknown / compensation_required 边界

当前（C2 落地前）`tool_calls` durable 表不存在，重启时正在执行的外部副作用 tool
（`message_push`、MCP external-write、shell）**无法**做 outcome 查询，恢复扫描对
其所属 turn 统一处置为 `cancelled`，并在 recovery result detail 中注明
「tool outcome confirmation requires C2 tool_calls table」。此为已知缺口：

- 状态语义（`unknown` / `compensation_required`）已在 `agent/admission/recovery.py`
  冻结，C2 表落地后由对应 RecoverySource 产出；
- 在此之前，外部副作用 tool 的结果可能停留在「已执行但无终态记录」——
  该窗口随 C2 关闭（P1GATE 前置，D2）。
