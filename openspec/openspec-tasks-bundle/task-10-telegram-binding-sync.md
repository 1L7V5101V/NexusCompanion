# Task-10 — Telegram binding + cross-channel synchronization（telegram-binding-sync）

> 编号对应 PILOT_ROADMAP §5.9.10 第 10 项。状态标记复用 §8。

## 元数据

- **所属阶段**：主要里程碑 = P1
- **§5.9 引用**：§5.9.2（binding + dedup 语义）、§5.6（双入口及对话同步）、§10 DECIDED（Telegram 身份：管理员预绑定或一次性绑定码；接入方式并存；对话同步）
- **§6 出口条件引用**：P1 出口「WebChat 登录账号与 Telegram Bot 用户私聊身份绑定的可信关联；按 account→tenant→canonical conversation 映射两种入口，并对 channel 重试和客户端重发做幂等去重」；§5.6 同步验收条件
- **状态**：planned

## 目标

实现 Telegram 用户与 Bot 私聊身份 ↔ `test_account` 一对一绑定（管理员预绑定 + 一次性绑定码 10min/单次，§10 DECIDED「Trust the client-sent user/chat params」的反面：**不信任客户端提交的 user/chat 参数**）；双重唯一约束（account 侧 + platform identity 侧）；cross-channel 去重（同 source identity + source message id，§5.9.11 幂等键）；Telegram 新消息实时推送到 WebChat（§5.6）+ 游标补拉；解绑不删历史、新绑定不继承旧历史；首版只绑定用户与 Bot 的私聊身份，**不绑定群聊**、不登录个人账号。

## 输入

- 上游 change 产出：C1（canonical message stream + identity resolver，D1）、C2（durable inbox，E7）、C4（WebChat 实时推送，E8）、C5（auth，D3）
- roadmap 冻结决策：§5.9.2（canonical identity + Telegram binding 语义）、§5.6（对话同步验收）、§10 DECIDED（Telegram 身份、接入方式并存 = WebChat+Telegram 共享规范会话历史）
- 现有代码锚点：`bus/events.py`（跨端事件）、`infra/storage/tenancy.py`（`tenant_id_for_channel()` 现状）
- 依赖前置：C1 + C2(E7) + C4(E8) + C5(D3)

## 输出

- DB schema：`telegram_identity_bindings` / `telegram_binding_codes` 表（双重唯一约束）+ 迁移
- 代码：绑定码流程（生成/展示/兑换/10min 过期/单次使用）、同步 adapter（Telegram → canonical stream → WebChat 推送）、幂等去重、解绑/重绑逻辑、审计
- 测试/证据：约束测试、绑定码测试、Telegram 重试幂等测试、同步端到端测试、解绑/重绑测试、跨 tenant 负向测试、实现断言

## 验收标准

- [ ] 一对一唯一约束（account 侧 + platform identity 侧双唯一） — 验证：约束测试（违反唯一则报错）
- [ ] 绑定码 10min 过期 + 单次使用 — 验证：绑定码过期/重用负向测试
- [ ] 同 source identity + source message id 重试不入重（Telegram 重试/WebChat client_message_id 双键，§5.9.11） — 验证：Telegram 重试幂等测试
- [ ] WebChat 实时收到 Telegram 新消息且可按游标补拉 — 验证：同步端到端测试（§5.6）
- [ ] 解绑不删历史；新绑定不自动继承另一账号历史 — 验证：解绑/重绑测试
- [ ] 跨 tenant 负向：A 无法访问 B 的 Telegram 对话 — 验证：负向测试
- [ ] 不登录 Telegram 个人账号、不绑定群聊（首版仅私聊身份） — 验证：实现断言 + grep
- [ ] 本 task 不触碰 WebChat 协议本体（C4）与 canonical message schema（C1） — 验证：PR diff 范围检查

> 判定「真正完成」而非「执行过」：唯一约束/绑定码单次/幂等去重三条必须有失败即拒绝的负向测试；「实时收到 + 游标补拉」以端到端同步测试为准。

## 独立性边界（不与其他任务重复）

- 本任务拥有：`telegram_identity_bindings` / `telegram_binding_codes` 表、绑定码流程、同步 adapter、幂等去重、解绑/重绑审计
- 本任务不触碰：WebChat 协议本体/前端（C4，只消费推送通道）、canonical message schema（C1，只消费）、auth 端点（C5，只消费 principal）
- 共享 seam 协议：消费 C1 resolver（channel binding → account/tenant/canonical conversation）；消费 C2 durable inbox（入站幂等去重）；经 C4 WebSocket 推送 Telegram 新消息；Telegram source id 是 §5.9.11 强制幂等键

## 依赖

- **左依赖（必须先完成）**：C1（D1）+ C2（E7，durable inbox + canonical message stream）+ C4（E8，WebChat 实时推送）+ C5（D3）
- **右依赖（本任务前置于）**：无独立下游 change
- **可并行**：C6 / C7 / C9 / C11 / C14

## 风险与需冻结决策

- §10 DECIDED「Telegram 身份」：管理员预绑定或一次性绑定码，禁止信任客户端提交的 user/chat 参数 → 验收第 7 条实现断言。
- §10 DECIDED「对话同步」：PostgreSQL 统一消息流 + WebSocket 实时推送 + 游标补拉（不是两套互不相通的 session）。
- 风险：绑定码重放/竞态 → 10min + 单次 + 原子兑换；历史归属不清 → 解绑/重绑测试锁定「不删历史、不继承历史」；Telegram 私聊身份与群聊混淆 → 首版范围断言。