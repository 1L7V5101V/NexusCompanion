# C8 RuntimeSnapshot lease + hook failure + tenant secret/revocation — 设计

> 输入决策：PILOT_ROADMAP §5.9.16（RuntimeSnapshot/hooks/credentials/revocation 全节 + memory engine 固定表）、§5.9.7（per-task context）、§10 DECIDED（RuntimeSnapshot/hooks/revocation：P0 固化最小正确性 gate、统一 invocation seam、副作用前 revocation recheck；不建设插件安全扫描/sandbox/滚动重启）。
> 现有代码锚点：`agent/plugins/snapshot.py`（RuntimeSnapshot/Store/Lease/ContextVar 全套已存在）、`agent/plugins/manager.py`（compile/publish/drain）、`proactive_v2/loop.py`（proactive tick lease 参考实现）、`agent/plugins/jobs.py`（plugin job lease 参考实现）、`plugins/`（插件目录）。

## 1. 现状与差距

| §5.9.16 要求 | 现状 | 差距 |
| --- | --- | --- |
| work start 各取得一次 snapshot lease | proactive tick（`proactive_v2/loop.py::_tick`/`_tick_admitted`）与 plugin job（`agent/plugins/jobs.py`，lease 随 `_JobRequest` 入队）已绑定 | passive 全入口、consolidation（control 触发 + 后台 worker）、optimizer 无 lease；ProactiveLoop 生产构造未传 store（走静态回退） |
| 进行中 work 不切 snapshot | lease 绑定后 `get_current_runtime_snapshot()` 固定；`RuntimeSnapshotStore` commit 将旧快照置 `retired` 但 lease 保持可用 | 无测试证明 |
| 旧 snapshot 不绕过 suspension/revocation | 无任何 recheck 接缝 | 全缺 |
| hook policy 分层 | `ToolExecutor` pre 异常即弃（fail-closed 雏形）但无超时；post `fail_open=True` 无超时；EventBus observe 隔离但无界 | 超时边界全缺 |
| per-task tenant context（§5.9.7） | `PluginJobContext`/`HookContext` 均不携带 tenant-bound services；无 `TenantRuntimePlan` | 全缺 |
| contribution 元数据 | 无 `contribution_id`/`binding_policy`/`tenant_configurable` | 全缺 |
| tenant secret 静态加密 | 无加密原语；插件凭据明文 | 全缺 |
| compile/publish 失败保留旧 snapshot | `RuntimeSnapshotStore.begin_publish/commit/abort` 事务已存在 | 无 manager 级失败回退测试 |

Drift 说明：`DriftTurnPipeline.run` 仅由 proactive v2 runtime 路由驱动（`plugins/default_proactive/runtime.py` route=drift 分支），执行时已在 proactive tick 的 lease 与绑定内，满足 §5.9.16；不为 drift 单独取 lease（嵌套取 lease 无信息量）。audit 以测试证明该事实。

## 2. ADR 记录

### ADR-1 work-start lease 统一入口 `work_runtime_lease()`

**结论**：在 `agent/plugins/snapshot.py` 新增唯一 helper：

```python
@asynccontextmanager
async def work_runtime_lease(store: RuntimeSnapshotStore | None) -> AsyncIterator[RuntimeSnapshot | None]
```

- 语义：`await store.acquire()` → `bind_runtime_snapshot(lease)` → yield → `reset_runtime_snapshot(token)` + `lease.release()`；任一退出路径（异常/取消）都释放。
- `store is None` 时 yield None（未接线组件保持既有行为，不因 lease 缺失崩溃）——接线完整性由 audit 测试矩阵保证，不靠 helper 兜底。
- 覆盖点（每类入口一条测试）：
  - **Passive**：`AgentLoop._process_with_runtime_admission`（渠道消息经 `PassiveMessageWorker → ConversationRuntime → execute_control_turn → process_direct_message`，scheduler soft 任务经 `process_direct`，全部收束于此）；同时在 `AgentLoop.trigger_memory_consolidation`（guard/control 触发的 consolidation work）与 `MarkdownMemoryMaintenance` 后台 maintenance task 内各取一次 lease（嵌套 lease 合法：lease 只计数不互斥）。
  - **Proactive**：`ProactiveLoop._tick`/`_tick_admitted`（已有，本 change 接通生产 store 注入）。
  - **Drift**：proactive tick lease 内执行（audit 测试断言 drift 执行时 `get_current_runtime_snapshot()` 非空且为 tick lease 的 snapshot）。
  - **maintenance**：consolidation（上）+ `MemoryOptimizer.optimize`/`MemoryOptimizerLoop.run`。
  - **plugin job**：`PluginJobRuntime`（已有，保留）。
