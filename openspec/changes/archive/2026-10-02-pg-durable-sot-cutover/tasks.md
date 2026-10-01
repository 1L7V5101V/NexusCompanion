# pg-durable-sot-cutover — 实施任务

> 依据 design.md ADR-1..7 与 specs/（webchat-durable-storage 新增 + webchat-protocol-dev-loop 修改）；证据统一落 `openspec/evidence/pg-durable-sot-cutover/`。
> 管理闭环：任务 checkbox → `openspec status` → evidence → 仅在有 evidence 时更新 `PILOT_ROADMAP_PROJECT_CHECKLIST` / `SCALING_ROADMAP_PROJECT_CHECKLIST` 状态。
> 约定：实现默认在 main 直接分任务提交（同 C7/C8 模式）；PG 集成测试跑 `NEXUS_REQUIRE_PG=1`（本地 Docker PG 5433）；scratch 库做迁移演练。

## 1. 前置核对与迁移

- [x] 1.1 **控制面/canonical API 对账**：程序化核对 `accept_inbound`/`complete_turn_with_delivery`/`DeliveryRepository`/`canonical_repo` 的签名与参数覆盖本 change 需求面（双键去重、WebChat 键类型、queued turn 可选后台项、turn 置 `in_progress` 路径、重复注入返回既有身份字段）；缺口逐项回 design 补决策。验证：对账表入 evidence，未决项 0
  → 完成（2026-09-29）：对账表 [task-1.1-api-reconciliation.md](../../evidence/pg-durable-sot-cutover/task-1.1-api-reconciliation.md)——7 项已覆盖零改动消费（accept_inbound WebChat 键+重复重放字段全量返回、transition_turn CAS 状态机、T2、DeliveryRepository 全链、canonical fetch_messages/latest_sequence、auth 路径 conversation_id=canonical UUID）；4 个缺口均无需改 design：G1 列非终态 turn 方法（task 5.3）、G2 重放帧表（task 1.2）、G3 pg_turn_id 经 metadata 贯穿（task 3.x）、G4 durable 分支=UUID 身份∧PG 可用（task 2.x）；未决项 0
- [x] 1.2 **重放帧记录表迁移**：alembic 迁移（per-conversation `(conversation_id, seq)` 唯一 + frame_json + created_at；事务内 seq 分配策略定案）。验证：scratch 库 `upgrade head` → `downgrade base` → `upgrade head` 全链通过，结果入 evidence `task-1.2-migration.txt`
  → 完成（2026-09-29）：迁移 `d8e4f2b6a9c1`（down=b3f7a1c5d9e2）= `webchat_replay_counters`（per-conversation 计数器，T1/T2 事务内 `UPDATE..RETURNING` 取号，同 canonical next_sequence 模式）+ `webchat_replay_frames`（帧 CHECK 限三种 replayable 类型 + `(conversation_id, seq)` 唯一 + tenant 索引）+ ORM 双模型；scratch `nexus_sottest`（便携 PG 5432，psycopg3 方言 + vector/pg_trgm 扩展预建）三段演练全绿，evidence `task-1.2-migration.txt`
- [x] 1.3 **spec 交叉核对**：本 change delta 与 `durable-control-plane`/`canonical-identity`/`webchat-protocol-dev-loop` 现行规格逐条对读，确认零冲突（尤其重放窗口、overload 时序、`sent` 语义）。验证：对读结论入 evidence
  → 完成（2026-09-29）：evidence `task-1.3-spec-crosscheck.md`——DCP 9 条零冲突（「sent=明确成功结果」措辞直接覆盖 ADR-4；发现接线点：inbox 收束归 task 3.x）；canonical 契约零触碰（重放计数器独立于 canonical 序号）；**delta 扩展 2 条 MODIFIED**（慢消费者降级、连接生命周期——仅「重放 buffer」→「持久重放记录」承载措辞，行为不变，场景名保持原文，validate 通过）

## 2. durable 接受接线（ADR-1）

