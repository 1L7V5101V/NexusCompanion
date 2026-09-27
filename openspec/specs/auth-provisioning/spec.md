# auth-provisioning Specification

## Purpose
定义 Pilot 受邀用户认证（邀请 Token 一次性兑换 → 登录会话）、单一 admin principal
（bootstrap/轮换/撤销）、账号 provisioning/readiness 生命周期，以及浏览器安全边界
（Cookie/CSRF/Origin/401-403）的行为契约。attachment 授权（C6）、工具 allowlist（C7）、
Telegram binding（C10）、schedule（C11）、WebChat 协议帧契约（C4）不在本规格范围。

## Requirements

### Requirement: 邀请 Token 一次性原子兑换

系统 SHALL 支持签发一次性**租户邀请码**（仅存 HMAC-SHA-256-with-pepper digest，携带管理员
预指定的租户名；签发 SHALL NOT 要求预先存在的账号）。注册请求 SHALL 在单个数据库事务中
完成「锁定邀请码行 → 校验未消费/未撤销/未过期 → 创建 `provisioning` 账号（含全局唯一邮箱与
argon2 密码哈希）与持久 provisioning job → 标记已消费」。并发注册同一邀请码 SHALL 恰好一个
成功；账号转为 `active` 前 SHALL NOT 建立登录会话；provisioning 失败时账号 SHALL 进入
`failed` 且邀请码 SHALL NOT 重复签发或重复消费（管理员可对同一 job 幂等 retry，不产生第二个
tenant）；`suspended`/`revoked` 账号 SHALL NOT 获得新会话。明文凭据 SHALL NOT
出现在数据库、日志、审计或前端持久存储。

#### Scenario: 并发兑换同一 Token 仅一个成功

- **WHEN** 多个客户端并发用同一邀请码 + 各自邮箱调用注册
- **THEN** 恰好一个请求成功并获得登录会话，其余请求以认证错误失败，且系统只创建一个账号与一个会话行

#### Scenario: 非 active 账号的 Token 不可兑换

- **WHEN** 邀请码已被消费（其对应账号仍处于 `provisioning`/`failed`/`suspended`/`revoked`）时重放注册请求
- **THEN** 注册失败并返回认证错误，不创建新会话；账号不进入 `active` 前不产生任何可用会话

#### Scenario: 邀请码签发无需预先存在的账号

- **WHEN** 管理员为不存在的目标租户签发邀请码（仅指定租户名）
- **THEN** 签发成功返回一次性明文邀请码，不创建账号、不触发 provisioning

#### Scenario: provisioning 失败后 retry 收敛

- **WHEN** 注册触发的 provisioning job 执行失败（账号 `failed`、邀请码已消费）后管理员对同一 job retry
- **THEN** 同一 job 行推进（attempt 递增），tenant/conversation 与失败前一致，账号最终 `active`，之后登录成功

#### Scenario: 明文凭据不落库

- **WHEN** 检查 `access_tokens`、`test_accounts`、`auth_sessions` 表及全部日志/审计输出
- **THEN** 只存在 64 字符 HMAC digest 与 argon2 密码哈希，不存在任何 `nxt_`/`nad_`/`ns_` 前缀明文凭据或可逆密码

### Requirement: 邮箱密码注册与登录

系统 SHALL 支持以「全局唯一邮箱 + 密码」注册新账号：密码 SHALL 以 argon2 哈希保存（仅存哈希，
不存明文、不存可用于恢复明文的熵），注册成功即进入账号 provisioning 生命周期（与邀请码注册
同一账户体系），SHALL NOT 要求邮箱验证。系统 SHALL 提供邮箱+密码登录入口，校验通过后建立
登录会话；校验失败 SHALL 使用同一认证错误语义，SHALL NOT 区分「邮箱不存在」「密码错误」
「账号被 suspended/revoked」等具体原因。登录会话 SHALL 与邀请码兑换产生的会话共享同一
timeout 与应用权限边界。

#### Scenario: 重复邮箱注册被拒绝

- **WHEN** 两个不同邀请码下以同一邮箱注册
- **THEN** 第二次注册以认证错误失败，且不泄露既有账号是否存在

#### Scenario: 错误响应不泄露账号状态

- **WHEN** 分别以「邮箱不存在」「密码错误」「账号已 suspended」尝试登录
- **THEN** 三者返回同一语义的认证错误，响应中不存在可区分三者的字段或文案

#### Scenario: 密码哈希不落明文

- **WHEN** 检查数据库 `password_digest` 列
- **THEN** 只存在 argon2 哈希字符串，任何日志/审计/API 响应均不含密码原文或哈希

#### Scenario: 登录成功建立会话

- **WHEN** 合法邮箱密码登录成功
- **THEN** 返回登录会话（HttpOnly Cookie），随后以该会话访问受保护资源成功，且会话归属该账号的租户

### Requirement: 登录会话与 timeout 契约

登录会话 SHALL 以 HttpOnly Cookie（普通 `__Host-nexus_session`，admin `__Host-nexus_admin`，
两者不可互换）承载，服务端仅存 digest；普通会话默认 idle 7 天 / absolute 30 天，admin 会话
默认 idle 30 分钟 / absolute 12 小时（创建时固化到会话行）。`logout` SHALL 只撤销当前会话；
账号级凭据撤销 SHALL 是独立管理操作。

#### Scenario: Cookie 隔离

- **WHEN** 用 admin 会话 Cookie 访问普通用户会话校验，或反向
- **THEN** 校验失败（principal_type 与 Cookie 不匹配）

#### Scenario: idle timeout 到期后会话失效

- **WHEN** 会话超过其 idle timeout 未活动
- **THEN** 后续请求返回 401，不返回任何账号身份信息

