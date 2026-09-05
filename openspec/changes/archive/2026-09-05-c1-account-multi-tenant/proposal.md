# C1 扩展:一个账号拥有多个 tenant(每个 tenant 一个 agent / canonical conversation)

> 对应任务计划:`openspec/openspec-tasks-bundle/task-01-canonical-identity.md`(PILOT_ROADMAP §5.9.10 第 1 项)。本 change 是 C1(canonical-identity) **verified 后的 follow-up**:不改 C1 已归档历史(archive 是审计),归档后同步修正主 spec 的 1:1 措辞。
> 输入的已冻结决策(§5.9.2 / §5.9.9 / §10 DECIDED)不在此重复论证,design.md 逐条引用。

## Why

Pilot 需要一个账号拥有**多个不同记忆 / persona 的 agent**。C1 把模型冻结成
`1 test_account : 1 tenant_id`(账号表内嵌全局唯一 tenant 列),在结构上把一个
账号限死为一个资源域,无法表达「同一登录主体下多个 agent,各自独立的记忆 /
persona」。记忆 / persona 是 tenant 域内的(C9),因此「多个 agent」在身份层就是
「多个 tenant」。当前 schema 预 cutover(仅 dev seed + scratch DB),是 reshape 的
最便宜窗口:没有生产行需要迁移,改表即改模型。

选定方案甲「会话即 agent」:账号行去掉内嵌 `tenant_id`(它是 1:1 的唯一来源);
tenant 落到 `canonical_conversations` 行上——该表本就 `tenant_id UNIQUE +
account_id FK`,天然支持一账号多行。建 agent = 给账号加一条会话行。明确**不做一个
tenant 多会话**(一个 agent 的资源域内只有一个规范会话,消息流 / 记忆都挂它)。

## What Changes

- **schema**:新 Alembic migration(`c4d8f2a6e9b3`,下行 `e2b4d6f8a0c2`)移除
  `test_accounts.tenant_id` 列与 `uq_test_accounts_tenant_id` 唯一约束;tenant 全局
  唯一性收敛到 `canonical_conversations.tenant_id`(该约束保持)。
- **模型** `bootstrap/db/models/canonical.py`:删 `TestAccountModel.tenant_id`;
  `CanonicalConversationModel` 语义明确为「一行 = 一个 agent(tenant 资源域),账号
  可拥有多行」。
- **repository** `canonical_repo.py`:
  - `create_account`(按账号 id 幂等);
  - `create_agent(account_id, tenant_id)`(按 tenant 幂等,tenant 已属其它账号 →
    `TenantAlreadyBoundError` fail-closed,跨账号占用拒绝);
  - `provision_account_with_agent(...)`(单事务 = 账号 + 首个 agent,**替代**旧
    `create_account_with_conversation`,旧名在新模型下误导);
  - `list_conversations_by_account`(created_at / id 升序,账号级确定性枚举);
  - 删 `get_account_by_tenant`(账号→唯一 tenant 的查询随模型消失)。
- **resolver** `bootstrap/identity.py`:解析单元是 tenant——`resolve_by_tenant` 恒
  产出单一归属三元组(conversation→account);删除单三元组 `resolve_by_account`,
  改为 `list_agents(account_id)` 产出该账号 0..N 个 agent 三元组(空列表是合法
  状态;未知 / 空账号仍 fail-closed)。
- **测试 / fixture / 静态契约**:C1 集成测试对齐新 API;fixture
  `canonical_identity_chain.json` 升 version=2,账号级正向用例改「账号 + agent
  列表」;新增「一账号两个 agent 各自独立」正用例与「同 tenant 跨账号占用被拒」
  负用例。
- **spec delta**:`specs/canonical-identity/spec.md` 完整新版 capability spec,
  归档后同步覆盖 `openspec/specs/canonical-identity/spec.md`。
- **任务文档**:task-01 顶部注记(主 spec 1:1 措辞待本 change 归档后修正);
  task-07 补 ToolExecutionContext 结论与 account→N 模型注记。

## Capabilities

### Modified Capabilities

- `canonical-identity`:「规范身份三元组唯一」(1 test_account : 1 tenant)重写为
  account→N tenant + tenant↔canonical conversation 1:1;fail-closed 与
  per-conversation sequence 语义不变。

## Non-Goals(明确不做)

- **不做一 tenant 多会话**:每个 agent = tenant = 恰好一个 canonical conversation。
- **不改 C1 已归档 change**:`openspec/changes/archive/2026-09-05-c1-canonical-identity/`
  保持历史原样。
- **不建新 task-XX 编号**:本 change 是 task-01(canonical-identity)的 follow-up。
- **不接运行时**:identity resolver 仍不被 runtime wire;不改旧单体
  `channel:chat_id` 派生(`infra/storage/tenancy.py`、`session_key`)。
- **不建 C2/C3/C5/C9/C10 的任何表或逻辑**:inbox/turn/admission/auth/binding/
  persona/relationship 均不在本 change。
- **不动 `canonical_messages`**:消息结构、per-conversation sequence 事务原子分配
  语义不变。
- **不做 per-agent 记忆 / persona 实现**:那是 C9 的 tenant 域内容。

## Impact

- **代码**:`bootstrap/db/models/canonical.py`、`bootstrap/db/repository/canonical_repo.py`、
  `bootstrap/identity.py`、新增 `alembic/versions/c4d8f2a6e9b3_c1_account_n_tenants.py`。
- **配置**:无新配置项。
- **依赖**:无新增第三方依赖。
- **测试**:`tests/canonical_identity/`(PG 集成)+ `tests/test_canonical_identity_contract.py`
  (静态契约)+ `tests/fixtures/canonical_identity_chain.json`(version=2)。
- **系统/运维**:C1 未 cutover,升级 = 删列无生产数据迁移;rollback = 关闭入口 +
  downgrade(每账号恰一条会话时一步回填;多 agent 需人工合并,见 design.md
  ADR-5)。
- **主 spec**:归档后同步修正 `openspec/specs/canonical-identity/spec.md`。
