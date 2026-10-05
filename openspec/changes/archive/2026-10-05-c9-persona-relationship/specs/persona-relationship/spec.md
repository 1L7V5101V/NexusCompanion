## Purpose

定义 tenant 独立 PersonaProfile / RelationshipState 的行为契约：一次性 onboarding
与提交后锁定、PG 当前值存储与恢复、RelationshipState 单写者原地演化与生效时机、
prompt 四层组装与 source breakdown 调试权限、Persona 审计、无 revision 链约束。
依据 PILOT_ROADMAP §5.7（全节）、§5.9.8、§10 DECIDED（Persona 系列六条）冻结。

## ADDED Requirements

### Requirement: 一次性人设设置流程（onboarding）

每个 tenant 的用户 SHALL 在首次进入时经历一次性人设设置流程：从管理员提供的
可选 Persona 模板中选择，或提交完整自由文本（identity / personality_rules /
self_model 三块，沿用当前单体自由文本语义）。提交 SHALL 原子保存为该 tenant 的
PersonaProfile 快照与初始 RelationshipState，此后用户侧 SHALL NOT 存在任何修改
PersonaProfile 的入口（API 与 UI 双重缺失）。未完成 onboarding 的 tenant SHALL
在每次进入时被引导至 onboarding 流程。

#### Scenario: 提交后用户侧无修改入口

- **WHEN** 用户完成 onboarding 提交后再次访问任何用户侧页面或 API
- **THEN** 不存在修改 PersonaProfile 的界面元素；直接调用修改类 API 返回 403/404 语义

#### Scenario: 模板选择与自由文本等价可用

- **WHEN** 管理员已发布至少一个启用状态的 Persona 模板，且另一 tenant 直接编辑自由文本提交
- **THEN** 两条路径均能完成 onboarding，产物为各自 tenant 独立快照，内容互不可见

#### Scenario: 模板变更不覆盖已有 tenant

- **WHEN** 管理员修改或停用某个 Persona 模板
- **THEN** 已完成 onboarding 的 tenant 快照内容保持不变，仅影响后续 onboarding

### Requirement: PersonaProfile 提交后固定（current-state 模型）

PersonaProfile SHALL 为 tenant 当前固定值：提交后系统任何路径（用户、系统、
optimizer）SHALL NOT 修改其正文。系统 SHALL NOT 建设 Persona/Relationship 的
产品级 revision 链、版本表或 CAS 机制；异常恢复 SHALL 走 PostgreSQL 备份/PITR，
SHALL NOT 提供产品内 revision 浏览或回滚入口。

#### Scenario: 无版本表

- **WHEN** 检查数据库 schema 与代码
- **THEN** 不存在 persona/relationship 的 revision/version/history 表，也无按版本切换正文的代码路径

#### Scenario: 提交后正文不可变

- **WHEN** 后续 turn、optimizer、管理员操作发生
- **THEN** 该 tenant 的 PersonaProfile 正文保持与提交时逐字一致

### Requirement: RelationshipState 按单体语义原地演化且租户隔离

RelationshipState SHALL 保存该 tenant 的当前可演化值，沿用当前单体
`read_self() → optimizer 计算 → write_self(updated)` 的原地覆盖语义：数据库中
直接覆盖该 tenant 当前状态，SHALL NOT 产生历史版本链。更新 SHALL 在该 tenant
串行 lane（maintenance lock）内以单事务完成，保证并发 optimizer 写不互相覆盖。
每个 tenant 的 PersonaProfile 与 RelationshipState SHALL 严格按 tenant 隔离：
主对话、Proactive、Drift、optimizer SHALL NOT 读取其他 tenant 的人设或关系状态。
同一 tenant 的更新 SHALL 写入 Persona 审计记录（来源、触发 turn、时间），但不
复制保存每一版完整正文。

#### Scenario: 并发写不丢失

- **WHEN** 同一 tenant 的两次 RelationshipState 更新在串行 lane 约束下先后执行
- **THEN** 最终状态为后一次更新的完整结果，无部分写入或交错覆盖

#### Scenario: 跨 tenant 不串

- **WHEN** tenant A 与 tenant B 均已完成 onboarding，A 的主对话 / Proactive / Drift 组装 prompt
- **THEN** 注入的是 A 自己的 PersonaProfile 与 RelationshipState，B 的内容不出现在任何 prompt、日志或调试输出中

### Requirement: Persona/Relationship 生效时机

RelationshipState 更新成功 SHALL 从该 tenant 的下一轮 turn 开始注入新内容；
正在进行中的 turn SHALL 继续使用其组装时的 prompt snapshot，不中途替换。
PersonaProfile 在 onboarding 提交后 SHALL 立即对下一轮 turn 生效且保持固定。

#### Scenario: 进行中 turn 不受更新影响

- **WHEN** 某轮 turn 执行期间 optimizer 更新了 RelationshipState
- **THEN** 该轮 prompt 仍为组装时快照，更新内容出现在该 tenant 下一轮 turn

### Requirement: Prompt 四层来源与 source breakdown 调试权限

最终 prompt SHALL 由四类可识别来源组装：RuntimeInvariant（代码维护的运行时
不可违反规则）、PersonaProfile（tenant 固定人设）、RelationshipState（tenant
演化关系状态）、ChannelPolicy（channel 渲染与传输约束）。系统 SHALL 提供
prompt source breakdown 调试清单（仅显示区块来源与允许披露的内容），该清单
SHALL 仅对 admin/debug 模式开放，普通用户请求 SHALL 拿不到它；breakdown
SHALL NOT 展示模型隐藏推理或思维链。

#### Scenario: 普通用户不可获取 breakdown

- **WHEN** 普通测试用户请求任何包含 prompt source breakdown 的接口或页面
- **THEN** 请求被拒绝（403/404 语义），响应与普通响应无差别

#### Scenario: admin 可看来源清单

- **WHEN** admin/debug 模式请求 breakdown
- **THEN** 返回四层来源的区块清单（来源类别、字符预算等 metadata），不含隐藏推理内容

### Requirement: PG 当前值存储、恢复与配置边界

PersonaProfile、RelationshipState 与 persona_templates SHALL 以 PostgreSQL 为
规范 source of truth（按 tenant_id / 模板 id 隔离）；进程重启后 SHALL 能从 PG
正确恢复当前值。`config.toml` 的 `[agent.persona]` SHALL 只承担实例默认
persona（单体兼容）与迁移 seed 角色，SHALL NOT 被多租户运行时读取为 tenant
当前值；单体 / SQLite dev 模式 SHALL 继续沿用现有文件语义（行为不变）。
单写者路径 SHALL 使用受限数据库权限与 tenant scope 过滤。

#### Scenario: 重启恢复

- **WHEN** 进程重启后同一 tenant 发起新 turn
- **THEN** 注入的 PersonaProfile 与 RelationshipState 与重启前当前值一致

#### Scenario: config 不承载 tenant 当前值

- **WHEN** 修改 `config.toml [agent.persona]` 后（多租户 PG 模式下）重启
- **THEN** 已完成 onboarding 的 tenant 快照不变（config 值仅作为新 tenant 的 seed 或单体兼容默认）