### Requirement: 浏览器安全边界

修改型 API（POST/PUT/PATCH/DELETE）SHALL 同时校验 Origin/Referer allowlist 与 session-bound
CSRF token，任一失败 SHALL 403 且不泄露校验维度；WebSocket 握手 SHALL 校验登录 Cookie 与
Origin allowlist；401 SHALL 表示无有效 principal/session，403 SHALL 表示 principal 有效但被
禁止（封禁/CSRF/来源/边界）；错误响应 SHALL NOT 泄露账号、Token 或 binding 是否存在。

#### Scenario: 缺少 CSRF token 的 mutation 被拒绝

- **WHEN** 携带有效 Cookie 与合法 Origin 但缺少 `X-CSRF-Token` 发起 mutation
- **THEN** 返回 403，错误体为统一 forbidden 文案

#### Scenario: 非回环来源访问 admin API 被拒绝

- **WHEN** 非回环客户端地址请求 `/api/admin/*`
- **THEN** 返回 403 统一文案，不区分资源是否存在

#### Scenario: 封禁账号请求返回 403 而非 401

- **WHEN** 账号被 `suspended`/`revoked` 后，其仍有 Cookie 的会话发起请求
- **THEN** 返回 403（principal 有效但被封禁），会话与 Token 记录保留 `revoked_at` 审计痕迹

### Requirement: Admin bootstrap 与恢复

系统 SHALL 提供单一 admin principal：`pilot-admin bootstrap/status/rotate-recovery-token/
revoke-sessions/disable/enable`。bootstrap SHALL 仅在受信主机交互式 TTY 成功且仅当 admin
不存在；recovery token 明文 SHALL 只向 TTY 回显一次；token 轮换与浏览器 session 撤销 SHALL
为独立操作（轮换默认不撤销有效 browser sessions）；`disable` SHALL 拒绝新 exchange 并立即
终止既有 admin 浏览器会话；所有 admin 操作 SHALL 写审计且不出现明文。

#### Scenario: 重复 bootstrap 被拒绝

- **WHEN** admin principal 已存在时再次运行 `pilot-admin bootstrap`
- **THEN** 命令失败且不产生第二个 admin 凭据

#### Scenario: 轮换不撤销有效 browser sessions

- **WHEN** 存在有效 admin 浏览器会话时执行 recovery token 轮换
- **THEN** 旧 recovery token 立即失效、新 token 只显示一次，既有浏览器会话保持有效

### Requirement: 账号 provisioning/readiness 生命周期

账号创建 SHALL 进入 `provisioning` 并生成持久 provisioning job；job 完成（partition +
canonical conversation 就绪）后账号 SHALL 进入 `active`；pending/failed job SHALL 在进程启动时
从 PostgreSQL 恢复并支持幂等 retry（不产生第二个 tenant/conversation/seed）；不由第一个用户
turn 隐式触发正常开户；`suspended`/`revoked` SHALL NOT 删除 partition 或历史数据，`revoked`
SHALL 拒绝新凭据签发与新会话。

#### Scenario: provision 失败后 retry 不产生第二个 agent

- **WHEN** provisioning job 执行失败后管理员 retry
- **THEN** 同一 job 行推进（attempt 递增），tenant/conversation 与失败前一致，账号最终 `active`

#### Scenario: 进程重启后 pending job 被恢复

- **WHEN** 存在 pending/running provisioning job 时进程重启
- **THEN** 启动扫描重新入队并最终收束，账号进入 `active` 或 `failed`（对管理员可见原因）

### Requirement: WebSocket 握手认证

系统 SHALL 在 WebSocket 通道 `accept` 之前校验连接凭据，校验对象 SHALL 与 HTTP 面同一套：普通用户会话 Cookie（`__Host-nexus_session`）与 Origin allowlist。校验失败 SHALL NOT 完成 `accept`、SHALL NOT 提交任何入站消息。拒绝 SHALL 只区分「凭据」与「来源」两类，SHALL NOT 回显会话不存在/过期/已撤销/账号被封等具体原因。校验 SHALL 使用与 HTTP 面相同的 session 有效性判定（含 timeout、撤销、账号状态），SHALL NOT 维护第二套会话状态。

#### Scenario: 无有效会话的连接被拒绝

- **WHEN** 客户端发起 WebSocket 握手但未携带 `__Host-nexus_session`，或该会话不存在/已过期/已撤销/所属账号已 suspended
- **THEN** 握手以「凭据」类协议级拒绝关闭，未完成 `accept`，且不产生任何入站消息

#### Scenario: Origin 不在白名单的连接被拒绝

- **WHEN** 握手携带有效会话，但 Origin 不在 `origin_allowlist` 内（或缺失）
- **THEN** 握手以「来源」类协议级拒绝关闭，不完成 `accept`，且不产生任何入站消息

#### Scenario: 有效会话与合法来源的握手成功

- **WHEN** 握手携带有效会话且 Origin 在 allowlist 内
- **THEN** 握手完成 `accept`，后续 `hello` 返回按该会话派生的服务端身份三元组

#### Scenario: 拒绝不泄露账户状态

- **WHEN** 分别以「会话不存在」「会话已撤销」「账号已 suspended」三种情形发起握手
- **THEN** 三者得到同一类拒绝语义，响应中不出现可区分三者的字段或文案

#### Scenario: 未启用认证的实例不引入握手门禁

- **WHEN** 实例未启用 `[auth]`（dev-only 路径）
- **THEN** 握手不执行凭据校验，行为与本增量引入前一致（dev 回退身份），且该路径 SHALL NOT 在启用认证的实例上生效
