# Design — c10-telegram-binding-sync

## Context

C1/C2/C4/C5 已归档，C10 需要的 seam 全部现成：

- `IngressRepository.accept_inbound`（`bootstrap/db/repository/control_plane_repo.py`）**已内建双幂等键**：Telegram source 三元组（`source_channel + source_identity_id + source_message_id`，部分唯一索引 `index_where=source_message_id IS NOT NULL`）与 WebChat `account_id + client_message_id`——T1 接受事务（dedupe + canonical user message + inbox + queued turn + replay frame）无需为 Telegram 改一行。
- `WebchatDurableTurnFinisher`（T2）/ `OutboundDeliveryWorker` + `DeliveryRepository`（delivery 状态机）通道无关；仅 `WebchatDurableTurnFinisher` 订阅单一 channel 名、`complete_turn_with_delivery` 的 `delivery_channel/target` 由调用方给定。
- `WebchatReplayRepository` 的重放帧表按 `(tenant_id, conversation_id)` 记账，`message.accepted` / `turn.completed` 均可重放；`WebChatChannel.deliver_frame(conversation_id, frame)` 是现成的按会话推送通道。
- `resolve_webchat_identity`（`bootstrap/auth/identity.py`）示范了「服务端查表派生三元组、fail-closed」；`AccessTokenModel` + `register_from_invite`（digest-only、行锁、条件消费、原子事务）是绑定码的现成模式。
- 旧单体 `TelegramChannel`（`infra/channels/telegram_channel.py`）走 allowlist + `tenant_id_for_channel` + 直接 `publish_inbound`，出站经 `_on_response` 直接订阅发送——Pilot 模式必须替换这两端。

## Goals / Non-Goals

**Goals**

- 绑定（预绑定 + 绑定码）可信、唯一、可审计；解绑/重绑语义符合 §5.9.2。
- Telegram 入站进 canonical 流（durable、幂等、绑定门禁 fail-closed）。
- WebChat 与 Telegram 双入口共享同一 canonical 会话与 agent 上下文；WebChat 三层同步（实时帧/游标补拉/REST 重建）全通。
- Telegram 方向回复走 §5.9.11 durable delivery（worker 状态机、provider ack 推进 sent、重启补投）。
- 开关默认关；关闭时旧路径逐字不变；非法配置 fail-fast。

**Non-Goals**

- 群聊绑定、Telegram 个人账号登录（MTProto/Telethon 类）——永不在本 capability 出现（§5.9.2 冻结）。
- Telegram 入站图片/文件：首版只接受私聊文本；媒体消息收到后回「首版暂不支持」提示（不写 canonical）。扩展路径（下载 → 租户 attachment blob → `nexus_media` metadata）留后续 change。
- Telegram 出站流式 live-edit（thinking 块/临时回复编辑）：durable 模式下回复只有 final 一次投递；流式体验属旧单体模式，保留在那里。
- WebChat → Telegram 消息镜像/双向广播（§5.6 明示为独立策略）。
- WebChat 协议帧集合、前端渲染逻辑的修改（边界见 proposal）。

## Decisions

### ADR-1 表结构与约束（新 `bootstrap/db/models/telegram.py` + 单个 Alembic 迁移）

`telegram_identity_bindings`：`id` UUID PK；`account_id` FK→test_accounts(RESTRICT)；`tenant_id`（绑定时目标 agent，取账号 `created_at` 首个会话，与 `resolve_webchat_identity` 同规则，绑定时点冻结）；`telegram_user_id`（platform identity）、`telegram_chat_id`；`status` CHECK in `('active','unbound')`；`bound_via` CHECK in `('admin','code')`；`bound_by`、`note`、`bound_at`、`unbound_at`、`unbound_by`；时间戳。**双重部分唯一索引**：`uq_tib_account_active (account_id) WHERE status='active'` 与 `uq_tib_identity_active (telegram_user_id) WHERE status='active'`——unbound 行不阻塞重绑且保留审计。

`telegram_binding_codes`：`id`、`account_id` FK、`tenant_id`、`code_digest`（sha256 hex，UNIQUE）、`digest_version`、`issued_by`、`note`、`expires_at`（签发 +10min，可配置）、`consumed_at`、`created_at`。沿用 `AccessTokenModel` 的 digest-only 约定（明文只在签发响应出现一次）。

*备选被否*：单表 + status 全行唯一（无法表达「历史 unbound 行不阻塞」）；可复用 access_tokens 表加列（污染 C5 凭据语义，§10 边界）。