- `recovery work`（`StartupRecoveryScanner.scan()`）：启动期同步扫描、无插件/hook 交互，P0 段仅记录审计结论「不绑定」（其补偿执行闭环依赖 C2 durable 表，归 P3 演练时评估）。

**备选**：各入口直接内联 `store.acquire()+bind()` 五处重复。不选：绑定/解绑/释放的配对逻辑重复五遍，任何一处遗漏 reset 都会造成跨 task 的 ContextVar 泄漏；统一入口让 audit 测试可以直接对着 helper 断言语义。

### ADR-2 AgentLoop store 接线与生产 proactive store 注入

**结论**：

- `AgentLoopDeps` 增加 `runtime_snapshot_store: RuntimeSnapshotStore | None = None` 与 `revocation_gate: RevocationGate | None = None`；`AgentLoop` 新增 `bind_runtime_snapshot_store(store)` 公开方法——`bootstrap/tools.py` 既有 `getattr(loop, "bind_runtime_snapshot_store", None)` seam 从静默 no-op 变为真实接线（PluginManager 在 loop 之后创建，无法走构造参数）。
- `bootstrap/proactive.py::build_proactive_runtime` 显式传 `runtime_snapshot_store`，生产 proactive tick 从静态回退切到 lease 路径（同一 snapshot 内容，行为等价；`_tick` 在 store 为 None 时保留回退分支，测试与 dev 直构场景仍可用）。

**备选**：把 PluginManager 提前到 AgentLoop 之前构造以走构造参数。不选：PluginManager 依赖 `memory_runtime.engine` 与 loop 周边的构造顺序，重排接线风险大于收益；既有 getattr seam 本来就是为此预留的。

### ADR-3 RevocationGate：fail-closed 的副作用前 recheck

**结论**：新模块 `agent/admission/revocation.py`：

```python
class TenantStatus(StrEnum): ACTIVE | SUSPENDED | REVOKED | UNKNOWN
TenantStatusProvider = Callable[[str], Awaitable[TenantStatus]]

class RevocationRejected(RuntimeError): ...   # 携带 tenant_id/action/status

class RevocationGate:
    def __init__(self, provider: TenantStatusProvider | None, *, source: str = "default") -> None
    async def check(self, tenant_id: str, *, action: str) -> None  # 通过返回，否则抛 RevocationRejected
```

- 判定：`REVOKED`/`SUSPENDED` → 拒绝；`ACTIVE` → 放行；`UNKNOWN` 或 provider 抛异常 → **拒绝**（fail-closed，§5.9.16「任一无法判定时必须 fail-closed」）。
- `provider is None`（Pilot 未接账号库，C5 前的 dev 现实）：放行并记录结构化日志（`revocation_gate=dev_open`）。这是**显式声明的 dev-open 模式**，不是静默降级——gate 存在且日志可观测，C5 接线后删除该分支即全域 fail-closed。
- 检查点（全部在**当前 recheck**，不读 snapshot 捕获状态）：
  - passive work start（`_process_with_runtime_admission`，lease 之后、`_process` 之前）；
  - proactive tick start（`_tick_bound`，provisioning gate 旁）；
  - scheduler `_execute`（instant 直推 `push_tool.execute` 前——这是最纯粹的「外部副作用 + outbound delivery」）；
  - plugin job 执行前（`PluginJobRuntime._run_one`）。
- 负向测试必须证明：work 持有旧 snapshot lease 期间账号被 revoke → 副作用点 `RevocationRejected`，即「旧 snapshot 不能绕过 revocation」。
- maintenance（consolidation/optimizer）为内部写，不设 gate 检查点（suspend 语义面向 tenant-facing 副作用；账号模型落地后由 C5 扩展 provider 判定）。

