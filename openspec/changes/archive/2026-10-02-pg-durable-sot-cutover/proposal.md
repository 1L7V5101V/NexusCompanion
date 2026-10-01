# pg-durable-sot-cutover — proposal

## Why

WebChat 公网开放的唯一前置 blocker 是存储切换（PILOT_ROADMAP_PROJECT_CHECKLIST current blocker，§5.9.6/§5.9.9）。当前 WebChat 消息路径的持久化面是 session store（`chat:{tenant}` session_key 键控，`session/store.py` / `postgres_session_store.py`）+ 进程内幂等缓存（`web_chat_channel.py` `_accepted_frames`）+ 进程内重放 buffer（`_ReplayBuffer`，注释自述"dev v0；P1 由 PG durable sequence 替换"）；而 C1 canonical message stream 与 C2 durable control plane（入站接受事务、执行完成事务、outbox delivery 状态机、lease worker）虽已 verified，却**没有任何活跃通道调用**（仅测试与演练引用）。多规范源并存：重启丢失幂等与 seq、恢复承诺缺失，不满足 §5.9.6 的 PostgreSQL durable source of truth，不得对受邀用户开放。

## What Changes

- **入站接受事务接线**：WebChat WS `send` 在通过 overload 门禁后调用 C2 `accept_inbound`（PG 单事务：dedupe 键〔账号+client_message_id〕+ canonical user message + inbox + queued turn），**事务提交后才**返回 `message_accepted`；幂等从进程内 dict 升级为数据库部分唯一索引，重启后存续。
- **执行完成事务接线**：turn 继续由既有 bus/admission/agent loop 执行（交互延迟优先），但生命周期落控制面：成功完成经 `complete_turn_with_delivery` 原子写 final assistant canonical message + turn 终态 + pending outbox intent；失败/取消只记终态、不创建 intent。
- **delivery worker 接线**：`OutboundDeliveryWorker` 装配进应用生命周期，WebChat 投递适配器把 final 帧写到该会话在线连接；`sent` 仅由 WS 发送成功推进（at-least-once），失败按冻结退避重试至 `dead_letter`；delta/tool 帧仍走 EventBus 即时广播（在线优化，不入恢复承诺）。
- **durable 重放**：replayable 帧（`message.accepted`/`turn.completed`/`turn.failed`）的 wire seq 与帧记录随 T1/T2 同事务持久化，重连补拉从 durable 源服务（重启存续）；窗口外仍回 `replay_required` 由 REST 从 canonical message 重建。C4 帧协议本身不变。
- **canonical 流成为会话内容 source of truth**：user/final assistant 消息以 canonical message stream（0-based per-conversation 序号，复用 canonical-identity 契约）为准；session store 在多租户 PG 模式降级为**可重建派生视图**（prompt 组装与 dashboard 继续消费它，由 durable 事务后的投影写入 + 启动对账保证可重建，§5.9.12 例外逐项声明于 design）。
- **重启对账**：启动时非终态 turn 对账为显式失败终态（不重新生成、不产生 intent）；未确认 delivery intent 按既有重启重放语义恢复重试。
- **C12 §8.1 余留承接（E10）**：`turn`/`tool_call`/`delivery` 三个记录点按 `tests/fixtures/observability_event_schema.json` 白名单构造落地（`work_queue_telemetry.py` 既有模式），消除该无 owner 滞留项。
- Pilot 从空历史开始：**无存量消息迁移**，切换即 cutover，回滚 = revert。

## Capabilities

### New Capabilities

- `webchat-durable-storage`: WebChat 通道的 durable 存储契约——durable 接受与重启存续幂等、canonical 流为会话内容 source of truth、执行完成事务与终态、delivery 状态机接线与 `sent` 语义、durable 重放与 REST 重建、重启对账、E10 记录点（内容边界与脱敏）。

### Modified Capabilities

- `webchat-protocol-dev-loop`: 「client_message_id 强制幂等」升级为 durable 承接（重启后重复提交仍重放首次 accepted、同 seq）；「按 last_sequence 游标的重连补拉」的 seq 分配与补拉源从进程内有界 buffer 改为 durable 持久层（重启存续），窗口外行为不变。帧协议字段/错误码/close code 不变。

## Non-Goals

- **Telegram/其他通道接线**：C10 范围；本 change 只切 WebChat（Telegram 私聊绑定未开工）。
- **Attachment/媒体生命周期**：C6 范围；上传/media 端点维持现状。
- **SQLite 单机 dev 路径退役或补齐**：`agent.dev_mode` 且无 PG 时保持现行为（in-proc replay/dedupe），仅限本地 dev（§5.9.12 dev compatibility 例外）；不为 SQLite 实现 control-plane adapter。
- **prompt 组装直读 canonical**：agent loop 内部继续消费 session view；直读优化归后续 change。
- **schedule（C11）/Persona（C9）/memory catalog（C14）durable 化**：各自独立 change。
- **公网开放本身**（Tunnel/DNS/监控等运维动作）与 **C3 P3 恢复演练全量、LLM 429 退避**：本 change 只解除存储 blocker。
- **总控台聚合 API / retention 接线**（C12 §8.4）：replay 帧记录的 retention 仅在本 change 登记 manifest/清理策略条目，执行归 C12 §8.4。

## Impact

- **接线面**：`infra/channels/web_chat_channel.py`（`_handle_send`/`_on_outbound`/replay）、`bootstrap/chat_api.py`（REST 重建读 canonical）、`bootstrap/channel_host.py`/`bootstrap/app.py`（装配 ingress/delivery worker 与启动对账）、`bootstrap/delivery_worker.py`（send callback 装配）。
- **消费面（按需小改）**：`bootstrap/db/repository/control_plane_repo.py`、`canonical_repo.py`；`session/manager.py`（投影写入入口）。
- **新增**：WebChat 投递适配器、启动对账模块、alembic 迁移（durable 重放帧记录表）。
- **遥测**：`bootstrap/` 下新增 turn/tool_call/delivery 记录点模块（fixture 白名单构造，模式同 `work_queue_telemetry.py`）。
- **前端**：零改动（帧协议冻结）；`tests/` 新增 PG 集成测试（`NEXUS_REQUIRE_PG=1`）与单测。
- **依赖**：无新外部依赖；PG 为多租户模式既有硬依赖。
