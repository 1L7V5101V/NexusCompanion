## Purpose
<!-- Existing capability delta: no Purpose section -->

## MODIFIED Requirements

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

## ADDED Requirements

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