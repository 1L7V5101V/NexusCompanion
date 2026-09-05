# C1 account→N tenant(account-multi-tenant)设计

> 依据 proposal.md 的 Why / What Changes;冻结语义源:PILOT_ROADMAP §5.9.2 /
> §5.9.9 / §10 DECIDED + 已归档 C1 design ADR-1..7。本文件只记录相对 C1 的增量
> 决策与不选理由。主 spec 同步前,C1 的 1:1 措辞以已验证 C1 spec 为准。

## ADR-1 账号行去掉 `tenant_id` 列,而不是只去掉它的唯一约束

账号表内嵌的 `tenant_id` 是「账号 ↔ tenant 1:1」的唯一结构来源(C1 用
`uq_test_accounts_tenant_id` 强制)。只去掉唯一约束、保留列会诱使代码继续走
「账号 → 唯一 tenant」路径,产生看似能跑、实则语义悬空的分支。

选择:列与约束一并移除。账号回归纯「登录主体」;tenant / agent 归属只由
`canonical_conversations` 行表达。repo 层同步删 `get_account_by_tenant`
(它查的就是账号→tenant 这张单射表)。

## ADR-2 方案甲「会话即 agent」,不做一 tenant 多会话

对比方案乙(一个 tenant 下多条会话 = agent 集合):

- 甲把「agent」与「资源域」重合:一个 agent 就是一条 canonical conversation
  行(自带全局唯一 `tenant_id` + `account_id`),记忆/persona(C9)与消息流都直接
  挂这个会话。乙需要额外一层「哪个会话代表哪个 agent」的绑定与每会话子域隔离,
  与「记忆/persona 是 tenant 域内」的既定模型冲突。
- 乙的用户否定项:一个 tenant 多会话意味着同域消息流混杂多个 agent,身份解析
  必须先选 agent 再选会话,徒增歧义。

选甲。建 agent = `INSERT canonical_conversations(tenant_id, account_id)`;
`uq_canonical_conversations_tenant_id` 已保证 tenant 全局唯一,数据库层零新约束。

## ADR-3 repository seam:账号 / agent 创建与 retry 语义

- `create_account`:按账号 id 幂等(`on_conflict_do_nothing` 后回查)。provisioning
  retry 用同一外部账号 id 调用时返回现有行,不产生第二个账号。
- `create_agent(account_id, tenant_id)`:按 tenant 幂等。同账号重放返回现有会话
  (不产生第二个 agent);tenant 已属**另一账号**时抛 `TenantAlreadyBoundError`
  (跨账号占用 fail-closed,不改写既有行)。账号不存在由 FK 拒绝(IntegrityError
  上抛,调用方先建账号)。
- `provision_account_with_agent`:单事务 = `create_account` + `create_agent`
  (首个 agent)。同 tenant 二次 provision 属同账号 → 返回现有状态;属其它账号 →
  `TenantAlreadyBoundError` 使整个事务回滚,**不留下孤儿账号**。
- 对比旧 `create_account_with_conversation`:旧语义「tenant 建在账号行上、会话随
  账号」在新模型下不存在,更名以消除误导。

## ADR-4 resolver:解析单元是 tenant;账号级是枚举

account→N 后「按账号解析出单一三元组」不成立。身份解析的天然单元是 tenant
(每个 agent 一个),未来 C5/C10 绑定流程是「账号 + 选中 agent」→ tenant。

- `resolve_by_tenant(tenant_id)` → 单一 `CanonicalIdentity`(conversation→account):
  这是 per-Work / 工具授权的可信入口;空 / 未知 / `default` 一律 fail-closed。
- `list_agents(account_id)` → 0..N 个三元组,`created_at / id` 升序(确定性):
  登录主体枚举其 agent。未知 / 空账号 fail-closed 抛错;已知账号尚无 agent 返回
  空列表(合法状态,不等于默认租户)。
- 单三元组 `resolve_by_account` 删除,防止调用方拿「恰好一个」当语义正确。

## ADR-5 migration upgrade / downgrade 语义(预 cutover)

- upgrade:仅删 `test_accounts.tenant_id` 及其唯一约束。dev seed 的账号行与会话行
  不受影响(会话行自带 `tenant_id='dev'`),无数据回填。
- downgrade(一步回 e2b4d6f8a0c2,重建 1:1 账号↔tenant):只对「每账号恰好一条
  会话」的数据可行,从会话行回填;存在会话数 ≠1 的账号 → `RuntimeError`
  fail-closed(多 agent 无法无损失表示,不回填猜值)。全量 rollback 的下一步即 C1
  migration 删三张表,本步忠实度边界以预 cutover dev 数据为准(验收在 downgrade
  前先 `c1_reset` 收敛到 dev 单账号)。
- 不做 Expand→backfill→cutover 双写:本 change 仍在 Create 期,直接 reshape。

## Risks / Trade-offs / 回滚

- **风险:既有 C1 测试断言 1:1(账号表有 tenant 列)** → 全量改写
  `tests/canonical_identity/` + 静态契约 + fixture(version=2),新增一账号多 agent
  正用例反向锁定新语义。
- **风险:下游(C2/C4/C5/C9/C10/C14)读旧 fixture 形状** → fixture 尚无消费方;
  version=2 + 结构自检强制账号级用例新形状。
- **风险:多 agent 账号不可一步降级** → 预 cutover 期无生产数据;downgrade 守卫
  fail-closed,验收用 dev 单账号路径;升级本身无回填、可逆性边界写入 ADR-5。
- 回滚策略:关闭入口 + `alembic downgrade c4d8f2a6e9b3@...`(单账号数据)或 PITR
  (多账号,同 C1 §10 DECIDED「rollback=关闭入口 + forward-fix/PITR」)。
