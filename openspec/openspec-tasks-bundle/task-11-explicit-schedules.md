# Task-11 — tenant-owned explicit schedules + recovery（explicit-schedules）

> 编号对应 PILOT_ROADMAP §5.9.10 第 11 项。状态标记复用 §8。跨阶段：P2 主体 + P3 恢复演练收尾。

## 元数据

- **所属阶段**：主要里程碑 = P2；恢复演练 = P3
- **§5.9 引用**：§5.9.14（显式用户 schedule 与 proactive tick 分离）、§10 DECIDED（Explicit schedule 语义）
- **§6 出口条件引用**：P2 出口「显式用户 schedule 迁移为 tenant/account/conversation owned durable work，delivery target 由服务端 binding 解析」；P3 出口「schedule misfire/restart/idempotency/DST 演练」
- **状态**：planned

## 目标

将显式用户 schedule（reminder/cron）迁移为 tenant/account/conversation owned durable business work：`scheduled_jobs` / `schedule_executions` 表；tenant_id/account_id/canonical_conversation_id 与**服务端解析的 delivery binding**（模型/客户端不能提交任意 `channel/chat_id` 作为授权目标，§5.9.14）；`(job_id, scheduled_for)` 幂等键 + 持久化 execution attempt/terminal outcome/delivery intent/时区/计划版本；one-shot misfire grace 5min→`missed`（§10 PROPOSED DEFAULT，P-1 复核数值）；recurring 不回放全部 missed、只算下一未来 occurrence，每次 skip/miss 有记录；IANA 时区 + DST 跳时/重复时刻 contract test；`suspended` 暂停新执行、`revoked` 禁用；与 Proactive/Drift/optimizer tick（可重算）**分开恢复语义**（§5.9.14 不能共享「错过就跳过」的笼统恢复语义）。

## 输入

- 上游 change 产出：C5（owner principal，D3）、C1（tenant/canonical conversation 派生）、C2（outbox intent 复用）
- roadmap 冻结决策：§5.9.14 全节、§10 DECIDED（Explicit schedule 语义：`(job_id, scheduled_for)` 幂等；suspend 暂停/revoke 禁用；recurring 不回放）、§10 PROPOSED DEFAULT（Schedule misfire 5min grace）
- 现有代码锚点：`schedules.json`（全局 JSON schedule 现状）、`check_schedules.py`、scheduler 相关实现
- 依赖前置：C5（D3）

## 输出

- DB schema：`scheduled_jobs` / `schedule_executions` 表 + 迁移（delivery target 为服务端解析 binding，非客户端提交 channel/chat）
- 代码：scheduler rework（tenant-owned durable work）、execution recovery（从 durable schedule/execution 恢复）、admin misfire 查看
- 测试/证据：owner 校验负向测试、幂等执行测试、misfire 测试、recurring 测试、DST contract test、状态联动测试、恢复演练

## 验收标准

- [ ] owner 校验：模型/客户端不能指定任意 channel/chat_id 作为授权目标（delivery binding 由服务端解析） — 验证：负向测试
- [ ] `(job_id, scheduled_for)` 幂等执行 — 验证：重复执行测试（同 key 不产生第二次副作用）
- [ ] one-shot 超 5min grace 标 `missed` 且可 admin 查看（**不静默删除**） — 验证：misfire 测试 + admin 查询
- [ ] recurring 不回放全部 missed、只算下一未来 occurrence；每次 skip/miss 有记录 — 验证：recurring 测试
- [ ] IANA 时区 + DST 跳时/重复时刻 contract test — 验证：DST contract test
- [ ] suspend 暂停新执行、恢复从下一未来 occurrence；revoke 禁用 — 验证：状态联动测试（§5.9.14）
- [ ] 重启恢复可解释（从 durable schedule/execution 恢复，`(job_id, scheduled_for)` 去重） — 验证：P3 恢复演练
- [ ] 显式 schedule 与 proactive/optimizer tick 恢复语义分离（tick 不回放、只重算下次调度） — 验证：恢复语义断言
- [ ] 本 task 不触碰 proactive/optimizer tick 实现本体 — 验证：PR diff 范围检查

> 判定「真正完成」而非「执行过」：owner 校验负向、幂等执行、DST contract 是可复现测试要求；「不静默删除 missed」「recurring 不回放全部」需负向断言。

## 独立性边界（不与其他任务重复）

- 本任务拥有：`scheduled_jobs` / `schedule_executions` 表、scheduler rework、execution recovery、admin misfire 查看
- 本任务不触碰：proactive/optimizer tick（可重算 runtime tick，恢复语义分离）、C2 的 outbox 表 schema（只复用 intent 模式）、schedule 全局 JSON 迁移后的旧文件清理（可派生物）
- 共享 seam 协议：消费 C5 owner principal；delivery 经 C2 outbox intent 模式投递；schedule 触发点记录归 C12 观测

## 依赖

- **左依赖（必须先完成）**：C5（D3：5 是 11 对普通 tenant 开放的前置）
- **右依赖（本任务前置于）**：无独立下游 change
- **可并行**：C6 / C7 / C9 / C10 / C14

## 风险与需冻结决策

- §10 PROPOSED DEFAULT「Schedule misfire」：one-shot grace 默认 5 分钟，产品确认是否保留；无论数值如何都必须持久化 miss/attempt/outcome（§5.9.14）。
- §10 DECIDED「Explicit schedule 语义」：suspend 暂停、revoke 禁用、recurring 不回放全部 missed。
- 风险：全局 `schedules.json` 残留被当 canonical → 迁移后旧文件只作可重建派生物（§5.9.12）；DST 边界（跳时/重复时刻）漏测 → contract test 固化。