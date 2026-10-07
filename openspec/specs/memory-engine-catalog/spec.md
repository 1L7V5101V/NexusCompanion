# memory-engine-catalog Specification

## Purpose

定义 tenant memory engine 目录/绑定与 WebChat 选择器的行为契约：服务端允许目录、
每 tenant 单 active binding（初始 `default`）、`tenant_policy_revision` 提升语义、
切换生效时机（下个 work 生效、进行中 work 不换引擎）、切换不迁移/删除旧引擎数据、
active engine 独占 ingest、inspector admin-only、与 C13 解耦。依据 PILOT_ROADMAP
§4.3（memory engine 插件与 WebChat 选择）、§4.2（tenant 正式 turn 从 binding 解析
active engine）、§5.9.16（memory engine 插件固定表 + provisioning/运行时解析规则）、
§10 DECIDED（memory engine slot = required、首版 default/rachael、切换只影响后续
work）冻结。

## Requirements

### Requirement: 服务端允许目录

memory engine 目录 SHALL 由服务端冻结（Pilot 固定为 `default` 与 `rachael` 两项，
binding_policy 按 §5.9.16 固定表）；`GET /api/memory/engines` SHALL 只返回服务端
目录及各项的能力/ready 状态/当前 active，SHALL NOT 信任任何客户端字段扩充或修改
目录。default-memory inspector SHALL NOT 出现在目录中（admin observability
contribution，非普通 tenant 可选能力）。

#### Scenario: 目录来自服务端且不含 inspector

- **WHEN** 已认证用户请求 `GET /api/memory/engines`
- **THEN** 响应的目录项只含服务端冻结的引擎集合（`default`、`rachael`），每项带
  能力描述与 ready 状态，不含 inspector；请求携带的任何查询参数都不能使目录之外
  的引擎出现

#### Scenario: 客户端字段只是设置请求

- **WHEN** `PUT /api/memory/engines/active` 请求体携带目录之外的 engine_id 或
  额外字段（如 capability/binding_policy 声明）
- **THEN** 服务端只读取 `engine_id` 并与冻结目录比对，目录外的值一律拒绝，额外
  字段不产生任何授权效果

### Requirement: 切换授权与校验

`PUT /api/memory/engines/active` SHALL 在服务端校验：目标 engine 在目录内、当前
tenant policy 允许用户侧切换（opt-in 未放行/总开关关闭时拒绝）、engine 已在本进程
构建（ready）。任一不满足 SHALL 拒绝且不产生任何持久化变更；认证失败/身份不可派
生 SHALL 拒绝。同 engine 的重复提交 SHALL 幂等成功且 SHALL NOT 提升 revision。

#### Scenario: 未授权切换被拒（目录外 / not ready / 不允许用户侧切换）

- **WHEN** 用户分别提交目录外 engine_id、未构建（not ready）的引擎、或 opt-in 未
  放行/用户侧切换关闭时的引擎
- **THEN** 三种请求均被拒绝（各自机器可读错误码），binding 行与 revision 保持
  不变

#### Scenario: 同 engine 重复提交幂等

- **WHEN** 用户提交的 engine_id 与当前 active engine 相同
- **THEN** 请求成功返回当前状态，binding 行不变、revision 不提升、不写切换事件

### Requirement: active binding 持久化与 revision 提升

active binding SHALL 持久化到 PostgreSQL（`tenant_memory_engine_bindings`）；每次
成功的引擎切换 SHALL 使该 tenant 的 `tenant_policy_revision` 恰好 +1 并记录切换
事件；revision SHALL 随 binding 一起可读，供 work-start 解析与 `TenantRuntimePlan`
（`engine_binding` + revision）消费。

#### Scenario: 切换持久化并提升 revision

- **WHEN** tenant 的 active engine 从 `default` 切换为 `rachael`（校验通过）
- **THEN** binding 行更新为新 engine_id，`tenant_policy_revision` 恰好 +1，事件表
  新增一条 switch 记录；服务重启后 binding 仍生效（PG 为 source of truth）

### Requirement: 单 active 约束与初始绑定

每个 tenant SHALL 始终有且只有一个 active engine（数据库层以 `tenant_id` 为主键
强制）；tenant 创建（provisioning）时 SHALL 建立初始绑定 `default`；存量无绑定
tenant SHALL 由懒补齐建立同样的初始绑定，SHALL NOT 出现无 active engine 的 tenant
被运行时解析成功的情况。

#### Scenario: provisioning 建初始绑定且重复 ensure 幂等

- **WHEN** 新 tenant 完成 provisioning，或对已有绑定的 tenant 再次执行 ensure
- **THEN** binding 为 `default` 且 tenant 在 binding 表中恰有一行；重复 ensure 不
  产生第二行、不提升 revision

#### Scenario: 并发 ensure/switch 不破坏单 active

- **WHEN** 并发对同一 tenant 执行 ensure 或切换
- **THEN** 数据库约束保证该 tenant 始终至多一行 binding，竞态失败方幂等回读既有
  状态

### Requirement: 切换生效时机

引擎选择 SHALL 在 work start 解析一次并固定到该 work（进行中的 work 保持原
engine，不中途切换）；revision/binding 的变更 SHALL 只对下一个 work 生效。检索
与该 work 内的记忆工具调用 SHALL 使用同一 active engine。

#### Scenario: 进行中 work 不换引擎

- **WHEN** 某 work 执行期间该 tenant 的 active binding 被切换
- **THEN** 该 work 的检索与记忆工具调用继续使用原 engine

#### Scenario: 下一个 work 取新引擎

- **WHEN** 切换完成后的下一个 work 启动
- **THEN** work-start 解析得到新 engine，检索与记忆工具调用使用新 engine

### Requirement: 切换数据隔离与可追踪

引擎切换 SHALL NOT 迁移、合并或删除任何旧引擎数据；记忆记录 SHALL 按
`tenant_id + engine_id` 可追踪（binding/事件历史落库；各引擎既有存储保持
tenant 作用域不变）；TurnCommitted/ConsolidationCommitted 的自动记忆写入 SHALL
由该 tenant 当前 active engine 独占处理。

#### Scenario: 切换后旧引擎数据原样保留

- **WHEN** tenant 从 `default` 切换到 `rachael` 并在新引擎下产生新记忆
- **THEN** 旧引擎（`default`）既有数据行不变、不删除；事件表可按
  `(tenant_id, engine_id)` 查出 initial/switch 历史

#### Scenario: 非 active 引擎不写入

- **WHEN** tenant 的 active engine 为 `rachael` 时产生 TurnCommitted 事件
- **THEN** `default` 引擎不处理该 tenant 的该事件（无自动记忆写入），反之亦然

### Requirement: 与 C13 召回质量改造解耦

本 capability SHALL NOT 依赖 BM25/ParadeDB/pg_search/jieba/reranker（C13 范围）：
实现与测试 SHALL 在不安装 C13 产物的构建上通过；目录/绑定/切换语义 SHALL NOT 因
C13 落地而变更。

#### Scenario: 依赖范围检查

- **WHEN** 对本 capability 新增模块做依赖范围检查（导入 grep + 构建测试）
- **THEN** 无 BM25/ParadeDB/pg_search/jieba/reranker 相关导入或配置依赖，测试不
  依赖 pg_search 扩展即可通过