- [x] 2.1 `_handle_send` 接 `accept_inbound`：overload 检查后同步 T1，提交后才发 `message.accepted`（durable seq）；事务失败回结构化错误帧且不缓存幂等。验证：新增单测——提交失败零写入可重发；accepted 帧.seq = 持久层 seq
  → 完成（2026-09-29）：repo 扩展（`accept_inbound(replay_frame=)` 同事务写 accepted 帧 + `_record_replay_frame` 计数器取号；`transition_turn`/`complete_turn_with_delivery` 同参接 turn.failed/completed 帧）+ `bootstrap/webchat_durable.py` 网关（预检→T1→`publish_inbound_wait` 界内入队，超时 turn.failed durable 收束）+ 通道 durable 分支（`DurableSendGateway` Protocol 注入，app.py 装配 + 停机释放）。ADR-1 实现细化已回填 design：T1 失败错误帧复用冻结 `overload` 码（零协议改动）。测试 5+5 全绿 + 回归 98 passed（evidence `task-2.x-durable-accept.txt`）
- [x] 2.2 幂等权威切换：重复 `client_message_id` 以数据库重复返回重放原 ack（同 seq）；`_accepted_frames`/`_deduper` 降级为缓存（命中路径保留、未命中回落 PG 查询）。验证：单测 + PG 集成——同 id 重发唯一消息行；重启进程后重发仍重放原 seq（对应 delta「重启后重复提交重放原 ack」场景）
  → 完成（2026-09-29）：重复注入回查**原 accepted 帧**逐字重放（帧表 `message_id` 软引用列确定性回查，同 wire seq；迁移 `d8e4f2b6a9c1` 就地修订并复跑三段演练通过）；进程内缓存仅 L1 加速（命中重放/未命中回落网关 PG 查询）。PG 集成 `test_duplicate_returns_original_frame_with_same_seq` + 通道清缓存重放测试锁定「重启后重复提交重放原 ack」场景（evidence 同上）

## 3. 执行与完成事务接线（ADR-2）

- [x] 3.1 turn 生命周期落控制面：执行起点置 `in_progress`（服务端派生 turn 身份贯穿）；失败/取消收束 `transition_turn` 记原因，不产 intent。验证：单测——失败 turn 终态有原因、delivery intent 表零行
  → 完成（2026-09-29，实现取更简形态）：取消独立 in_progress 步骤（无消费方，Pilot 从简）——终态 CAS expected=queued 直接收束；pg 身份经 `nexus_pg_*` metadata 贯穿（成功路径由 after_reasoning 的 `outbound_metadata={**msg.metadata}` 整体透传零改动；abort/错误路径经 `_control_outbound`/loop catch-all 显式富化 + `nexus_error`/`nexus_fail_reason` 标记）。finisher 镜像管线持久化事实：无 TurnCommitted 的出站（abort/provider_error）= 失败终态（session 与 canonical 均无 final，零漂移），不产 intent（PG 测试锁定 intent 零行）。**wire 行为变化**：错误回复从「completed 帧带错误文本」改为协议正确的 `turn.failed` 帧（C4 fixture 示例文案即此场景，前端已消费该帧类型）。测试 7+9 全绿 + 管线回归 308 passed（evidence `task-3.x-turn-lifecycle.txt`）
- [x] 3.2 成功完成包 `complete_turn_with_delivery`（T2）：final canonical message + completed 终态 + pending intent 同事务；T2 后同步投影 `session_manager.append_messages`（失败记日志不阻断，ADR-5）。验证：PG 集成——T2 原子性（中途失败全回滚）；投影内容与 canonical 一致
  → 完成（2026-09-29）：`WebchatDurableTurnFinisher` 订阅 outbound（app 装配先于通道，dispatch 顺序保证先收束后发帧）→ T2 同事务 final+completed+intent+重放帧 → `mark_inbox_processed`（task 1.3 登记的收束点）→ `nexus_replay_seq` 盖回。**投影修正**：session view 的 user/assistant 写入由既有管线 after_reasoning `_AppendMessagesModule` 承担（即 ADR-5 所述投影，无需新写）；T2 与投影的顺序差由启动对账以 canonical 兜底（5.3）。T2 原子性由 C2 既有测试 + 本组 finisher PG 测试（canonical final/intent/帧/inbox 四点断言）覆盖
