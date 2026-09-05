# observability-privacy 增量规格

## Purpose

定义 Pilot 观测采集的隐私默认边界：默认只采集结构化 lifecycle metadata；内容类数据默认关闭、debug 采集受 admin 短期审计化开关约束且入库前脱敏；metrics label 白名单；观测数据三档 retention；admin 内容访问产生审计事件；SLO 红线在基线报告前不进入实现。本规格约束所有后续 change 的观测落点（伴随落地协议）。

## ADDED Requirements

### Requirement: 默认只采集结构化 lifecycle metadata

系统默认 SHALL 只采集结构化 lifecycle metadata（内部标识、分类维度、状态、耗时、token 数、队列深度、错误类别、provider request id 摘要）；raw provider payload、完整 prompt、message content、tool args/result、attachment content SHALL 默认不进入任何日志、metrics、trace 或审计存储。

#### Scenario: 内容字段默认被拒绝

- **WHEN** 采集调用在 content capture gate 关闭（默认态）时携带内容类字段（如完整 prompt 或 tool args）
- **THEN** 该内容字段被丢弃且不写入任何持久面，采集侧记录一次内容丢弃计数，结构化 metadata 字段正常保留

#### Scenario: 进程重启后默认回到关闭态

- **WHEN** content capture gate 曾被开启，随后进程重启
- **THEN** gate 回到默认关闭态，内容字段再次被拒绝，无需任何持久化开关复位操作

### Requirement: debug 内容采集为 admin 短期审计化开关且入库前脱敏

开启内容采集 SHALL 同时满足：管理员 principal、开启原因、自动过期时间（TTL）三者齐备，且开启动作本身 SHALL 产生一条审计事件；TTL 过期后采集 SHALL 自动回到关闭态；开启期间被采集的内容 SHALL 在进入任何持久存储前完成 secret、credential、PII 与本地路径脱敏。

#### Scenario: 缺任一要素不得开启

- **WHEN** 以缺少 principal、缺少原因或缺少 TTL 的参数请求开启内容采集
- **THEN** 开启被拒绝，gate 保持关闭态，不产生审计事件

#### Scenario: 开启即审计且过采集内容已脱敏

- **WHEN** 管理员以合法 principal、原因与 TTL 开启采集并捕获一段含 API key、邮箱与本地路径的文本
- **THEN** 产生一条 action 为开启内容采集的审计事件（含 principal 与原因），且持久化的文本中不含该 API key、邮箱原文与本地路径原文，只出现类型化占位符

#### Scenario: TTL 过期自动关闭

- **WHEN** gate 开启并超过其 TTL 后再次发起内容采集
- **THEN** 内容字段被拒绝，行为与默认关闭态一致

### Requirement: metrics label 白名单

metrics 的 label 名 SHALL 来自显式白名单；account、message、tool-call 等高基数字段（如 account_id、message_id、tool_call_id、turn_id、work_id、session_key）与原始 tool args、内容字段 SHALL 不在白名单内；使用白名单之外的 label 名注册指标 SHALL 在注册时失败。

#### Scenario: 白名单外 label 注册被拒绝

- **WHEN** 以 label 名 `message_id` 或 `tool_args` 注册新指标
- **THEN** 注册失败并抛出校验错误，该指标不出现在任何导出中

#### Scenario: 白名单内 label 注册成功

- **WHEN** 以白名单内的 label 组合（如 `work_kind`、`flow`、`stage`、`tenant_id`、`channel`、`model`）注册指标
- **THEN** 注册成功且该指标可正常导出

### Requirement: 观测数据三档 retention 可配置且清理生效

观测数据 retention SHALL 按三个独立可配置类别管理：operational metadata（默认 30 天）、audit metadata（默认 180 天）、内容型 debug 数据（默认 7 天，且短于前两档）；清理任务 SHALL 幂等（重跑不重复删除、无副作用）并 SHALL 支持只报告不删除的演练模式。

#### Scenario: 过期文件被清理且未过期保留

- **WHEN** 对包含超龄 operational 文件、未超龄 operational 文件与超龄 debug 文件的目录执行清理
- **THEN** 仅超龄文件被删除，未超龄文件与 audit 类别文件（未超 180 天）保留，清理结果报告中删除与保留计数与文件实际状态一致

#### Scenario: 清理幂等

- **WHEN** 清理任务连续执行两次，期间无新文件写入
- **THEN** 第二次执行删除数为 0

### Requirement: admin 内容访问产生审计事件

管理员查看内容、跨 tenant 下钻与导出 SHALL 各自产生一条审计事件，事件 SHALL 包含操作者 principal、目标 tenant、操作类型（查看内容/下钻/导出/开启内容采集）、目标标识、原因与时间戳；审计事件 SHALL 归入 audit retention 类别。

#### Scenario: 导出产生审计事件

- **WHEN** 管理员执行一次跨 tenant 的内容导出
- **THEN** 产生一条 action 为导出的审计事件，包含操作者、目标 tenant 与原因字段

#### Scenario: 审计事件不含被查看内容

- **WHEN** 被查看或导出的内容含敏感文本
- **THEN** 审计事件本身只含 metadata 与占位符摘要，不含该敏感文本原文

### Requirement: 事件 schema 单一来源且禁止内容字段

work/turn/tool/delivery 的生命周期事件字段、派生耗时公式与聚合维度 SHALL 以共享 fixture 为单一来源（对应 roadmap §7.1）；该 fixture 与 metrics label 白名单 SHALL 交叉一致——事件身份字段（work_id、turn_id、tool_call_id、message_id、session_key、account_id）SHALL NOT 出现在 metrics label 白名单中。

#### Scenario: fixture 与 §7.1 冻结字段一致

- **WHEN** 契约测试比对 fixture 与 roadmap §7.1 的七类字段表、派生耗时公式与聚合维度
- **THEN** 全部冻结字段在 fixture 中存在且分类一致

#### Scenario: 身份字段不进 label 白名单

- **WHEN** 契约测试比对事件 schema 的身份字段集与 label 白名单
- **THEN** 任一身份字段都不在白名单内

### Requirement: 基线报告前不冻结 SLO 红线

在 Pilot 基线报告产出前，观测实现 SHALL NOT 包含任何 SLO/容量红线的硬编码判定（P95、失败率、恢复时间等数字阈值）；阈值 SHALL 在基线报告后以配置方式由后续 change 引入。

#### Scenario: 实现中无 SLO 阈值判定

- **WHEN** 静态扫描观测契约实现模块
- **THEN** 不存在以 SLO 红线为语义的数字阈值常量或阈值判定分支