### ADR-2 绑定服务与端点（`bootstrap/telegram_binding.py` + `bootstrap/telegram_binding_repo.py`）

- **签发**：用户面 `POST /api/telegram/binding-codes`（挂 chat_api router，`_require_user_session` + identity 派生门禁，与 C9 persona 端点同模式）→ 为本账号签发码（返回明文一次）；每账号未消费码上限 5（防刷，超限 429 语义）。管理面 `POST /api/admin/telegram-bindings`（预绑定，直接校验并写 active 行）、`GET /api/admin/telegram-bindings`（列表）、`POST /api/admin/telegram-bindings/{id}/unbind`——挂 C5 admin router，复用 admin 门禁与审计。
- **兑换**：Telegram 私聊文本 → 绑定服务 `redeem(code, telegram_user_id, chat_id, username)`：单事务 `SELECT ... FOR UPDATE` 码行 → 校验未消费/未过期 → 插 active 绑定（唯一约束兜底并发，IntegrityError → 统一失败语义整体回滚、码不消耗）→ 置 `consumed_at` → 写审计。已绑定身份尝试兑换 → 明确拒绝（须先解绑）。
- **解析**：`resolve_telegram_identity(telegram_user_id, chat_id)` → active 绑定 + 账号 status=active + canonical 会话存在 → 返回身份三元组；任一不满足 fail-closed（不落 DEFAULT_TENANT）。
- **审计**：复用 `admin_audit_events`（actor=`telegram-binding:<uid>` 或 admin 名；action=`telegram_binding.issue/redeem/prebind/unbind`；不含明文码）。

### ADR-3 入站：TelegramChannel 增加 Pilot 门禁分支（durable 网关 `bootstrap/telegram_durable.py`）

`TelegramChannel` 增加可选 `pilot_gateway` 注入（`TelegramDurableGateway`，含绑定解析）。设置后：

1. `_on_message`（私聊 text）：未绑定身份 → 若全文命中未消费码 → 兑换并回复结果；否则回复绑定指引（不产生任何 durable 写入）。已绑定 → 群聊消息直接忽略（`chat.type != 'private'` 不进分支）→ `resolve_telegram_identity` → `gateway.accept_message(identity, source 三元组, content)`：
   - overload 预检（`bus.inbound_full()`）→ 满则直接回复「繁忙」提示（无 durable 写入，Bot 端可重发）；
   - T1 `accept_inbound(tenant, conversation, source_channel="telegram", source_identity_id, source_message_id, create_turn=True, replay_frame=message_accepted(client_message_id=f"tg:{chat_id}:{message_id}", session_key=f"chat:{tenant}"))`；
   - `publish_inbound_wait` 入队（T1 已提交，语义同 WebChat 网关），metadata 携带 `nexus_pg_*` 键贯穿到 T2。
2. `_on_photo` / `_on_document`：Pilot 分支回复「首版暂不支持媒体消息」（reply 媒体下载等旧逻辑不进 Pilot 路径）。
3. `/stop`：Pilot 分支先绑定解析，成功则以 `session_key=f"chat:{tenant}"` 请求中断（与 turn 执行同键）。
4. **会话键统一**：`InboundMessage.session_key = f"chat:{tenant_id}"`（与 WebChat 完全一致）——agent 上下文/派生 session 视图/记忆域按 tenant 解析（`session_manager.get_or_create(tenant_id, session_key)`），双入口因此共享同一上下文；`chat_id`/`sender` 仍携带 Telegram 值供出站路由。*备选被否*：`telegram:{chat_id}`（tenant 内分裂出第二个 session，双入口上下文互不可见，违背「共享规范会话历史」）。

### ADR-4 出站：泛化终态收束 + delivery worker 按 channel 分发