- [x] 3.3 `_on_outbound` 拆分：final/failed 帧不再由 EventBus 直发（改由 T2/终态路径与 delivery worker），delta/tool 帧维持即时广播。验证：协议契约测试（`chat_protocol_frames.json`）全绿不变；帧序——delta 先于 final 的既有客户端预期不破坏
  → 完成（2026-09-29）：`_on_outbound` durable 分支 = finisher 已持久化并盖 `nexus_replay_seq` → 通道逐字下发同帧（`_broadcast(stamp=False)`，进程内 buffer 不再盖 seq）；legacy 分支行为不变（buffer 盖 seq）。final 帧的 delivery worker 重投语义归 4.x（在线连接去重策略在该任务定案）；delta/tool 帧维持 EventBus 即时广播不受影响。协议契约测试全绿（`test_web_chat_protocol_contract.py` 在回归集内 308 passed）

## 4. delivery worker 接线（ADR-4）

- [x] 4.1 WebChat 投递适配器 + worker 装配进应用生命周期：按 conversation 找在线连接逐个投递，≥1 写成功推进 `sent`（record_attempt_sent），零连接/写失败走退避；T2 提交后事件唤醒 worker（轮询兜底）。验证：单测——在线投递 sent、无连接重试、唤醒触发即时投递
  → 完成（2026-09-29）：`WebchatDeliveryAdapter`（帧取自重放帧表 `frame_for_message`，与在线广播逐字一致；零在线连接抛错按 spec 走重试；前端按 turn_id 幂等渲染故 at-least-once 重复投递安全）+ `WebchatDeliveryLoop`（finisher T2 唤醒 + 1s 轮询兜底）+ runtime `start_delivery`/`stop_delivery` 二阶段装配（解 channel↔gateway 构造环）；worker 参数保持 C2 冻结值。PG 3/3 全绿（evidence `task-4.x-delivery.txt`）
- [x] 4.2 `dead_letter` 与重试边界：按冻结参数（lease 60s/5 次/`1m–6h`）验证 attempt 链路收束，dead_letter 可查、final 消息不受影响。验证：PG 集成——退避参数下重试至 dead_letter 的状态轨迹（可注入缩短退避），结果入 evidence `task-4.2-delivery.txt`
  → 完成（2026-09-29）：验证并入 `tests/control_plane/test_webchat_delivery.py`（evidence 合并为 `task-4.x-delivery.txt`）：max_attempts=1 注入演练 → `dead_letter` 终态 + attempt_count=1 + last_error「无在线连接」可查、重放帧/final 不受影响；未到期 intent 不被认领（退避排程）；已 sent 不被二次认领。lease/接管/失去租约语义由 C2 既有测试继续守护

## 5. durable 重放、REST 重建与启动对账（ADR-3）

- [x] 5.1 重连补拉切持久层：`replay{after_seq}` 从重放帧表按序服务；窗口外/不可用回 `replay_required`；delta/tool 帧不入表。验证：协议契约测试补「重启后补拉」「游标超窗」场景（对应 delta 两 Scenario）
  → 完成（2026-09-29）：gateway 增 `hello_seq`/`replay_after`（窗口语义：游标超前/低于保留下沿/retention 清空 → replay_required）+ 协议接口扩展 + 通道 hello/replay durable 分支（dev 回退走 legacy buffer 不变）；delta/tool 帧由 repo 白名单 + CHECK 双重拒绝（task 2.1 已落）。PG 测试覆盖「重启后补拉」「游标超窗」两场景（evidence `task-5.x-replay-rest-reconcile.txt`）
- [x] 5.2 REST 重建读 canonical：`/api/chat/sessions/{key}/messages` 与重建路径以 canonical 流为权威（会话映射：canonical conversation ↔ session_key）；序号连续无重复。验证：PG 集成——重建结果与实时推送内容一致（对应 spec「重建结果与实时会话一致」场景）
  → 完成（2026-09-29）：chat_api 注入 `durable_runtime`，durable 分支（auth + `chat:*` 键）以 canonical 流为权威产出重建条目（seq/role/content/thinking/created_at，分页/排序对齐既有契约）；**tenant 由已认证 session 派生、URL key 只做归属匹配**——附带收口 durable 键的越权读取面（他人 `chat:{tenant}` → 404；非 chat:* 的 dashboard 会话聚合行为不变）。真实 AuthRuntime + provisioning 的 PG e2e 锁定（含跨账号 404）
