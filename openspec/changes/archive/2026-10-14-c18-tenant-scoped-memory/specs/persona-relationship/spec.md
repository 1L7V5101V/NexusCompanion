## MODIFIED Requirements

### Requirement: PG 当前值存储、恢复与配置边界

PersonaProfile、RelationshipState 与 persona_templates SHALL 以所属后端的规范表为
source of truth（按 tenant_id / 模板 id 隔离）：PostgreSQL 后端用 PG 侧的表，
SQLite / 单机 dev 后端用其库内的同名表。两种后端 SHALL 走同一套读写与审计语义，
SHALL NOT 再有一侧走文件、另一侧走表。进程重启后 SHALL 能从所属后端正确恢复当前值。

PersonaProfile 的当前值在 persona 专表内。RelationshipState 的当前值 SHALL 存放在
画像层的专表内、占一行，SHALL NOT 存放在记忆召回表内，也 SHALL NOT 存放在
persona 专表内——persona 专表里那一列自我认知文本只承载 onboarding 提交时的
seed 值，提交后固定，不是当前值。RelationshipState 的读写 SHALL 只经单写者那条缝，
画像层的组装侧 SHALL NOT 直接读它，它 SHALL 经 persona 快照交付给 prompt 组装。

`config.toml` 的 `[agent.persona]` SHALL 只承担单机固定租户的初始 seed 角色，
SHALL NOT 被任何后端的运行时读取为 tenant 当前值——seed 之后该行的当前值只由单写者
路径演化。单写者路径 SHALL 使用受限数据库权限与 tenant scope 过滤，并 SHALL 在两种
后端下同事务写入审计事件。

#### Scenario: 重启恢复

- **WHEN** 进程重启后同一 tenant 发起新 turn
- **THEN** 注入的 PersonaProfile 与 RelationshipState 与重启前当前值一致

#### Scenario: 单机 dev 重启恢复

- **WHEN** 以 SQLite 后端启动、完成一轮触发自我认知更新后重启，同一固定租户再发起一轮
- **THEN** 注入的自我认知来自 SQLite 库内画像专表的关系状态行，与重启前一致，且不读取任何 markdown 文件

#### Scenario: config 不承载 tenant 当前值

- **WHEN** 修改 `config.toml [agent.persona]` 后（任一后端）重启
- **THEN** 已完成 seed 的租户当前值不变（config 值仅在初始 seed 时生效）

#### Scenario: seed 值不冒充当前值

- **WHEN** 某租户完成 onboarding 后又经历若干轮关系状态更新，随后读取注入 prompt 的自我认知
- **THEN** 读到的是单写者路径演化后的当前值，不是 onboarding 提交时写进 persona 专表的那个 seed 值

#### Scenario: 两种后端写入留同一形态的审计

- **WHEN** 同一自我认知更新分别经 PG 后端与 SQLite 后端写入
- **THEN** 两侧都产生一条同语义的审计事件（actor、action、时间、正文字符数），且都不复制保存每一版完整正文
