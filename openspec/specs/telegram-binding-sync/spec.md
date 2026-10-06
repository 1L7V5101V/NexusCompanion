# telegram-binding-sync Specification

## Purpose

定义 Telegram 用户与 Bot 私聊身份和 test_account 的可信绑定与跨通道同步的行为契约：
绑定双重唯一约束、一次性绑定码与管理员预绑定、绑定门禁的 durable 入站、跨通道同步
（canonical 流 + WebChat 实时/补拉/重建）、durable 出站投递、解绑/重绑语义与审计、
首版范围断言。依据 PILOT_ROADMAP §5.6（双入口及对话同步）、§5.9.2（binding 语义）、
§5.9.11（幂等与 delivery 事务）、§10 DECIDED（Telegram 身份：管理员预绑定或一次性
绑定码；接入方式并存；对话同步）冻结。

## Requirements

### Requirement: 绑定双重唯一约束

每个 test_account 最多绑定一个 Telegram 用户身份，同一 Telegram 用户身份最多绑定
一个 test_account；两条唯一性 SHALL 由数据库部分唯一索引在 active 状态上强制。
违反任一侧唯一性的绑定写入 SHALL 失败并整体回滚，SHALL NOT 产生半绑定状态。
绑定 SHALL 服务端可信查表（source identity → account → tenant → canonical
conversation），SHALL NOT 信任客户端提交的 user/chat/tenant 参数决定归属。

#### Scenario: 同一 Telegram 身份并发绑定两个账号只成功一个

- **WHEN** 两个并发请求把同一 Telegram user id 分别绑定到账号 A 与账号 B
- **THEN** 恰好一个请求成功，另一个因 identity 侧唯一约束失败回滚；库中该身份仅有一条 active 绑定

#### Scenario: 同一账号并发绑定两个 Telegram 身份只成功一个

- **WHEN** 两个并发请求把两个不同 Telegram user id 绑定到同一账号
- **THEN** 恰好一个请求成功，另一个因 account 侧唯一约束失败回滚；库中该账号仅有一条 active 绑定

#### Scenario: 解绑后的行不阻塞重绑

- **WHEN** 一条绑定被解绑（active → unbound）后，同一 Telegram 身份再次绑定同一账号
- **THEN** 新建 active 绑定行成功；历史 unbound 行保留供审计

### Requirement: 一次性绑定码生命周期

绑定码 SHALL 以 digest 形式存储（明文不落库、不进日志与审计）；绑定码默认 10 分钟
过期且单次使用；兑换 SHALL 原子完成「校验未消费/未过期 → 创建 active 绑定（双重唯一
约束生效）→ 标记码已消费」，任一步失败整体回滚且码不被消耗；已消费/已过期/被撤销的
码 SHALL 拒绝兑换且错误语义 SHALL NOT 泄露具体原因之外的存在性信息。绑定码 SHALL
与签发账号（及其目标 agent/tenant）预关联，兑换即绑定到该账号。

#### Scenario: 过期码拒绝兑换

- **WHEN** 用户兑换一张签发超过 10 分钟的绑定码
- **THEN** 兑换被拒绝，不创建绑定，码不被标记消费，用户收到过期提示

#### Scenario: 已消费码不可重放

- **WHEN** 同一绑定码被成功兑换后再次提交（含并发重放）
- **THEN** 第二次及后续兑换全部被拒绝；库中仅第一次兑换产生的绑定与消费记录

#### Scenario: 兑换原子性

- **WHEN** 兑换事务中绑定创建因唯一约束冲突失败（如账号已绑定其他身份）
- **THEN** 整体回滚：码保持未消费状态，不产生绑定行；用户收到失败提示

### Requirement: 管理员预绑定与解绑

管理员 SHALL 能直接为指定账号创建 Telegram 身份绑定（预绑定），并 SHALL 能解绑
任一 active 绑定；预绑定与解绑 SHALL 写审计事件（动作、目标账号、目标 Telegram
身份、操作者）。解绑 SHALL 只把绑定标记为 unbound（保留行与时间戳供审计），SHALL
NOT 删除该账号 canonical 会话中的任何历史消息；解绑后该 Telegram 身份的后续消息
SHALL 被拒绝并收到重新绑定指引。

#### Scenario: 预绑定后立即生效

- **WHEN** 管理员为账号 A 预绑定 Telegram user 42 后，user 42 向 Bot 私聊发消息
- **THEN** 消息按账号 A 的 canonical 会话接受入站，无需兑换绑定码

#### Scenario: 解绑保留历史

- **WHEN** 管理员解绑 user 42 与账号 A 的绑定
- **THEN** 账号 A 的 canonical 会话与全部历史消息保持不变；user 42 后续私聊消息被拒绝且不产生 canonical 消息

#### Scenario: 换绑不继承历史

- **WHEN** user 42 解绑后通过绑定码绑定到账号 B
- **THEN** user 42 的消息进入账号 B 自己的 canonical 会话（空历史开始），SHALL NOT 看到或续写账号 A 的任何历史

### Requirement: 绑定门禁的 durable 入站

开启 Pilot Telegram 接入后，私聊文本消息 SHALL 先经绑定解析得到 account → tenant →
canonical conversation；无 active 绑定、账号非 active 状态、或 canonical 会话不存在
时 SHALL fail-closed 拒绝（不落默认租户、不猜测归属），未绑定用户 SHALL 收到绑定
指引而非静默丢弃。已绑定消息 SHALL 经 durable 入站接受事务（C2 T1）以 Telegram
source 三元组（source channel + source identity + source message id）幂等去重后写入
canonical 流并入队 turn；重复投递（Bot API 重试/重连）SHALL NOT 产生第二条 canonical
消息或 turn。群聊消息 SHALL NOT 被绑定或接受（首版仅私聊身份）；系统 SHALL NOT 登录
Telegram 个人账号（仅 Bot API）。

