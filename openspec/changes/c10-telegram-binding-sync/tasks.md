# C10 Telegram binding + cross-channel sync — 实施任务

> 对应 `openspec/changes/c10-telegram-binding-sync/`；design.md ADR-1..ADR-6 为实现约束，偏离需先改 design。
> 分支：`feature/c10-telegram-binding-sync`（基于 main tip）。
> 证据统一落 `openspec/evidence/c10-telegram-binding-sync/`（每条任务给出命令与结果）。
> 环境：本地 PG 5433（Docker 实例）；集成测试 `NEXUS_TEST_PG_URL` + `NEXUS_REQUIRE_PG=1`
> 走独立 scratch 库（模式同 `tests/retention/conftest.py` / C9 先例）。
> 前置：C1/C2/C4/C5 已归档 ✅（`accept_inbound` 已内建 Telegram source 三元组幂等键）。
> 验收映射：task-10 验收 1→任务 1.x/7.2；2→7.3；3→7.4；4→7.5；5→7.6；6→7.7；7→7.8；8→8.2。

## 1. 迁移与模型（ADR-1）

- [ ] 1.1 新增 alembic revision（expand-only，`down_revision` 取实现时 `alembic heads` 实测值）：
  `telegram_identity_bindings`（account_id FK RESTRICT + tenant_id + telegram_user_id +
  telegram_chat_id + status CHECK ∈ ('active','unbound') + bound_via CHECK ∈ ('admin','code')
  + bound_by/note/bound_at/unbound_at/unbound_by + 时间戳；**部分唯一索引**
  `uq_tib_account_active(account_id) WHERE status='active'` 与
  `uq_tib_identity_active(telegram_user_id) WHERE status='active'`）、
  `telegram_binding_codes`（account_id FK + tenant_id + code_digest UNIQUE（sha256 hex
  长度 CHECK）+ digest_version + issued_by/note + expires_at + consumed_at + created_at）。
  验证：`upgrade → downgrade -1 → upgrade head` 闭环实跑 5433；`information_schema`
  断言两表与两个部分唯一索引存在。证据：`task-1.1-migration.md`
- [ ] 1.2 模型新增 `bootstrap/db/models/telegram.py`（列与迁移逐字一致）+ 仓储
  `bootstrap/db/repository/telegram_repo.py`：`TelegramBindingRepository`
  （预绑定创建、按身份解析 active 绑定、解绑（置 unbound + 时间戳/操作者，幂等）、
  列表、绑定码签发/按 digest 查找/原子消费原语、审计追加）。
  验证：真 PG 用例——unbound 行不阻塞重绑；解绑幂等；digest 查找不命中返回 None。
  证据：`task-1.2-repository.md`

## 2. 绑定生命周期服务（ADR-2）

- [ ] 2.1 `bootstrap/telegram_binding.py`：`TelegramBindingService`——
  `issue_code`（≥128bit 随机码、sha256 入库、默认 10min 过期、每账号未消费码上限 5、
  返回明文一次）、`redeem`（单事务：码行 `FOR UPDATE` → 校验未消费/未过期 → 插
  active 绑定（唯一冲突 IntegrityError → 统一失败语义整体回滚、码不消耗）→ 置
  consumed_at → 审计；已绑定身份兑换 → 拒绝）、`prebind`（admin，唯一冲突同样
  fail-closed）、`unbind`（admin，幂等 + 审计）、`resolve_identity`（active 绑定 +
  账号 active + canonical 会话存在 → 三元组；任一不满足 fail-closed）。
  验证：并发兑换同码只成功一次（两任务并发）；过期/已消费/未知码统一拒绝语义。
  证据：`task-2.1-service.md`
- [ ] 2.2 审计接线：issue/redeem/prebind/unbind 各写 `admin_audit_events`
  （action=`telegram_binding.issue/redeem/prebind/unbind`；不含明文码/session 凭据）。
  验证：四个动作后审计行存在且内容 grep 无明文码。证据：并入 `task-2.1-service.md`

## 3. HTTP 端点（ADR-2）

- [ ] 3.1 用户面 `POST /api/telegram/binding-codes`（router 挂 chat_api，`_require_user_session`
  + identity 派生门禁，与 C9 persona 端点同模式）：为本账号签发码，响应含明文码与
  过期时间；在途超限 → 429 语义。`GET /api/telegram/binding`：返回当前账号绑定状态
  （已绑/未绑、绑定身份摘要、在途码数）。验证：未认证 401；签发 201；超限 429；
  响应不含 digest。证据：`task-3.1-endpoints.md`
- [ ] 3.2 管理面（挂 C5 admin router）：`POST /api/admin/telegram-bindings`（预绑定：
  account_id + telegram_user_id/chat_id + note）、`GET /api/admin/telegram-bindings`
  （列表）、`POST /api/admin/telegram-bindings/{id}/unbind`。验证：非 admin 403；
  唯一冲突 409 语义；解绑后可重绑。证据：并入 `task-3.1-endpoints.md`

## 4. 入站路径（ADR-3）

- [ ] 4.1 `bootstrap/telegram_durable.py`：`TelegramDurableGateway.accept_message`——
  overload 预检（`bus.inbound_full()` → 满返回 busy 语义）→ T1 `accept_inbound`
  （source 三元组，`create_turn=True`，`replay_frame=message_accepted(client_message_id=
  f"tg:{chat_id}:{message_id}", session_key=f"chat:{tenant}")`）→ `publish_inbound_wait`
  （metadata 携带 `nexus_pg_*` 贯穿键）。duplicate 路径零新写入。
  验证：真 PG 集成用例——接受后 canonical/inbox/turn 各一条；同三元组重复接受幂等。
  证据：`task-4.1-gateway.md`