- [x] 5.3 启动对账模块：非终态 turn 收束 failed（原因 `restart_reconciled`、不产 intent）；派生视图与 canonical 分歧以 canonical 修复；对账结果出日志/指标。验证：PG 集成——执行中重启（kill 模拟）→ 启动后 turn=failed、无 intent、重建不出现半截 final（对应 spec 启动对账两场景）
  → 完成（2026-09-29）：`reconcile_webchat_on_startup`（app 启动装配，best-effort 不阻断）：`list_non_terminal_turns` → failed（同事务 turn.failed 帧 + inbox 收束）+ 受影响会话 session view 按 canonical 全量重建（delete cascade + user/assistant 重放，双向分歧一并修复，§5.9.12）；摘要 warning 日志。二次运行幂等、失败终态零 intent 已由 PG 测试锁定；指标出线归 6.x E10 记录点

## 6. E10 记录点（ADR-7）

- [x] 6.1 turn/tool_call/delivery 记录点模块：fixture 白名单构造（字段超出丢弃并报错）+ `redact_text` + label 白名单 fail-fast；接线到 §3/§4 各收束点；记录点异常不阻断主流程。验证：契约测试——事件字段 ⊆ fixture 白名单（双向断言，模式同 `test_work_queue_telemetry.py::test_allowed_event_fields_equal_fixture`）；若发现 fixture 字段不足 → 停，回 design 补决策（不许顺手扩 fixture）
  → 完成（2026-09-29）：`bootstrap/webchat_telemetry.py` 三记录点（`turn_finished`/`tool_call_finished`/`delivery_finished` + 4 指标族）；接线 = finisher 终态（turn）+ `PgToolAuditSink` 投影（tool_call，C7 审计流同点）+ `OutboundDeliveryWorker.on_delivery_finished` 回调（delivery，sent/failed/dead_letter 三态）；词汇映射：status 落 fixture 冻结枚举、原始词汇（sent/succeeded/unknown/rejected/dead_letter）落无枚举约束的 `result`（fixture 无缺口，未触发回 design 条件）。契约测试 7 passed（evidence `task-6.x-e10-telemetry.txt`）
- [x] 6.2 C12 台账与 manifest 登记：`c12-observability-backup/tasks.md` §8.1 追加本 change 承接记录（turn/tool_call/delivery 三记录点落地，消除无 owner 滞留项）；重放帧表纳入 backup manifest 校验器 fixture/模板核对（有差异则补模板条目）。验证：C12 tasks 更新 + manifest fixture 核对结论入 evidence
  → 完成（2026-09-29）：C12 §8.1 已追加承接记录（记录点义务全部落地：work=C15、turn/tool_call/delivery=本 change；8.4 聚合 API 阻塞解除）；manifest 核对：重放帧表在控制面 PG 同库、由 pg-canonical 条目（base backup+WAL）覆盖，模板无需新增（retention 清理入口 `delete_frames_before` 已预留，执行归 C12 §8.4）

## 7. 验收与回归

- [x] 7.1 **PG 端到端**（`NEXUS_REQUIRE_PG=1`，真实 C5 provisioning 账号）：发消息 → accepted（durable seq）→ final 经 delivery worker 抵达 → 重启进程 → 重发同 id 重放原 ack + 补拉连续 + REST 重建一致 + delivery 意图不重生成。验证：`tests/pg_sot/`（或就近目录）e2e 全绿，输出入 evidence `task-7.1-pg-e2e.txt`
  → 完成（2026-09-29）：`tests/pg_sot/test_e2e_roundtrip.py` 全链路通过（真实 C5 provisioning + AuthRuntime，scratch nexus_sottest_e2e）：T1 接受 → T2 完成 → 在线帧/worker 投递/sent → 重启模拟（组件全重建）→ 同 id 重放原 ack 同 seq → 补拉 [accepted, completed] 连续 → REST 重建=canonical（含越权 404）→ 中断 turn 启动对账收束 failed（turn.failed 帧可见）。evidence `task-7.1-pg-e2e.txt`
