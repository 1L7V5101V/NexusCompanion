# auth-provisioning Specification

## Purpose
定义 Pilot 受邀用户认证（邀请 Token 一次性兑换 → 登录会话）、单一 admin principal
（bootstrap/轮换/撤销）、账号 provisioning/readiness 生命周期，以及浏览器安全边界
（Cookie/CSRF/Origin/401-403）的行为契约。attachment 授权（C6）、工具 allowlist（C7）、
Telegram binding（C10）、schedule（C11）、WebChat 协议帧契约（C4）不在本规格范围。

## Requirements

### Requirement: 邀请 Token 一次性原子兑换

系统 SHALL 支持签发一次性邀请 Token（仅存 HMAC-SHA-256-with-pepper digest），并在单个数据库
事务中完成「锁定 Token 行 → 校验未消费/未撤销/未过期且账号 active → 标记已消费 → 创建登录
会话」。并发兑换同一 Token SHALL 恰好一个成功；ready（账号非 `active`）前 SHALL NOT 签发
或兑换 Token。明文凭据 SHALL NOT 出现在数据库、日志、审计或前端持久存储。

#### Scenario: 并发兑换同一 Token 仅一个成功

- **WHEN** 多个客户端并发用同一邀请 Token 调用兑换
- **THEN** 恰好一个请求成功并获得登录会话，其余请求以认证错误失败，且系统只创建一个会话行

#### Scenario: 非 active 账号的 Token 不可兑换

- **WHEN** 账号状态为 `provisioning`/`suspended`/`revoked` 时尝试兑换其名下邀请 Token
- **THEN** 兑换失败并返回认证错误，不创建会话

#### Scenario: 明文凭据不落库

- **WHEN** 检查 `access_tokens` 与 `auth_sessions` 表及全部日志/审计输出
- **THEN** 只存在 64 字符 HMAC digest，不存在任何 `nxt_`/`nad_`/`ns_` 前缀明文凭据

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