- [ ] 4.2 `infra/channels/telegram_channel.py` 增加 Pilot 门禁分支（可选
  `pilot_gateway` 注入，缺省行为逐字节不变）：私聊 text → 未绑定身份先试兑换
  （全文命中未消费码）失败则回绑定指引（零 durable 写入）；已绑定 →
  `resolve_identity` → 网关接受；`/stop` Pilot 分支按 `session_key=f"chat:{tenant}"`
  请求中断；photo/document Pilot 分支回「首版暂不支持」；群聊消息不进绑定/接受路径；
  入站受理时发送 typing chat_action。
  验证：单测用假网关/假 Bot——未绑定指引路径、绑定接受路径、群聊忽略、
  开关关闭走旧路径（对旧 handler 行为快照不变）。证据：`task-4.2-channel.md`

## 5. 出站路径（ADR-4）

- [ ] 5.1 `WebchatDurableTurnFinisher` 泛化：接受 `channel_names: frozenset`（默认
  `{"chat"}` 行为不变），`delivery_channel=msg.channel`、`delivery_target=telegram 取
  msg.chat_id / chat 取 tenant_id`；T2 后对该 turn 的 `turn.completed` durable 帧
  调 `webchat_channel.deliver_frame` 实时推送 WebChat。验证：既有 webchat 终态测试
  全绿（无回归）+ 新用例：telegram outbound 触发 T2 且 intent channel/target 正确。
  证据：`task-5.1-finisher.md`
- [ ] 5.2 delivery 分发路由：worker adapter 层按 `envelope.channel` 分发（`chat` →
  既有 WebchatDeliveryAdapter 原样；`telegram` → 新 `TelegramDeliveryAdapter`：取
  canonical final message → Bot API 发送 → telegram message_id 作 receipt 推进 sent；
  文本成功即 sent、媒体 best-effort 失败仅记日志）。Pilot 模式 TelegramChannel 不订阅
  outbound（不双发）；delivery worker 暴露 pilot 发送方法。
  验证：混合双 channel intent 的 claim→分发→receipt 用例；未注册 channel 的 intent
  记失败重试不静默。证据：`task-5.2-delivery.md`

## 6. 装配与配置（ADR-6）

- [ ] 6.1 `agent/config_models.py` 增 `[channels.telegram] pilot_identity_binding`
  （默认 false）；`bootstrap/app.py`/`bootstrap/channels.py` 装配：开 → 要求
  backend=postgres 且 auth 启用，否则启动 fail-fast；满足则构建 repo/service/gateway、
  注入 channel、finisher 订阅扩 `{"chat","telegram"}`、delivery 路由注册。
  验证：配置矩阵用例（关=现状装配；开+sqlite=fail-fast 报错；开+pg+auth=装配成功
  且 gateway 注入）。证据：`task-6.1-wiring.md`

## 7. 验收测试（对应 task-10 验收标准）

- [ ] 7.1 端到端同步测试（真 PG + 假 Bot/假 WS 客户端）：绑定 → Telegram 私聊文本 →
  T1 提交 → turn 执行（假 runtime）→ T2 → 断言：WebChat 测试客户端实时收到
  `message.accepted`（含 durable seq）与 `turn.completed` 帧；断线重连按游标补拉到
  缺失帧；REST 重建含双入口消息且顺序与 canonical sequence 一致。
  证据：`task-7.1-sync-e2e.txt`
- [ ] 7.2 唯一约束负向（失败即拒绝）：并发同身份绑两账号 / 同账号绑两身份各只成功
  一个；违反唯一整体回滚无半绑定。
- [ ] 7.3 绑定码负向（失败即拒绝）：过期码拒绝且不消耗；已消费码重放拒绝；
  兑换事务绑定冲突时码不消耗。
- [ ] 7.4 Telegram 重试幂等（失败即拒绝）：同 source identity + source message id
  重试不入重（复用 `accept_inbound` 幂等路径 + 网关级重复返回既有身份）。
- [ ] 7.5 同步端到端（即 7.1，验收第 4 条以该测试为准）。
- [ ] 7.6 解绑/重绑：解绑不删历史（canonical 消息数不变）；换绑另一账号后新会话
  空历史开始、不继承。
- [ ] 7.7 跨 tenant 负向：账号 B 的 WebChat session 无法拉取账号 A 的会话（既有
  门禁回归）+ Telegram 身份绑定 A 后不可能触发 B 的会话（解析只按绑定表）。
- [ ] 7.8 范围断言：实现断言 + grep——仅 Bot API（无个人账号登录依赖）、群聊不绑定
  （`chat.type=='private'` 门禁）、`web_chat_protocol.py` 与 canonical message schema
  零 diff、开关关闭时 `telegram_channel.py` 旧路径 diff 为纯增量分支。
  证据：`task-7.8-scope-check.txt`

## 8. 收尾

- [ ] 8.1 全量回归：`NEXUS_REQUIRE_PG=1`（5433 可达）全量 pytest 全绿（对齐
  C9 基线 1921+），pyright 新增代码零新错误。证据：`task-8.1-regression.txt`
- [ ] 8.2 PR diff 范围检查（验收第 8 条）：diff 只含本 change 清单文件 + openspec；
  写明 ADR-5 的边界遵守方式（未改协议帧集合/前端渲染）。证据：并入
  `task-7.8-scope-check.txt`
- [ ] 8.3 `openspec validate --change c10-telegram-binding-sync` 通过；PILOT_ROADMAP
  PROJECT_CHECKLIST §5.9.2 Telegram 行与 P1 条目回填（待归档时 sync spec）。
