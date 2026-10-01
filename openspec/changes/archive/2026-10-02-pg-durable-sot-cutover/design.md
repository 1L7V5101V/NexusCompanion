# pg-durable-sot-cutover — design

## Context

已验证的既有接缝（全部已归档、有 evidence）：C1 canonical 表与序号分配（`bootstrap/db/repository/canonical_repo.py`，迁移 `e2b4d6f8a0c2`/`c4d8f2a6e9b3`）；C2 控制面事务与状态机（`bootstrap/db/repository/control_plane_repo.py`：`IngressRepository.accept_inbound` / `TurnControlRepository.complete_turn_with_delivery` / `DeliveryRepository` lease 认领，迁移 `f3c8a9d2e7b4`）+ `OutboundDeliveryWorker`（`bootstrap/delivery_worker.py`，冻结参数 lease 60s/heartbeat 20s/5 次/`1m–6h` 退避）；C3 admission（overload 先于 durable acceptance 的语义已由 C4 冻结）；C15 work queue telemetry（`bootstrap/work_queue_telemetry.py`）= E10 记录点既有模式。

当前活跃 WebChat 路径（本 change 要替换的）：WS `send` → `web_chat_channel._handle_send` → 进程内 `_accepted_frames`/`_deduper` 幂等 → `publish_inbound` → C3 admission → agent loop → `session_manager.append_messages`（session_key 存储）→ `_on_outbound` 直接广播 `turn_completed`。`_ReplayBuffer` 进程内有界（500）。控制面事务与 delivery worker 目前零活跃调用方（仅 tests + 演练）。Pilot 从空历史开始（§5.9.2 冻结决策），无存量消息迁移。

## Goals / Non-Goals

- **Goals**：WebChat 入站/执行完成/投递三条事务边界落到 C2 控制面；canonical 流成为会话内容 SOT；重放与幂等重启存续；重启对账；E10 三个记录点承接。
- **Non-Goals**：见 proposal Non-Goals（Telegram/C6/C11/C9/C14、SQLite dev 补齐、prompt 直读 canonical、公网运维动作、C12 §8.4 执行）。

## Decisions

### ADR-1 接线位置：channel 侧调用 `accept_inbound`，MessageBus 保持不变

`_handle_send` 在 overload 检查后同步 await `accept_inbound`（PG T1），提交成功才以 durable seq 盖 `message.accepted`；进程内 `_accepted_frames`/`_deduper` 降级为缓存加速（权威判定 = 数据库唯一约束 + 重复返回既有身份）。
**实现细化（task 2.1/2.2 回填）**：序列 = `inbound_full()` 预检（overload 拒绝在 T1 前、不消耗幂等键）→ T1 → `publish_inbound_wait`（T1 后队列满载**界内等待槽位**而非拒绝——此刻拒绝只会产生孤儿 turn；超时按 `turn.failed`（原因 `overload_enqueue_timeout`）durable 收束）。T1 失败的错误帧复用冻结的 `overload` 码（可重发语义；真实原因服务端日志可见），零协议改动。网关以 Protocol 注入通道（`infra.channels.web_chat_channel.DurableSendGateway`，bootstrap `webchat_durable.py` 实现，ADR-6 分支 = identity 三元组为合法 UUID ∧ storage.backend=postgres）。
**备选**：把 MessageBus 消费端整体改造为 durable turn 执行器（从 queued turn 拉起执行）——拒绝：改造面横跨 admission/agent loop，Pilot 交互延迟与回归风险不可控；§5.9.10 允许接线型切换，全量总线 durable 化留待多副本阶段（SCALING_ROADMAP C3 范畴）。

### ADR-2 执行模式：bus 执行 + 控制面状态跟随，启动对账收束中断 turn

turn 继续由既有 bus/admission/agent loop 执行（interactive lane 不变）；loop 侧在执行起点把控制面 turn 置 `in_progress`，成功终点包 `complete_turn_with_delivery`（T2）。
**备选**：由 C15 work queue worker 消费 queued turn 执行 interactive turn——拒绝：work queue 是 background/maintenance 语义（§6 flow 分类），interactive 延迟 SLO 走 admission lane；queued turn 行在本设计中是 T1 的 durable 意图记录 + 对账依据，不是执行队列。
中断收束：启动扫描非终态 turn → failed（原因 `restart_reconciled`），不重新生成、不产 intent——与 `durable-control-plane` 规格的重启重放语义一致（重放只针对 delivery intent）。

### ADR-3 durable 重放：独立重放帧记录表，随 T1/T2 同事务写入

新增 per-conversation 重放帧记录（迁移新表，形如 `webchat_replay_frames(conversation_id, seq, frame_json, created_at)`，`(conversation_id, seq)` 唯一）：T1 提交时写 accepted 帧，T2（或失败终态收束）提交时写 `turn.completed`/`turn.failed` 帧；wire seq 由该表在事务内分配（per-conversation 单调）。补拉 = 按游标 SELECT。
**备选**：复用 canonical message seq 作 wire seq（accepted=user msg seq、completed=final msg seq）——拒绝：`turn.failed` 无 canonical 行，需在 turn 行加 seq 列且补拉要跨表 union 重建帧 JSON，帧格式演进时脆弱；独立表是重放投影（内容=已发给客户端的帧，无新增泄露面），canonical 仍是内容 SOT，两表职责清晰。retention：帧记录归 C12 §8.4 清理范围，本 change 在 backup manifest 登记条目（表加入 §5.9.12 workspace/PG manifest 校验器 fixture 所列范围由任务 6.2 核对）。
**实现细化（task 2.1/2.2 回填）**：seq 由独立计数器表 `webchat_replay_counters`（per-conversation 行，`UPDATE..RETURNING`，与 canonical next_sequence 同模式）分配，从 1 起与 legacy 进程内 buffer 对齐；帧表增 `message_id`/`turn_id` 软引用列——重复注入按 conversation+message 回查**原 accepted 帧**逐字重放（同 seq，重启存续），不按 canonical sequence 兜底；delta/tool 帧由 repo 白名单 + CHECK 约束双重拒绝入表。

