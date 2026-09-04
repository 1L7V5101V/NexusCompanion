# Task-14 — memory engine plugin catalog/binding + tenant 选择 + WebChat selector（memory-engine-catalog）

> 编号对应 PILOT_ROADMAP §5.9.10 第 14 项。状态标记复用 §8。

## 元数据

- **所属阶段**：主要里程碑 = P1
- **§5.9 引用**：§4.3（memory engine 插件与 WebChat 选择）、§5.9.16（engine slot 表/binding policy：memory engine 插件固定表）、§4.4（engine 契约）、§10 DECIDED（RuntimeSnapshot/hooks/revocation 中 memory engine slot 部分）
- **§6 出口条件引用**：P1 出口「WebChat 提供 memory engine selector：读取当前 tenant 被允许的 engine、展示能力/状态、提交 default 或 rachael 的选择；服务端校验 tenant binding 和 engine readiness，选择结果持久化到 PostgreSQL，切换只对下一次 work 生效」
- **状态**：planned

## 目标

落地 tenant memory-engine catalog/binding：`default` 为 `required` slot 初始绑定、`rachael` 为已安装 `opt_in`/`default_on` 可选实现（§5.9.16 固定表）；`GET/PUT /api/memory/engines`（只读 catalog 来自服务端允许目录，客户端字段**只是设置请求**）；WebChat selector UI（展示能力/状态/提交选择）；`tenant_id + engine_id` 命名空间（memory/KV/credential/scope/config，禁止 `DEFAULT_TENANT` fallback）；active binding 持久化到 PG + 每次切换提升 `tenant_policy_revision`、**下个 work** 生效（进行中 work 保持原 plan，不中途切引擎）；切换不迁移/不删除旧引擎数据；每个 tenant 始终且仅有一个 active engine；**不依赖 BM25/ParadeDB/jieba/reranker**（与 C13 解耦，D7）；inspector 为 admin-only（非普通 tenant 开关）。

## 输入

- 上游 change 产出：C1（tenant identity，D7）、C4（WebChat 前端，D7）、C5（WebChat auth，D7）、C8（RuntimeSnapshot + tenant plugin catalog，D7）
- roadmap 冻结决策：§4.3（WebChat 选择）、§5.9.16 memory engine 插件固定表（6 行能力项 + 6 条 provisioning/运行时解析规则）、§10 DECIDED（memory engine slot = required、首版 default/rachael、active engine 进入 TenantRuntimePlan、切换只影响后续 work）
- 现有代码锚点：`plugins/default_memory/*`（memory_plugin.py = engine infrastructure；plugin.py = recall inspector）、`plugins/default_memory/engine.py`（可消费 C13 改造，可选）
- 依赖前置：C1 + C4 + C5 + C8（D7）

## 输出

- DB schema：catalog/binding 表（tenant memory engine binding + policy revision）+ 迁移
- 端点：`GET /api/memory/engines`（服务端允许目录 + 能力/状态）、`PUT /api/memory/engines/active`（提交选择，服务端校验绑定与 readiness）
- 前端：WebChat selector UI
- 契约：`tenant_id + engine_id` 命名空间隔离、binding_policy 元数据（required/default_on/opt_in）、切换提升 revision 语义
- 测试/证据：API 测试、负向测试（未授权切换）、revision 提升测试、生效时机测试、数据隔离测试、单 active 约束测试、依赖范围检查、权限测试

## 验收标准

- [ ] 只读 catalog 来自服务端允许目录（客户端字段只是设置请求） — 验证：API 测试
- [ ] 未授权 engine 切换被拒（不在 tenant catalog / not ready / 不允许用户侧切换） — 验证：负向测试
- [ ] active binding 持久化到 PG + 提升 `tenant_policy_revision` — 验证：revision 提升测试
- [ ] 进行中 work 不换引擎；下一 work 取新 `TenantRuntimePlan` 后生效 — 验证：生效时机测试
- [ ] 切换不迁移/合并/删除旧引擎数据；记录按 `tenant_id + engine_id` 可追踪 — 验证：数据隔离测试
- [ ] 每个 tenant 始终有且只有一个 active engine；初始绑定为 `default` — 验证：单 active 约束测试
- [ ] 不依赖 BM25/ParadeDB/jieba/reranker（与 C13 解耦） — 验证：依赖范围检查（grep 无滥用导入 + 构建/测试不依赖 pg_search 亦可过）
- [ ] inspector（default-memory inspector）标为 admin-only，非普通 tenant 开关 — 验证：权限测试
- [ ] 本 task 不触碰召回链路改造（C13）与 RuntimeSnapshot 生成（C8） — 验证：PR diff 范围检查

> 判定「真正完成」而非「执行过」：「未授权切换被拒」「进行中 work 不换引擎」「单 active 约束」是三条关键负向/约束断言；「不依赖 C13」以依赖范围检查（构建与测试证明）为准，不能只写文档声明。

## 独立性边界（不与其他任务重复）

- 本任务拥有：catalog/binding 表、selector API、WebChat selector UI、`tenant_id + engine_id` 命名空间、revision 提升语义
- 本任务不触碰：召回链路改造（C13，解耦）、RuntimeSnapshot 生成（C8，只消费 tenant plugin catalog）、memory_plugin.py/plugin.py 内部（C8 边界内的 engine runtime/inspector 归属）
- 共享 seam 协议：消费 C8 的 engine slot 元数据（binding_policy 表）与 `TenantRuntimePlan`；消费 C1 tenant 派生；经 C4 前端承载 selector；经 C5 auth 校验 principal

## 依赖

- **左依赖（必须先完成）**：C1（D7：tenant identity）+ C4（D7：WebChat 前端）+ C5（D7：WebChat auth）+ C8（D7：tenant plugin catalog/engine slot）
- **右依赖（本任务前置于）**：无独立下游 change
- **可并行**：C6 / C7 / C9 / C10 / C11

## 风险与需冻结决策

- §5.9.16 / §10 DECIDED：memory engine slot 为 `required`（不能关闭，可在允许目录内切换）；`default` 初始绑定、`rachael` 管理员允许后可选；切换只影响后续 work，不迁移/删除旧数据。
- C13↔C14 解耦契约（§6.1 retrieval seam）：C14 **不强依赖** C13 改造（D7），仅 default engine 实现可消费 C13；验收第 7 条以依赖范围检查固化。
- 风险：客户端提交 engine 被当授权依据 → 验收第 1、2 条（server 允许目录 + 负向）；revision 提升遗漏 → 第 3 条强制；进行中 work 中途切引擎 → 第 4 条生效时机测试。