**备选**：把 recheck 埋进 `MessageBus.publish_outbound`（所有出站消息统一拦）。不选：bus 无法可靠拿到 tenant→账号映射且会拦到系统公告类出站；§5.9.16 点名的是「外部副作用、schedule trigger 和 outbound delivery」三类动作点，在动作点检查语义更精确、可测试。

### ADR-4 hook failure 分层与有界 timeout

**结论**（§5.9.16「gate/interceptor fail-closed；fanout/telemetry 有界 timeout 且不反向改写已提交终态」）：

| hook 类型 | 层级 | 异常 | 超时 | 上下文缺失 |
| --- | --- | --- | --- | --- |
| `ToolExecutor` pre-tool hook（`on_tool_pre` 返回 deny/new args） | gate | deny 该次工具调用 | deny（`wait_for` 默认 5s） | deny |
| 生命周期 phase module（before_turn 等 7 槽） | gate | 异常上抛中断 turn（保持现状） | `wait_for` 超时 → 异常上抛（fail-closed） | — |
| `ToolExecutor` post_tool_use/post_tool_error | fanout | 记录失败，工具结果不变 | 每 hook `wait_for`，超时记录失败 | — |
| `EventBus.emit` intercept 链 | gate | 异常上抛（保持现状） | — | — |
| `EventBus.observe`/`fanout`/`on_any` 观察者 | telemetry | 单观察者隔离（保持现状）+ 失败记录 | 每观察者 `wait_for`，超时记录失败 | — |

- 超时值：`AgentPluginConfig.hook_timeout_seconds`（默认 5.0，`[agent.plugins]` 配置），经构造参数注入 `ToolExecutor` 与 `EventBus`，不散落常量。
- gate 超时 = fail-closed（deny / 中断 turn），fanout 超时 = 记录后继续；「不反向改写已提交终态」由 post-hook/observe 协议保证——handler 无返回值通道写回主链路（现有结构即是），测试矩阵断言终态字段不被改写。

**备选**：给 gate 也做 fail-open 重试。不选：直接违反 §5.9.16 fail-closed 冻结决策。

### ADR-5 TenantRuntimeResolver / TenantRuntimePlan / PluginInvocationContext 接缝

**结论**：新模块 `agent/plugins/tenant_plan.py`（§5.9.16「共享 PluginManager 只承载 union；每个 work 由 resolver 生成不可变 plan」）：

- `ContributionMeta`（frozen dataclass）：`contribution_id`（稳定键：module=slot、tool=tool name、job=plugin_job_key、hook=`<plugin_id>:<hook_kind>`）、`kind`（固定 hook/tool/job/module）、`plugin_id`、`binding_policy`（`required|default_on|opt_in`）、`tenant_configurable: bool`。来源：插件通过类属性 `binding_policies: Mapping[str, tuple[BindingPolicy, bool]]` 声明（未声明默认 `opt_in + tenant_configurable=True`），resolver 从 snapshot 内容派生全量索引并合并声明。
- `TenantRuntimePlan`（frozen）：`snapshot_id`、`tenant_id`、`tenant_policy_revision`、`enabled_contribution_ids: frozenset[str]`、`engine_binding: str | None`；`allows(contribution_id)` 是唯一可见性判定（未在 plan 中的 contribution 不可见/不可调用/不可隐式触发）。
- `TenantRuntimeResolver.resolve(snapshot, *, tenant_id, tenant_policy_revision, bindings, engine_binding)`：`required`/`default_on` 默认进 plan，`opt_in` 仅当 bindings 显式启用；同输入产出相同 plan（不可变、可缓存）。
- `PluginInvocationContext`（frozen）：`plan`、`work`（`WorkContext`: `work_id/turn_id/session_key/tenant_id/work_kind`）、`snapshot`；构造工厂 `PluginInvocationContext.for_work(...)`；`ContextVar` 仅观测不做授权（§5.9.16），授权判定一律读 `context.plan`。
- 本 change 交付 seam + 元数据断言 + plan 过滤/revocation 联动测试；把 plan 接进 ToolExecutor/PluginManager 的执行路径归 C7（E4）/C14（D7）消费时落地。

