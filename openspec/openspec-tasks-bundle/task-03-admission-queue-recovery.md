# Task-03 — tenant admission + bounded queue + restart recovery（admission-queue-recovery）

> 编号对应 PILOT_ROADMAP §5.9.10 第 3 项。状态标记复用 §8。跨阶段 change：P0 主体 + P3 恢复演练收尾，分段验收。

## 元数据

- **所属阶段**：主要里程碑 = P0；恢复演练收尾 = P3（分段验收，见下文「验收标准」中 P3 段）
- **§5.9 引用**：§5.9.5（admission/队列容量/overload policy）、§5.9.6（restart recovery）、§6.1 A（tenant-scoped serial lane）、§6.1 B（durable recovery/replay）、§10 DECIDED（Tenant admission、Queue 容量初始值）
- **§6 出口条件引用**：P0 出口「同一 canonical conversation 不会无序并发执行多个状态写入任务」「后台 maintenance 不会长期挤压 interactive turn」「进程内 queue/task 已知丢失窗口有明确记录」；P3 出口「启动恢复扫描…恢复契约」
- **状态**：in_progress（P0 段主体已实现于 `feature/c3-admission-queue-recovery`，change `openspec/changes/c3-admission-queue-recovery/`，2026-09-06；P3 恢复演练段保持 planned 待 C2 durable 表落地）

## 目标

落地 tenant-scoped serial lane（lane key = 服务端派生 `tenant_id`）；有界队列 + overload（global interactive 128、per-tenant pending 16、maintenance 64、per-kind maintenance 1、WS outbound 256/soft 192/1 MiB hard；LLM/embedding/MCP/process 默认 30/4/8/2，§10 DECIDED Queue 容量初始值）；interactive 优于 maintenance（maintenance 可合并/延后）；启动恢复扫描 + `unknown` / `compensation_required` 语义（§6.1 B，compensation 演练在 P3）；**无全局 maintenance lock**，optimizer lock 按 tenant 隔离；lane owner 统一释放，进程关闭不产生新 work。

## 输入

- 上游 change 产出：C1（E9 弱对齐：admission key = tenant_id；C1 canonical mapping 落地后切换）——C3 主体可与 C1 并行，仅在切换点对齐
- roadmap 冻结决策：§5.9.5 容量表与 overload 行为、§6.1 A/B、§10 DECIDED Tenant admission / Queue 容量初始值 / Adjustable（P0/P0.5 压测后可调）
- 现有代码锚点：`bus/queue.py`、`bootstrap/passive_worker.py`、`agent/control/runtime.py`
- 依赖前置：无（并行根；E9 弱耦合 C1）

## 输出

- 代码：tenant admission 实现、有界 queue adapter、overload 行为（429+Retry-After / 拒绝 / 合并 / overload close）、启动恢复扫描框架、lane owner 统一释放
- 日志/观测字段：`recovery_started_at` / `recovery_finished_at` / `recovery_action` / `recovery_result` / 原始 work id / attempt（供 C12 §7.1 指标复用）
- 测试/证据：时间线观测测试（串行/并行）、overload 测试矩阵、资源 semaphore 上限测试、优先级测试、恢复演练报告（P3）、隔离测试
- 演练：P3 恢复演练（每一条未完成任务可解释为 replay/recompute/compensate/cancelled/missed/intentionally skipped，§6.1 B 落地顺序）

## 验收标准

**P0 段（主体）**

- [ ] 不同 tenant 异步执行、同 tenant 串行（Passive/Proactive/Drift/consolidation/optimizer/tool 不并发） — 验证：时间线观测测试断言无交叉等待
- [ ] 队列满载行为符合 §5.9.5：interactive 128 满返 429+Retry-After、per-tenant 16 满拒绝、maintenance 64 满合并/延后、WS outbound 192→delta degrade、256/1MiB→overload close — 验证：overload 测试矩阵
- [ ] 资源 semaphore 生效：LLM=30 / embedding=4 / MCP=8 / process=2，分类计数不互耗 — 验证：并发上限测试
- [ ] interactive 优先于 maintenance；maintenance 可合并/可从 durable state 重算 — 验证：优先级测试
- [ ] 无跨 tenant global maintenance lock；optimizer lock 按 tenant 隔离 — 验证：grep + 隔离测试
- [ ] lane owner 在成功/失败/取消/超时/异常路径统一释放；进程关闭不产生新 work — 验证：异常路径测试
- [ ] 已知丢失窗口（进程内 queue/task）有明确记录 — 验证：P0 出口文档断言

**P3 段（恢复演练收尾）**

- [ ] 启动恢复扫描能逐条解释每条未完成任务为 replay / recompute / compensate / cancelled / missed / intentionally skipped — 验证：恢复演练报告（每任务一行断言 + `recovery_action`/`recovery_result` 字段）
- [ ] 显式用户 schedule 的「错过即跳过」恢复语义**不**与 proactive/optimizer tick 混用（§5.9.14 分离） — 验证：恢复语义断言（本 task 只覆盖 runtime/scheduler tick 恢复；用户 schedule 恢复归 C11）

> 判定「真正完成」而非「执行过」：P0 段与 P3 段**分别**产生可复现证据；时间线/overload/恢复演练的关键断言需可反复执行。

## 独立性边界（不与其他任务重复）

- 本任务拥有：admission lane、queue adapter、overload 策略、资源 semaphore、启动恢复扫描框架、lane owner 释放
- 本任务不触碰：durable 表 schema（inbox/turn/outbox/delivery，属 C2）、canonical identity 映射（C1，E9 弱对齐）、用户 schedule 表（C11）
- 共享 seam 协议：lane key = `tenant_id`（C1 canonical mapping 切换点对齐）；为 C4（E2）提供 tenant-scoped admission + 有界队列；为 P1GATE（D2）提供公网 recovery 承诺；为 C12（E10）提供队列/恢复观测字段

## 依赖

- **左依赖（必须先完成）**：无（并行根；E9 弱耦合 C1 —— roadmap 未将 C3 列为 C1 强依赖，D1 仅列 2/4/5/10）
- **右依赖（本任务前置于）**：C4（E2：dev WebChat 闭环前置）、P1GATE（D2：与 C2 共同满足公网 delivery/recovery 承诺）、C12 指标字段（E10）
- **可并行**：C1、C12（契约）、C13

## 风险与需冻结决策

- §10 DECIDED「Queue 容量初始值」已冻结数字（128/16/64/1/256-soft192-1MiB、30/4/8/2），P0/P0.5 压测记录后**后续 change** 才可调整；本 task 以可配置初始值实现。
- §10 DECIDED「Tenant admission」：不引入跨 tenant maintenance lock；
- 风险：E9 弱耦合点（canonical mapping 切换）易遗漏 → 在切换对齐点加集成测试；「丢失窗口记录」易写成空话 → 用文档 + 可数化断言。