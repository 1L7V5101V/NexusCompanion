# C8 P0 — RuntimeSnapshot work-start lease coverage audit

> change：`openspec/changes/c8-runtimesnapshot-secrets` · 分支 `feature/c8-runtimesnapshot-secrets`
> 依据：PILOT_ROADMAP §5.9.16 + §10 DECIDED（P0 固化最小正确性 gate）；ADR-1 work_runtime_lease、ADR-2 store 接线、ADR-3 RevocationGate。
> 验证命令：
> ```
> ./.venv/Scripts/python.exe -m pytest tests/c8/ -q
> ./.venv/Scripts/python.exe -m pytest tests/test_plugin_manager.py tests/test_scheduler_service.py \
>   tests/test_agent_background_job_runtime.py tests/admission/ tests/test_agent_self_check.py \
>   tests/proactive_v2/test_integration.py -q
> ```
> P0 专项输出见 `pytest-p0-lease-gate.txt`（25 passed）。

## 结论

P0 段全入口 lease 覆盖完成。每类 work 入口在 work start 各取一次 `work_runtime_lease`（acquire → bind → yield → reset + release，异常/取消统一释放）；副作用方 revocation recheck（`RevocationGate`）全部使用**当前 provider 状态**，不读 snapshot 捕获状态，旧 snapshot 不能绕过。

`work_runtime_lease` 语义窗（ADR-1）：store=None 时 yield None（未接线组件保持既有行为，不因 lease 缺失崩溃）；接线完整性由本测试矩阵保证，不靠回退分支兜底。

## 入口逐条

| 入口 | 接线点 | 测试入口 | 结论 |
| --- | --- | --- | --- |
| Passive turn（渠道消息 / scheduler soft / programmatic direct） | `AgentLoop._process_with_runtime_admission`：`work_runtime_lease` 内执行 `_process`；lease 后、`_process` 前 gate recheck | `test_passive_process_holds_lease_and_gate`；`test_passive_process_rejected_before_process` | ✅ lease + recheck |
| Passive 内部 `process_direct_message` → `process_direct` | 收束于 `_process_with_runtime_admission`（无独立接线点） | 同上 | ✅（同一收束点） |
| Proactive tick | `ProactiveLoop._tick` 在 store 注入后经 `acquire + bind → _tick_bound`；生产构造 `bootstrap/proactive.py` 显式注入 store（静态回退切 lease 路径） | `test_proactive_tick_holds_lease_and_drift_runs_inside_it` | ✅ lease |
| Proactive tick 副作用方 recheck | `_tick_bound` 开头（session_key 设置后、provisioning gate 前） | `test_revocation_gate.py::test_revoked_status_rejected`（action=proactive_tick） | ✅ recheck |
| Drift | `DriftTurnPipeline.run` 仅由 proactive v2 runtime route=drift 分支驱动，执行时已在 tick lease 绑定期内（不为 drift 单独取 lease，设计决策） | `test_proactive_tick_holds_lease_and_drift_runs_inside_it`（drift 在 tick lease 内运行） | ✅（归属证明） |
| Consolidation（control/guard 触发） | `AgentLoop.trigger_memory_consolidation`：`work_runtime_lease` 包裹 `maintenance.consolidate` | `test_trigger_memory_consolidation_holds_lease` | ✅ lease |
| Maintenance 后台 worker | `MarkdownMemoryMaintenance._run_maintenance_queue`：每条 maintenance work 在 `work_runtime_lease` 内执行 consolidate/refresh；store 由 `bind_runtime_snapshot_store()` 传播注入 | `test_maintenance_background_worker_holds_lease` | ✅ lease |
| MemoryOptimizer | `MemoryOptimizer.optimize`：`work_runtime_lease` + tenant lock；`MemoryOptimizerLoop.run` 周期调用 `optimize`（同一 lease 路径） | `test_memory_optimizer_holds_lease`；`test_memory_optimizer_busy_rejects_concurrent_run` | ✅ lease |
| Plugin job | `PluginJobRuntime._invoke`：gate recheck 放进 lease 内（拒绝也释放在 enqueue 期获取的 lease，不泄漏 snapshot）；lease 绑定 handler 执行 | `test_plugin_job_passes_when_active_and_binds_snapshot`；`test_plugin_job_rejected_before_handler`；`test_revocation_gate.py::test_plugin_job_*` | ✅ lease + recheck |
| Scheduler instant 直推 | `SchedulerService._execute`（instant 分支）：`push_tool.execute` 前 gate recheck | `test_scheduler_instant_push_rejected_before_push`；`test_scheduler_instant_push_passes_when_active` | ✅ recheck |
| Recovery work（启动期 `StartupRecoveryScanner.scan()`） | 同步扫描、无插件/hook 交互 —— 审计结论**不绑定**（补偿执行闭环依赖 C2 durable 表，归 P3 演练评估） | —（设计决策） | ⚪ 不绑定（记录） |

## RevocationGate 语义验证

| 判定 | 测试 | 结论 |
| --- | --- | --- |
| ACTIVE | `test_active_status_passes` | ✅ 放行 |
| SUSPENDED / REVOKED | `test_suspended_status_rejected` / `test_revoked_status_rejected` | ✅ 拒绝（携带 tenant/action/status） |
| UNKNOWN | `test_unknown_status_rejected_fail_closed` | ✅ 拒绝（fail-closed） |
| provider 异常 | `test_provider_exception_fail_closed` | ✅ 拒绝（归 UNKNOWN） |
| provider=None（Pilot dev-open） | `test_dev_open_allows_and_logs` | ✅ 放行 + 结构化日志 `revocation_gate=dev_open`（显式声明，非静默降级） |
| 旧 snapshot 不能绕过 | `test_old_snapshot_cannot_bypass_revocation` | ✅ 持旧 lease 期间账号 revoke → 副作用点拒绝，绑定不被判定影响 |

## 进行中 work 不切 snapshot

| 场景 | 测试 | 结论 |
| --- | --- | --- |
| 热更新发布 commit | `test_work_keeps_snapshot_across_publish` | ✅ 进行中 work 绑定保持旧 snapshot；提交后新 work 拿新 snapshot |
| 发布失败 abort | `test_work_keeps_snapshot_across_abort` | ✅ 进行中 work 不切；abort 后 current 恢复旧 snapshot |
| 异常 / 取消释放 | `test_work_lease_releases_on_exception` / `test_work_lease_releases_on_cancel` | ✅ 统一释放 |
| 跨 task 隔离 | `test_work_lease_binding_is_task_local` | ✅ ContextVar 带 owner 校验，子 task 不可见父绑定 |

## 复现

```powershell
cd D:\.Projects\NexusCompanion-c8
./.venv/Scripts/python.exe -m pytest tests/c8/ -q          # 25 passed
# 回归（改动模块关联现有套件）：
./.venv/Scripts/python.exe -m pytest tests/test_plugin_manager.py tests/test_scheduler_service.py `
  tests/test_agent_background_job_runtime.py tests/admission/ tests/test_agent_self_check.py `
  tests/proactive_v2/test_integration.py -q               # 128 passed
```