**备选**：现在就把 plan 过滤塞进 snapshot 编译（每个 tenant 编译一份 snapshot）。不选：§5.9.16 冻结的是「base snapshot + tenant bindings 解析 plan」，plan 解析是 per-work 轻量运算，per-tenant 编译会把 tenant 维度泄进进程级 snapshot 模型。

### ADR-6 tenant_policy_revision 来源

**结论**：resolver 参数必填，dev/单机调用方默认 `"r0"`；binding/config 的 durable 化与 revision 提升（每次 binding/config 修改 +1）归 C14/C5 的 tenant binding 存储。本 change 的 revocation 负向测试证明：即使 revision/snapshot 均为旧值，gate 仍以当前账号状态拒绝副作用。

### ADR-7 tenant secret 静态加密（§10 OPEN FOR P-1 SPEC 的算法/key source/rotation 冻结）

**结论**：

- **算法**：AES-256-GCM（`cryptography` 库 AESGCM），96-bit 随机 nonce 每次加密新生成，sealed 格式 `v1:<key_id>:<base64url(nonce)>:<base64url(ciphertext+tag)>`；`key_id` = key 的 SHA-256 前 12 hex。非对称的是：没有认证标签校验失败即解密失败（GCM 内建）。
- **key source**：workspace `keys/` 目录下每 key 一个文件 `secret_key_<key_id>`（内容为 base64url 32 字节）；`SecretBox.from_keyring_dir()` 启动时加载全部 key 文件，`active_key_id` 取 `keys/ACTIVE_KEY` 指针文件（缺省为字典序最大 key_id）。多 key 共存即 rotation：新增 key 文件 + 更新 ACTIVE_KEY 指针 → 新数据用新 key 加密、旧数据仍可解密（per-key 解密）；撤销 = 删除对应 key 文件，之后该 key 的 sealed 值解密失败。旋转/撤销的生效边界测试：撤销旧 key 后旧 sealed 值不可解、新 sealed 值用新 key。
- **接口**：`core/crypto/secret_box.py` 提供 `SecretBox.encrypt(plaintext: str) -> str` / `decrypt(sealed: str) -> str`（sealed 值自带 key_id 与版本前缀，可跨 key 校验）。tenant secret 存储（哪个组件落盘、字段格式）归 C5/C14 消费时接入；本 change 交付原语 + rotation + 负向测试。
- **泄漏面约束**（测试断言）：sealed 输出不含明文子串；`SecretBox` 不提供把明文写日志的通道；加密粒度为字符串级，调用方保证不把明文放 tool schema/metrics label（grep 契约归 C12 label policy 已有白名单机制，本 change 不重复建设）。
- **依赖**：`requirements.txt` 显式声明 `cryptography>=42`（venv 已有 49.0.0；属传递依赖转直接依赖）。

**备选**：Fernet（cryptography 内建）。不选：Fernet 单 key、无 key_id 内嵌，rotation 要自己包格式，等于重新发明 sealed 格式；AESGCM 直用更透明。备选 2：纯 stdlib HMAC 流密码自制。不选：自研密码学原语违反 §10 精神。

### ADR-8 snapshot 发布失败保留旧 committed snapshot

**结论**：行为由现有 `RuntimeSnapshotStore` 事务保证（`begin_publish` 校验、`abort` 恢复 `previous` 并恢复其 `accepting_leases`、失败候选进 `aborted` 后 drain），本 change 补 manager 级测试：候选含非法 contribution（如 Channel 名称冲突）时 `_publish_prepared` 走 abort 路径 → 旧 snapshot仍是 `store.current`、新 lease 可获取且内容为旧 snapshot、失败候选被 drain。`PluginManager` 发布代码路径已有 try/except 归位（`_publish_prepared`），测试证明而非新增逻辑。

### ADR-9 dormant 安装（安装只登记不执行代码）

**结论**：`agent/plugins/install.py` + `FreshPluginImporter` 的现有分工满足「安装时只把 package/manifest 登记进 catalog，不执行插件代码；代码级注册在候选激活时 import 后发生」。补 dormant 测试：install 完成后插件模块不在 `sys.modules`、插件代码的 import 副作用（模块级文件写入）未发生；激活后才发生。