- `WebchatDurableTurnFinisher` 泛化：构造接受 `channel_names: frozenset[str]`（默认 `{"chat"}` 行为不变；Pilot 装配传 `{"chat","telegram"}`），`delivery_channel = msg.channel`，`delivery_target = msg.chat_id if msg.channel=="telegram" else tenant_id`；T2 之后对该 turn 的 `turn.completed` durable 帧调 `webchat_channel.deliver_frame(conversation_id, frame)` 做 WebChat 在线实时推送（durable 权威仍在重放帧表，前端按 turn_id 幂等）。
- delivery 侧加**路由适配器**（`bootstrap/telegram_durable.py`）：`DeliveryRepository.claim_batch` 不分 channel（不改 claim SQL），在 worker 的 adapter 层按 `envelope.channel` 分发：`chat` → 既有 `WebchatDeliveryAdapter`（行为不变）；`telegram` → 新 `TelegramDeliveryAdapter`——取 canonical final message 内容，经 `TelegramChannel` 暴露的 pilot 发送方法（Bot API `send_message`，成功后以 telegram message_id 作 provider receipt 推进 sent）。
- **Pilot 模式下 TelegramChannel 不订阅 outbound**（不再走 `_on_response` 直接发送）——否则与 delivery worker 双发。作为交换，投递获得 at-least-once + 重启补投 + dead_letter 处置（§5.9.11 全语义）。
- 出站媒体（回复带图/文件）：文本投递成功即记 sent，媒体随后 best-effort 发送、失败仅记日志——避免「重试整条 intent 重发文本」造成的重复消息。

*备选被否*：保留直接订阅、成功后回填 intent 状态（把 delivery 状态机拆成两条推进路径，重启窗口语义含糊）；per-channel claim SQL（改动 C2 冻结面，收益为零）。

### ADR-5 WebChat 同步帧（不改协议）

Telegram 消息的 `message.accepted` 重放帧复用既有帧型与字段（`client_message_id=f"tg:{chat_id}:{message_id}"`、`session_key=f"chat:{tenant}"`），WebChat 客户端既有逻辑将其用于 lastSeq 推进（跨端消息进补拉游标），assistant 回复经既有 `turn.completed` 帧实时渲染。跨端 user 消息在 WebChat 前端消息列表的实时渲染属 C4 前端消费增强，不在本 change 内（重载/REST 重建可见）——此边界写入 tasks 的范围检查项。

### ADR-6 配置开关与 fail-fast

`[channels.telegram] pilot_identity_binding`（默认 `false`）。装配点（`bootstrap/app.py` / `bootstrap/channels.py`）：开关开 → 要求 `storage.backend == "postgres"` 且 auth runtime 存在，否则启动抛错（fail-fast，不回落）；满足则构建绑定 repo/service/gateway、注入 TelegramChannel、finisher 订阅扩到 telegram、delivery 路由注册。开关关 → 装配路径与现状逐字节相同（不 import 新模块的副作用、不写绑定表）。

## Risks / Trade-offs

- [Pilot Telegram 无流式/无 typing 直发体验回退（相对旧单体）] → v1 冻结为 final-only durable 投递（§5.9.11 换取恢复语义）；typing chat_action 在入站受理时发送，保留基本活性提示。
- [同一 Bot token 被新旧单体同时消费] → 配置层 fail-fast 只能管 Pilot 侧；部署约束（同一 token 只切一处）写入 config 注释与 proposal Impact，不做运行时探测。
- [兑换竞态（同码并发/同身份并发绑两账号）] → 码行 `FOR UPDATE` + 绑定双重部分唯一索引兜底；IntegrityError 统一映射为用户可见失败，事务回滚零半写入（负向测试覆盖）。
- [delivery 路由把 telegram intent 误交 WebChat adapter] → 路由按 `envelope.channel` 精确分发 + 未注册 channel 的 intent 记录失败重试（不静默丢弃）；集成测试覆盖双 channel 混合投递。
- [绑定码枚举] → 码空间 ≥ 128bit 随机、digest 查找、10min 过期、单次消费、每账号在途上限；失败语义不区分「码不存在/已消费/已过期」之外的信息。
- [retention 与重放帧] → Telegram 消息的 accepted/终态帧进既有重放帧表，自然受 C12 retention 清理与消费游标约束，无需新策略。

## Migration Plan

1. 合入后默认无行为变化（开关关）：仅新增两张表迁移（`alembic upgrade head` 幂等、可 downgrade）。
2. 启用顺序：服务器 PG 已激活（pg-durable-sot-cutover 已生产激活）→ 配置 `[channels.telegram] pilot_identity_binding=true` 重启 → 管理员预绑定或用户签发绑定码 → Telegram 私聊兑换 → 双入口验证。
3. 回滚：关闭开关重启即回旧单体路径（绑定表数据保留，无清理依赖）；迁移 downgrade 仅在需要移除表时执行。

## Open Questions

<!-- 无：绑定码时长（10min）、双重唯一、私聊-only、不镜像均为 §5.9.2/§5.6/§10 冻结决策；
出站 final-only 与入站 text-only 为本文 ADR 明确冻结的首版范围。 -->