#### Scenario: Telegram 重试幂等

- **WHEN** 同一 Telegram update（同 source identity + source message id）在接受事务提交后被重复投递
- **THEN** 第二次投递返回既有身份（幂等成功路径），canonical 流、inbox、turn 数量不变

#### Scenario: 未绑定用户拒绝且不落默认租户

- **WHEN** 一个从未绑定的 Telegram 用户私聊 Bot 发消息
- **THEN** 消息被拒绝，收到绑定指引，不产生 canonical 消息/inbox/turn，会话归属不落到任何默认租户

#### Scenario: 群聊消息不入绑

- **WHEN** Bot 被拉入群聊且群成员发消息
- **THEN** 消息被忽略（不绑定、不产生 canonical 消息），实现中不存在群聊绑定为 tenant 的路径

#### Scenario: 非活跃账号的门禁

- **WHEN** 绑定仍为 active 但账号状态为 suspended/revoked 时其 Telegram 身份发消息
- **THEN** 消息被 fail-closed 拒绝并收到状态提示，不产生 canonical 消息

### Requirement: 跨通道同步

Telegram 入口的用户消息与对应 turn 的 assistant 回复 SHALL 写入该账号的 canonical
会话（与 WebChat 共享同一统一消息流）；WebChat 在线连接 SHALL 实时收到 Telegram
新消息的接受帧与本轮终态帧；WebChat 断线重连 SHALL 按既有 sequence 游标补拉到这些
帧，REST 重建 SHALL 以 canonical 流为权威包含双入口消息且顺序一致。两种入口同时
活动 SHALL 互不停用、互不降级对方通道；系统 SHALL NOT 自动把 WebChat 消息镜像
发送到 Telegram（跨端镜像为后续独立策略）。

#### Scenario: WebChat 实时收到 Telegram 新消息

- **WHEN** WebChat 连接在线期间，该账号绑定的 Telegram 身份发送私聊消息
- **THEN** 该 WebChat 连接实时收到该消息的 `message.accepted` 帧（含 durable seq）及后续 turn 终态帧

#### Scenario: 断线补拉不遗漏

- **WHEN** WebChat 断线期间 Telegram 收发若干消息，随后重连并按最后游标补拉
- **THEN** 补拉结果按序包含缺失的 `message.accepted` 与 `turn.completed` 帧；REST 重建的顺序与 canonical sequence 一致且无重复

#### Scenario: 双入口同一会话上下文

- **WHEN** 同一账号先后从 WebChat 与 Telegram 发消息
- **THEN** 两条消息进入同一 canonical 会话（同 sequence 流），agent 两侧可见同一会话上下文与记忆域

### Requirement: durable 出站投递

Telegram 方向的 assistant 回复 SHALL 经执行完成事务（T2）原子写入 final canonical
message、turn 终态与 pending 投递意图；实际投递 SHALL 由 delivery worker 按状态机
`pending/attempting/sent/failed/dead_letter` 执行，`sent` SHALL 只由 Bot API 成功
确认推进并记录 provider receipt；进程重启 SHALL 只对未确认送达的意图补投，SHALL
NOT 重新生成或复制 final 消息。

#### Scenario: 重启后补投不重发 final

- **WHEN** T2 已提交但 Bot API 投递未确认时进程重启
- **THEN** 重启后 delivery worker 继续投递该意图（at-least-once），final canonical 消息只有一条；已 sent 的意图不再投递

#### Scenario: 投递失败不丢回复

- **WHEN** Bot API 投递持续失败到最大尝试次数
- **THEN** 意图进入 dead_letter 且 final 消息仍可从 canonical 流按序补拉；管理员可经既有 redrive 入口人工重投

### Requirement: 绑定操作审计

绑定码兑换成功、管理员预绑定、解绑 SHALL 各写一条审计事件（动作、目标账号、目标
Telegram 身份、操作者/来源、时间）；审计 SHALL NOT 包含绑定码明文或 session 凭据。

#### Scenario: 兑换与解绑可追溯

- **WHEN** 检查任一次绑定码兑换与任一次解绑的审计记录
- **THEN** 均能查到对应事件且不含明文绑定码

### Requirement: Pilot 开关与旧单体路径不变性

Pilot Telegram 接入 SHALL 由显式配置开关启用（默认关闭）；开关关闭时旧单体 Telegram
路径行为 SHALL 逐字保持不变。开关开启但前置条件不满足（非 PostgreSQL 后端或 auth
未启用）SHALL 在启动时 fail-fast，SHALL NOT 静默回落旧路径。同一 Bot token 的更新流
SHALL NOT 被旧单体与 Pilot 同时消费（部署约束，配置层不做兜底合并）。

#### Scenario: 开关关闭行为不变

- **WHEN** 配置未开启 Pilot 开关时启动并收发 Telegram 消息
- **THEN** 消息走旧单体路径（allowlist + channel:chat_id 派生租户），无绑定表写入、无 canonical 流写入

#### Scenario: 非法组合 fail-fast

- **WHEN** 开关开启但存储后端不是 PostgreSQL（或 auth 未启用）
- **THEN** 启动失败并给出明确原因，不回落旧路径、不半启用
