# task 7.8 / 8.2 — 范围断言与 PR diff 范围检查（2026-10-07）

## 检查命令与结果

### 1. WebChat 协议本体（C4）与 canonical message schema（C1）零 diff

```
$ git diff main -- infra/channels/web_chat_protocol.py bootstrap/db/models/canonical.py frontend/chat/
（空输出 = 零改动）
```

- 协议帧集合（`message.accepted`/`turn.completed`/...）无新增/修改；前端零改动。
- 同步推送只**消费**既有帧型：T1 经 `_record_replay_frame` 写 `message.accepted`
  durable 重放帧（C2 既有事务内机制），T2 写 `turn.completed`（既有）；
  实时下发经既有 `WebChatChannel.deliver_frame`。

### 2. Telegram 个人账号登录依赖（应为空 = 仅 Bot API）

```
$ grep -rn "telethon|pyrogram|MTProto" bootstrap/ infra/ agent/ --include="*.py" -i
（空输出）
```

- Pilot 通道唯一 Telegram 依赖 = `python-telegram-bot`（Bot API，既有依赖）。

### 3. 群聊不绑定/不接收（首版仅私聊身份）

```
$ grep -n '"private"' infra/channels/telegram_channel.py
578/606/628/662: 四个 Pilot 分支（text/command/stop/media）均以
chat.type != "private" → return 开头；绑定服务无任何 group 绑定路径。
```

### 4. 旧单体路径不变性（开关关闭行为逐字节等价）

- `infra/channels/telegram_channel.py` diff 全部为**纯增量**：`pilot_ingress`
  可选参数 + 各 handler 头部 `if self._pilot is not None: ... return` 早退分支 +
  `_bind_runtime` 的 `self._pilot is None` 守卫 + Pilot 私有方法块。
  开关关闭时（`pilot_ingress=None`）每个 handler 走原代码路径；
  `_bind_runtime` 守卫为真 → 原订阅行为。单元测试
  `test_pilot_mode_skips_outbound_and_event_subscription` 锁定两种模式。
- `bootstrap/channels.py`：仅新增可选参数透传。
- `bootstrap/webchat_durable.py`：finisher 泛化（`channel_names` 缺省
  `None` → `frozenset({channel_name})`，与原单 channel 行为一致）+
  delivery adapter 包一层路由（`chat` → 原 `WebchatDeliveryAdapter`，
  行为不变）。既有 pg_sot webchat e2e（`test_full_roundtrip_with_restart`）
  回归全绿。

### 5. PR diff 文件清单（对照 proposal Impact，无越界文件）

```
agent/config.py / agent/config_models.py           （配置开关 + 解析）
alembic/versions/f5a9c1e3b7d2_c10_telegram_binding.py（迁移，expand-only）
bootstrap/app.py                                    （装配 + fail-fast）
bootstrap/auth/api.py                               （admin 路由 + PrebindRequest）
bootstrap/auth/runtime.py                           （telegram_binding 注入位）
bootstrap/channel_host.py                           （get(name) 访问器）
bootstrap/channels.py                               （pilot_ingress 透传）
bootstrap/chat_api.py                               （用户面路由 + _IssueCodeBody）
bootstrap/db/models/{__init__,telegram}.py          （模型）
bootstrap/db/repository/telegram_repo.py            （仓储）
bootstrap/telegram_binding.py                       （服务层）
bootstrap/telegram_durable.py                       （网关/门面/投递适配）
bootstrap/webchat_durable.py                        （finisher 泛化 + 路由装配）
infra/channels/telegram_channel.py                  （Pilot 分支，纯增量）
tests/pg_sot/{conftest.py,+3 新文件}                （验收测试）
tests/test_telegram_pilot_channel.py                （单元测试）
openspec/changes/c10-telegram-binding-sync/*        （提案工件）
```

### 6. pyright

```
$ uv run --no-sync pyright bootstrap/telegram_{durable,binding}.py \
    bootstrap/db/repository/telegram_repo.py bootstrap/db/models/telegram.py \
    bootstrap/chat_api.py bootstrap/auth/api.py bootstrap/webchat_durable.py
0 errors（warnings 与既有模块同级，无新错误）
```

### 7. 验收测试（对应 task-10 验收 1–7）

```
$ NEXUS_REQUIRE_PG=1 NEXUS_TEST_PG_URL=postgresql://nexus:nexus_dev@localhost:5433/nexus \
  pytest tests/pg_sot/test_telegram_binding_e2e.py \
         tests/pg_sot/test_telegram_binding_api.py \
         tests/test_telegram_pilot_channel.py
14 passed
```

- 验收 1（双唯一）：`test_full_sync_roundtrip` 并发 prebind 竞态段 +
  `test_binding_negatives_and_races`（并发同身份绑两账号恰一个成功）。
- 验收 2（绑定码 10min/单次）：过期拒绝且不消耗、重放拒绝、
  兑换冲突码不消耗（negatives）+ HTTP 429 在途上限（api）。
- 验收 3（Telegram 重试幂等）：同 source 三元组重复注入零新写入（e2e）。
- 验收 4（实时 + 补拉）：WebChat 在线连接实时收 `message.accepted`（seq=1）/
  `turn.completed`（seq=2）；重连 `replay_after` 连续无重复；REST 重建
  顺序 = canonical sequence（e2e）。
- 验收 5（解绑/重绑）：解绑后 canonical 计数不变；换绑账号 B 会话空历史。
- 验收 6（跨 tenant 负向）：账号 2 请求账号 1 会话 404；未绑定身份
  fail-closed 无 canonical 写入。
- 验收 7（范围断言）：本文 1–3 节。

### 8. 全量回归

见 `task-8.1-regression.txt`。
