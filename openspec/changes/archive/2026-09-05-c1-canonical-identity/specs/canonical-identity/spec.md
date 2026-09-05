# canonical-identity 增量规格

## Purpose

定义 Pilot 服务端身份链（account → tenant → canonical conversation）、canonical message 流的 per-conversation 0-based 序号分配、以及无绑定请求 fail-closed 拒绝语义的行为契约。旧单体 `channel:chat_id` 身份与 SQLite 存储不在本规格范围（独立 legacy 路径）。

## ADDED Requirements

### Requirement: 规范身份三元组唯一

系统 SHALL 保证一个测试账号对应恰好一个 `tenant_id`，一个 `tenant_id` 对应恰好一个规范会话；该唯一性 SHALL 由数据库唯一约束强制，任何重复创建 SHALL 被数据库拒绝。

#### Scenario: 重复 tenant_id 创建账号被拒绝

- **WHEN** 向账号表插入一行，其 `tenant_id` 与已有账号相同
- **THEN** 数据库以唯一约束错误拒绝该插入，已有账号不受影响

#### Scenario: 重复 tenant_id 创建规范会话被拒绝

- **WHEN** 向规范会话表插入一行，其 `tenant_id` 与已有会话相同
- **THEN** 数据库以唯一约束错误拒绝该插入

#### Scenario: 1:1 派生链可解析

- **WHEN** 以已存在的可信账号标识解析身份
- **THEN** 返回的三元组（account、tenant、canonical conversation）满足：账号的 tenant 与会话的 tenant 一致，会话归属该账号

### Requirement: 无绑定请求 fail-closed 拒绝

对无法在服务端身份表中找到对应已验证 principal 的解析请求，系统 SHALL 以明确错误拒绝，SHALL NOT 返回任何默认租户身份，SHALL NOT 写入任何数据。

#### Scenario: 未知账号标识被拒绝

- **WHEN** 以不存在的账号标识请求解析身份
- **THEN** 解析失败并抛出身份解析错误，错误结果中不出现任何默认租户标识

#### Scenario: 未知 tenant 的会话追加被拒绝且零写入

- **WHEN** 对不存在的规范会话请求按序追加消息
- **THEN** 操作失败，消息表与计数器状态均无任何变更

#### Scenario: 空 principal 被拒绝

- **WHEN** 以空白标识请求解析身份
- **THEN** 解析立即失败，不发起任何数据库查询以外的副作用

### Requirement: per-conversation 0-based 原子序号分配

系统 SHALL 在单个 PostgreSQL 事务内原子地分配规范消息序号并写入消息行；序号 SHALL 为每个规范会话独立、从 0 开始、连续、`BIGINT` 范围；并发追加 SHALL NOT 产生重复或跳跃的序号；会话间分配 SHALL 互不阻塞。

#### Scenario: 首条消息序号为 0

- **WHEN** 向一个空规范会话追加第一条消息
- **THEN** 该消息的序号为 0，会话的下一序号计数变为 1

#### Scenario: 并发追加序号唯一、连续、跨会话独立

- **WHEN** 并发地向会话 A 与会话 B 各追加多条消息
- **THEN** A 与 B 的序号集合各自为 0..N-1 且无重复，B 从 0 开始不受 A 计数影响，`(会话, 序号)` 无任何冲突

#### Scenario: 追加失败不留序号空洞

- **WHEN** 序号已分配但消息行写入在同一事务中失败（如违反约束）
- **THEN** 整个事务回滚，会话计数器保持分配前的值，下一次追加重用该序号

### Requirement: 规范消息流可按序读回

系统 SHALL 支持按序号升序读取某规范会话的消息全量或游标之后的部分，供断线补拉与重放消费；读取 SHALL 以可信租户上下文过滤，其它租户 SHALL NOT 可见。

#### Scenario: 按序读回与游标补拉

- **WHEN** 会话已有 0..N-1 共 N 条消息，以「最后看到的序号 K」请求补拉
- **THEN** 返回序号 K+1 及之后的消息且按序号升序排列

#### Scenario: 跨租户不可见

- **WHEN** 以租户 X 的上下文请求租户 Y 会话的消息或身份
- **THEN** 返回为空或失败，不泄露租户 Y 的任何行

### Requirement: 规范身份的数据库防御纵深

消息与会话的租户归属 SHALL 落在行上；对规范消息的所有读写 SHALL 携带可信租户过滤条件；表结构 SHALL 以唯一约束、外键与状态 CHECK 约束保证归属与枚举合法，历史数据 SHALL NOT 被级联物理删除。

#### Scenario: 非法角色被拒绝

- **WHEN** 向消息表插入不在允许角色枚举内的行
- **THEN** 数据库以 CHECK 约束错误拒绝

#### Scenario: 会话不可脱离账号存在

- **WHEN** 尝试插入引用不存在账号的规范会话，或引用不存在会话的消息
- **THEN** 数据库以外键约束错误拒绝

### Requirement: 旧单体数据隔离

Pilot 规范存储 SHALL NOT 从旧单体 SQLite 导入任何会话、消息、记忆或身份数据；SHALL NOT 在规范查询失败时回退读取旧 SQLite；SHALL NOT 向旧库反向写入；新账号的规范会话 SHALL 从空历史开始。

#### Scenario: 新账号空历史

- **WHEN** 通过 provisioning 派生链创建全新测试账号并解析其规范会话
- **THEN** 该会话的消息数为 0，且不存在任何来自旧单体 `channel:chat_id` 会话的映射记录

#### Scenario: 规范路径无 SQLite 回退代码

- **WHEN** 静态扫描规范身份与规范消息的实现模块
- **THEN** 不存在任何 SQLite 连接、SQLite 文件路径或「查询失败改读旧库」的分支