### ADR-4 delivery 语义：WS 写成功 = channel ack；无连接按失败重试

投递适配器查该 conversation 在线连接逐个 `send_json` final 帧；≥1 连接写成功 → 返回 receipt 推进 `sent`；零连接/全部写失败 → 抛错走 attempt 退避。断线客户端以补拉/REST 重建补齐（at-least-once，前端按 `turn_id` 幂等渲染——既有行为）。
**备选**：引入客户端 delivery ack 帧改 C4 协议——拒绝：帧契约冻结，Pilot（10–30 受邀账号）无强需求，§5.9.11 允许"明确成功结果"作为 `sent` 依据。
延迟权衡：worker 轮询 1s 会让 final 晚于 delta ~1s，可能触 SLO（首帧 P95 ≤ 上游首 token + 1s）；缓解 = T2 提交后事件唤醒 worker（`asyncio.Event`/队列 notify），轮询退为兜底。实现细节归任务 4.1，不升格为决策。
`_on_outbound` 拆分：final/failed 帧改由 delivery worker/终态收束路径发出；delta/tool 帧维持 EventBus 即时广播。

### ADR-5 派生视图：session view 同步投影 + 启动对账修复（§5.9.12 例外声明）

T2 提交后同步把 user/final 消息投影进 SessionManager（`append_messages`），prompt 组装与 dashboard 不改读路径。投影写失败不阻断（记录 + 对账修复）；启动对账以 canonical 流为准补齐派生视图。
**§5.9.12 例外声明**：多租户 PG 模式下 session store 保留为**可重建派生物**（非 canonical state）；prompt 组装继续消费它属于性能与最小改造取舍，canonical 流为唯一权威，分歧一律以 canonical 修复。
**备选**：prompt 组装直读 canonical 流——拒绝：agent loop 内部重构面大（`get_or_create`/`peek_next_message_id`/proactive 历史装配等），归后续 change。

### ADR-6 dev/SQLite 回退：仅 PG 多租户模式启用 durable 接线

控制面 repo 为 async PG（`async_sessionmaker`）；不为 SQLite 实现 control-plane adapter。`agent.dev_mode` 且无 PG → 现行为保持（in-proc replay/dedupe），装配层按存储模式分叉，且 dev 回退路径显式标注 dev-only（延续 C4 三层门禁）。规格适用范围已在 delta 头部声明。

### ADR-7 E10 记录点：扩展 telemetry 模式，fixture 不动

新增 turn/tool_call/delivery 记录点模块，复用 `work_queue_telemetry.py` 三个既有机制：fixture 白名单构造（`ALLOWED_EVENT_FIELDS` 模式，字段超出即丢弃并报错）、自由文本 `redact_text`、metric label `validate_label_names` fail-fast；`tests/fixtures/observability_event_schema.json` 与 `metric_label_policy.json` 不新增字段——若实现发现 fixture 字段不足以表达某事件，回 design 补决策（fixture 是 C12 契约单一来源，不允许顺手扩）。tool_call 侧与 C7 `tool_audit_events`（审计流）并存：审计 = 逐调用明细，E10 = 生命周期指标事件，边界同 C12 §7.1。

## Risks / Trade-offs

- [双写（canonical + session view）漂移] → 投影失败记日志不阻断 + 启动对账以 canonical 修复 + 派生视图可全量重建（PG 集成测试覆盖）。
- [delivery 唤醒缺失导致 final 延迟 ~1s] → T2 后事件唤醒 worker；SLO 压测归 P0 后期基线报告（C12 §8.5），本 change 只保证语义。
- [accept_inbound 同步 PG 写增加入站延迟] → 本地 PG 单事务毫秒级，入站 P95 250ms 预算内；C0 负载工具可复测。
- [重放帧表膨胀] → 帧记录含内容（已发给客户端的帧），体积与消息量同阶；retention 归 C12 §8.4，本 change 登记 manifest 条目并在 design 留接口（按 conversation 清理函数）。
- [多连接重复投递]（delivery worker 补发 + 重连补拉 + REST 重建三源并存）→ 前端按 `turn_id`/`client_message_id` 幂等渲染为既有行为，PG e2e 验证无重复入库（帧级重复允许、消息级不允许）。
- [回滚] → Pilot 空历史，revert commit 即回滚；表保留无副作用。切换前在 scratch 库完成迁移演练（任务 1.2）。

## Migration Plan

1. alembic 迁移（重放帧记录表）在 scratch 库 `upgrade head` → `downgrade base` → `upgrade head` 全链验证（evidence）。
2. 实现 + PG 集成测试全绿后合入 main；多租户 PG 模式自动生效（无存量数据）；dev 无 PG 模式行为不变（ADR-6）。
3. 部署到服务器（既有 git clone 部署流程）后跑部署后 canary（模式同 webchat-auth-wiring 的 `deploy-e2e.txt`）。
4. 回滚 = revert merge commit；无数据迁移/回填需求（空历史决策，§5.9.2）。

## Open Questions

- 无。投递唤醒机制、帧表精确 DDL 与索引属实现细节，归 tasks 1.2/4.1；若任务期发现 fixture 字段不足或 `accept_inbound` 签名缺口，按 ADR-7/任务 1.1 回 design 补决策。