## 3. 数据流与模块边界

```
work start（passive/proactive/drift-via-proactive/consolidation/optimizer/plugin job）
  ├─ RevocationGate.check(tenant, action)          # 副作用前 recheck，fail-closed
  └─ work_runtime_lease(store)                     # 取 lease + ContextVar 绑定
        └─ TenantRuntimeResolver.resolve(snapshot, tenant, revision, bindings)   # per-work 不可变 plan
              └─ PluginInvocationContext(plan, WorkContext, snapshot)            # 每次 hook/tool/job 调用
                    ├─ gate hook：异常/超时 → fail-closed（deny / 中断）
                    └─ fanout/telemetry：有界 timeout → 记录失败，不改终态
副作用点（scheduler instant push / proactive deliver / job 执行）
  └─ RevocationGate.check —— 读当前 provider 状态，与持有的 snapshot 无关
```

- `RevocationGate` 与 `work_runtime_lease` 都不进入 `RuntimeSnapshot`（snapshot 保持进程级代码/接线视图，账号状态是 per-tenant 运行时状态）。
- `SecretBox` 独立于插件系统；C5/C14 消费时在各自组件内持有 `SecretBox` 实例，本 change 不改 `PluginContext`/KV 存储。

## 4. Risks / Trade-offs / 回滚

- **风险**：passive 全入口加 lease 后，snapshot 发布/热更新会被进行中的 passive turn 阻塞 drain（以前无 lease 时热更新不等待 passive）。缓解：lease 是短周期的（turn 级），`quiesce_current` 已有等待语义；实测回归覆盖 turn 全链路。
- **风险**：proactive 生产路径从静态回退切 lease 路径，若 store 注入时序错误会在 tick 时挂起等待。缓解：bootstrap 在任务启动前完成注入；`acquire()` 只在 `accepting_leases=False`（quiesce 窗口）时等待，行为与 C12 已验证的 quiesce 语义一致。
- **风险**：gate provider 异常时拒绝副作用可能放大瞬时故障（provider 抖动 → 拒绝用户消息）。接受：fail-closed 是冻结决策；Pilot 默认 provider=None 不走此路径，C5 接线时 provider 需自带重试/缓存。
- **风险**：`cryptography` 转直接依赖影响 Docker 镜像（Tsinghua mirror 已含该包）。影响有限（纯 wheel 依赖）。
- **回滚**：所有新行为经构造参数开关（store/gate 默认 None = 既有行为）；回滚 = bootstrap 不注入，代码路径退化为 no-op 分支。

## 5. 验收映射（task-08 ↔ 交付物）

| task-08 验收标准 | 交付物 | 验证 |
| --- | --- | --- |
| P0 audit 覆盖全入口且测试证明 lease 绑定 | `tests/c8/test_lease_coverage.py`（每入口一条）+ `openspec/evidence/c8-runtimesnapshot-secrets/lease-coverage-audit.md` | pytest + audit 报告复现命令 |
| 进行中 work 不切 snapshot | `tests/c8/test_lease_coverage.py::test_work_keeps_snapshot_across_publish` | pytest |
| 旧 snapshot 无法绕过 revocation | `tests/c8/test_revocation_gate.py::test_old_snapshot_cannot_bypass_revocation` 等 | pytest |
| gate/interceptor fail-closed；fanout 有界 timeout 不改终态 | `tests/c8/test_hook_failure_policy.py` | pytest |
| contribution_id 稳定 + 固定类型 + binding_policy + tenant_configurable；plan 外不可见 | `tests/c8/test_tenant_runtime_plan.py` | pytest |
| tenant secret 静态加密；不进 schema/日志/label | `tests/c8/test_secret_box.py` + C12 label policy 白名单（已有） | pytest |
| compile/publish 失败保留旧 snapshot | `tests/c8/test_snapshot_publish_fallback.py` | pytest |
| 安装只登记不执行；tenant 不能创造新 Hook | `tests/c8/test_dormant_install.py` + plan 过滤测试 | pytest |
