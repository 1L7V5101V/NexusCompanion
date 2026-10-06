## Why

M-P1 剩余 capability 中，C9（Persona/Relationship）已落地，C10（Telegram 绑定与跨通道同步）是「WebChat 登录账号与 Telegram Bot 用户私聊身份的可信关联」这一 P1 出口条件的最后一块身份拼图：没有它，Telegram 入口仍只能走旧单体 allowlist 路径（`tenant_id_for_channel` 派生、SQLite session），受邀用户的 Telegram 消息不进 canonical 流、WebChat 看不到、也无法做 durable 幂等。C1（canonical identity）/C2（durable inbox，已内建 Telegram source 三元组幂等键）/C4（WebChat 实时推送与重放）/C5（auth）四项依赖均已归档，seam 齐备，具备开工条件。

## What Changes

- **新增 `telegram_identity_bindings` / `telegram_binding_codes` 两表 + Alembic 迁移**：active binding 在 account 侧与 platform identity（Telegram user id）侧双重部分唯一约束（§5.9.2）；绑定码 digest-only、10 分钟过期、单次使用、原子兑换。
- **新增绑定生命周期服务**：管理员预绑定、账号侧签发一次性绑定码（auth session 门禁的 HTTP 端点）、Telegram 侧兑换（私聊发码给 Bot）、解绑（管理员）；兑换/解绑/预绑定写 `admin_audit_events`。
- **新增 Pilot Telegram 入站路径（`[channels.telegram] pilot_identity_binding` 开关，默认关）**：私聊文本消息先经绑定解析（source identity → account → tenant → canonical conversation，fail-closed），再走 C2 durable 接受事务（source 三元组幂等）入 canonical 流并入队 turn；未绑定用户拒绝并给出绑定指引；群聊一律不绑不收（首版只绑定私聊身份）。
- **新增 Telegram durable 出站路径**：turn 终态经 T2 写 final canonical message + `turn.completed` durable 重放帧 + pending 投递意图（channel=telegram）；回复由 delivery worker 按 §5.9.11 状态机经 Bot API 投递（`sent` 只由 provider ack 推进、重启只补投不重发 final）；WebChat 在线连接实时收到 Telegram 新消息的 `message.accepted` 帧与本轮 `turn.completed` 帧，断线重连按既有游标补拉、REST 重建以 canonical 为权威。
- **解绑/重绑语义**：解绑不删除任何 canonical 历史；Telegram 身份换绑到另一账号自然落在新账号的空会话上（不继承旧历史）；同一 Telegram 身份同时只绑定一个账号、一个账号同时只绑一个 Telegram 身份。
- **不触碰**：WebChat 协议本体与前端（C4，只经既有 `deliver_frame`/重放帧消费推送通道）、canonical message schema（C1，只消费）、auth 端点语义（C5，只消费 principal）、旧单体 Telegram 路径（开关关闭时行为逐字节不变）；不登录 Telegram 个人账号（仅 Bot API）。

## Capabilities

### New Capabilities

- `telegram-binding-sync`: Telegram 私聊身份与 test_account 的可信绑定（双重唯一约束、绑定码、审计）、绑定门禁的 durable 入站（source 三元组幂等）、跨通道同步（canonical 流 + WebChat 实时帧/补拉/重建）、durable 出站投递与解绑/重绑语义。

### Modified Capabilities

<!-- 无：durable-control-plane 的幂等双键与 delivery 状态机已通道无关且 SHALL 语义不变；
webchat-durable-storage / webchat-protocol-dev-loop 的协议帧集合与重放语义不变；
本 change 只新建 capability，消费既有 seam。 -->

## Impact

- **代码**：`bootstrap/db/models/telegram.py`（新）、`bootstrap/db/repository/telegram_repo.py`（新）、`bootstrap/telegram_binding.py`（绑定服务，新）、`bootstrap/telegram_durable.py`（入站网关 + 终态收束 + 投递适配器，新）、`bootstrap/channels.py` / `bootstrap/app.py`（Pilot 模式装配）、`infra/channels/telegram_channel.py`（Pilot 分支：绑定门禁 + durable 接受，禁用直接 outbound 订阅）、`bootstrap/webchat_durable.py`（终态收束器与 delivery 循环泛化为多 channel 分发，WebChat 行为不变）、`bootstrap/chat_api.py`（绑定码端点）、`bootstrap/auth/api.py`（admin 预绑定/解绑端点）、`alembic/versions/`（新迁移）。
- **测试**：约束负向、绑定码过期/重用/竞态、Telegram 重试幂等、跨通道同步端到端（WS 帧 + 游标补拉 + REST 重建）、解绑/重绑、跨 tenant 负向、范围断言（群聊拒绝/无个人账号登录/mono 路径不变）。
- **部署**：`config.server.toml` 需显式开启 `pilot_identity_binding` 且要求 `storage.backend="postgres"` + auth 启用（配置非法 fail-fast）；同一 Bot token 仍禁止新旧单体与 Pilot 同时消费（§5.9.2，运维约束）。