- [x] 7.2 **全量回归与基线对齐**：`NEXUS_REQUIRE_PG=1 pytest -q -W error tests/` 全绿；`pyright --level error` 双配置（project + tests）对齐基线（既有错误数不新增）。验证：结果入 evidence `task-7.2-regression.txt`
  → 完成（2026-09-29）：**1768 passed / 0 failed**（8:02，NEXUS_REQUIRE_PG=1，含本 change 新增 18 测试；基线 1738）；pyright 双配置 38/57 均既有基线零新增（结果并入 evidence `task-7.1-pg-e2e.txt` 7.2 段）。首跑的 36 failed/1 failed 均为环境问题（共享 nexus 库 schema 过旧 + 5433 Docker PG 缺失），已修复环境后复跑全绿，详见 evidence 环境备注
- [x] 7.3 **管理闭环回填**：本 change tasks 全勾 + `openspec status` 收口；`PILOT_ROADMAP_PROJECT_CHECKLIST` 回填（P0.5「canonical stream/0-based seq」、P1「durable source of truth」两条目勾选带 evidence；current blocker 解除说明——公网开放存储前置完成，下一步为部署 canary 与运维放开决策）。验证：checklist diff 仅含 evidence 支撑的状态变更
  → 完成（2026-09-29）：checklist 回填（P0.5 canonical 条目勾选；P1 durable SOT 条目按实际覆盖注记——schedule durable 归 C11 不勾选；当前进度/current focus/next decision 刷新）；本 change 18/18 全勾，`openspec status` 4/4 artifacts complete

## 8. 生产激活前置：consolidation 链路 tenant 贯穿（2026-10-01 追加）

> 背景：服务器（backend=sqlite）曾因 memory context guard 积压拦截出现用户可见错误
>（"处理消息时出错"，2026-09-29/30）；诊断确认 consolidation 三条路径（guard/后台/手动）
> 写死 `get_or_create(DEFAULT_TENANT, ...)`——sqlite 单 store 下碰巧无害，**backend=postgres
> 激活后 per-tenant 视图下默认租户查 `chat:*` 会话拿到空会话，归档必 skip → guard 持续拦
> turn**。本节为存储切换生产激活（任务 8.3）的硬前置。

- [x] 8.1 **tenant 贯穿修复**：`trigger_memory_consolidation` 增 `tenant_id` 参数（core.py，get_or_create 用真实租户）；guard 经 `state.tenant_id` 透传（before_turn.py + `MemoryConsolidator` 协议）；后台链路 `on_turn_committed` 捕获 `event.tenant_id` → 队列/续跑/`_get_session(tenant_id, key)` 双参（markdown.py + `MemoryLifecycleBindRequest` 接口）；tools.py 绑定 lambda 去 DEFAULT_TENANT 回退；dashboard 手动端点传 `_resolve_request_tenant(tenant_id)`（owner tenant 兜底语义不变）。验证：lifecycle/dashboard 测试全绿 + tenant 透传断言
  → 完成（2026-10-01）：五文件贯穿（core/before_turn/markdown/tools/dashboard）；既有 lifecycle 假 consolidator 补 kwarg 并加 `tenants == [msg.tenant_id]` 透传断言 + bind 接口双参断言；dashboard 假件同步。相关 57 passed
- [x] 8.2 **全量回归 + pyright 基线**：`NEXUS_REQUIRE_PG=1 pytest -q -W error tests/` 全绿；pyright 双配置对齐基线。验证：结果入 evidence
  → 完成（2026-10-01）：1768+ passed / 0 failed（见 evidence `task-8.x-tenant-activation.txt`）；pyright project/tests = 38/57 基线零新增
- [x] 8.3 **生产激活 + durable canary**：服务器部署含修复的镜像，config `[storage] backend="postgres"`（用户已拍板继续多租户改造），重启后验证 durable 网关装配 + e2e canary（exchange→握手→hello→收发）+ durable 落行核验（turns/outbox/replay frames 进 PG）+ consolidation 在 PG 视图下真实归档。验证：canary 输出 + PG 行数证据入 evidence
  → 完成（2026-10-01）：见 evidence `task-8.x-tenant-activation.txt`（部署 + 激活 + canary + PG 落行 + consolidation 归档实录）
