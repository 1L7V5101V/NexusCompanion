# canonical-identity 增量规格

account→N tenant 扩展（方案甲「会话即 agent」）：`test_accounts` 去掉内嵌 `tenant_id`，tenant 落到 `canonical_conversations` 行上。本文件为相对 `openspec/specs/canonical-identity/spec.md` 的 delta（原有 capability 增量修改）。

## REMOVED Requirements

### Requirement: 规范身份三元组唯一

## ADDED Requirements

### Requirement: 规范身份链与账号多租户

系统 SHALL 允许一个账号（登录主体）拥有多个 tenant，每个 tenant 对应恰好一个 canonical conversation（即一个 agent，各自的记忆 / persona 域在 tenant 内）；账号自身 SHALL NOT 携带 tenant；tenant 与规范会话的 1:1 以及跨账号的全局唯一 SHALL 由 `canonical_conversations.tenant_id` 唯一约束强制；同一 tenant SHALL NOT 被第二个账号占用，任何重复创建 SHALL 被数据库拒绝。

#### Scenario: 一个账号创建多个 agent

- **WHEN** 向已存在账号名下添加多个不同 tenant 的规范会话
- **THEN** 每个会话各自独立创建成功，彼此 tenant 不同、会话不同、消息流互不影响

#### Scenario: 同一 tenant 跨账号占用被拒绝

- **WHEN** 尝试为第二个账号创建已绑定到其它账号的 tenant 会话
- **THEN** 数据库以唯一约束错误拒绝，既有会话及其所属账号不受影响

#### Scenario: 同一账号重复创建同 tenant 会话被拒绝

- **WHEN** 向规范会话表插入一行，其 `tenant_id` 与已有会话相同
- **THEN** 数据库以唯一约束错误拒绝

#### Scenario: 按 tenant 解析恒为单一归属三元组

- **WHEN** 以已存在的可信 tenant 标识解析身份
- **THEN** 返回的三元组（account、tenant、canonical conversation）满足：会话确属该 tenant，且该会话的归属账号存在

#### Scenario: 账号级枚举其 agent

- **WHEN** 以已存在的可信账号标识枚举 agent
- **THEN** 返回该账号拥有的 0..N 个 agent（tenant、conversation）对，顺序确定（created_at/id 升序）；账号尚无 agent 时返回空列表（合法状态，不视为默认租户）

## MODIFIED Requirements

### Requirement: 无绑定请求 fail-closed 拒绝

对无法在服务端身份表中找到对应已验证 principal 的解析请求，系统 SHALL 以明确错误拒绝，SHALL NOT 返回任何默认租户身份，SHALL NOT 写入任何数据。已知账号但尚无 agent 只表示其 agent 列表为空，不构成任何默认身份来源。

#### Scenario: 未知账号标识被拒绝

- **WHEN** 以不存在的账号标识请求枚举其 agent
- **THEN** 枚举失败并抛出身份解析错误，错误结果中不出现任何默认租户标识

#### Scenario: 未知 tenant 的会话追加被拒绝且零写入

- **WHEN** 对不存在的规范会话请求按序追加消息
- **THEN** 操作失败，消息表与计数器状态均无任何变更

#### Scenario: 空 principal 被拒绝

- **WHEN** 以空白标识请求解析身份
- **THEN** 解析立即失败，不发起任何数据库查询以外的副作用
