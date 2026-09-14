# C8 RuntimeSnapshot lease + hook failure + tenant secret/revocation — tasks

> 任务计划：`openspec/openspec-tasks-bundle/task-08-runtimesnapshot-secrets.md`。分支/worktree：`feature/c8-runtimesnapshot-secrets` @ `D:/.Projects/NexusCompanion-c8`。
> 标记约定：`[x]` 完成（附验证），`[ ]` 未完成。P0 段与 P2 段按 task-08 分段验收。

## 1. P0 段 — lease coverage + revocation recheck

- [x] 1.1 `agent/plugins/snapshot.py`: 新增 `work_runtime_lease(store)` 统一 work-start lease 入口（acquire → bind → yield → reset + release，异常/取消路径统一释放）。验证：`tests/c8/test_lease_coverage.py`
- [x] 1.2 `agent/looping/ports.py` + `agent/looping/core.py`: `AgentLoopDeps` 增加 `runtime_snapshot_store`/`revocation_gate`；`AgentLoop.bind_runtime_snapshot_store()` 接通 bootstrap 既有 getattr seam；`_process_with_runtime_admission` work start 取 lease + gate recheck；`trigger_memory_consolidation` 取 lease。验证：`tests/c8/test_lease_coverage.py`、`tests/c8/test_revocation_gate.py`
- [x] 1.3 `bootstrap/tools.py` + `bootstrap/proactive.py`: AgentLoop store/gate 接线；`build_proactive_runtime` 显式传 `runtime_snapshot_store`；ProactiveLoop tick start gate recheck（`proactive_v2/loop.py`）。验证：`tests/c8/test_lease_coverage.py`（proactive 用例）
- [x] 1.4 maintenance/optimizer：`core/memory/markdown.py`（MarkdownMemoryMaintenance 后台 worker task work-start lease）+ `proactive_v2/memory_optimizer.py`（`optimize`/`MemoryOptimizerLoop.run` lease）。验证：`tests/c8/test_lease_coverage.py`
- [x] 1.5 scheduler/plugin job recheck：`agent/scheduler.py::_execute`（instant 直推前）+ `agent/plugins/jobs.py`（执行前）接 `RevocationGate`。验证：`tests/c8/test_revocation_gate.py`
- [x] 1.6 `agent/admission/revocation.py`: `RevocationGate` + `TenantStatusProvider`（fail-closed：REVOKED/SUSPENDED/UNKNOWN/provider 异常拒绝；provider=None 显式 dev-open 带日志）。验证：`tests/c8/test_revocation_gate.py`（含 `test_old_snapshot_cannot_bypass_revocation`）
- [x] 1.7 热更新不切 snapshot 测试 + drift lease 归属证明（drift 在 proactive tick lease 内执行）。验证：`tests/c8/test_lease_coverage.py`
- [x] 1.8 P0 lease coverage audit 报告（全入口逐条：入口/接线点/测试入口/结论）。验证：`openspec/evidence/c8-runtimesnapshot-secrets/lease-coverage-audit.md` + 复现命令

## 2. P2 段 — per-task context + hook failure + secrets

- [ ] 2.1 `agent/plugins/tenant_plan.py`: `ContributionMeta`（contribution_id/固定 kind/binding_policy/tenant_configurable）+ `TenantRuntimePlan`（frozen，`allows()` 唯一可见性判定）+ `TenantRuntimeResolver.resolve()` + `PluginInvocationContext`/`WorkContext`。验证：`tests/c8/test_tenant_runtime_plan.py`
- [ ] 2.2 hook failure 分层：`agent/tool_hooks/executor.py`（pre-tool 缺 context/超时/异常 → deny；post 有界 timeout 记录失败不改终态）+ `bus/event_bus.py`（observe/fanout 每观察者有界 timeout）+ `[agent.plugins].hook_timeout_seconds` 配置。验证：`tests/c8/test_hook_failure_policy.py`
- [ ] 2.3 `core/crypto/secret_box.py`: AES-256-GCM sealed 格式 `v1:<key_id>:<nonce>:<ct>`、workspace `keys/` keyring、ACTIVE_KEY 指针、rotation/撤销边界；`requirements.txt` 声明 `cryptography`。验证：`tests/c8/test_secret_box.py`
- [ ] 2.4 snapshot 发布失败保留旧 snapshot 测试（候选非法 → abort → 旧 snapshot 仍 current、新 lease 可用、候选被 drain）。验证：`tests/c8/test_snapshot_publish_fallback.py`
- [ ] 2.5 dormant 安装测试（install 后插件模块未 import、import 副作用未发生；激活后才发生）。验证：`tests/c8/test_dormant_install.py`

## 3. 回归与证据

- [ ] 3.1 全量回归：`pytest -q -W error tests/` 对齐 main 基线（1312 passed + 1 既有 chat_api 环境性失败）；`pyright` 对齐 main 既有 36 错误基线。验证：执行记录入 `openspec/evidence/c8-runtimesnapshot-secrets/`
- [ ] 3.2 状态同步：`PILOT_ROADMAP_PROJECT_CHECKLIST.md` P0 「RuntimeSnapshot 全入口 lease coverage audit」条目按 §8 更新（仅 P0 段 verified 后）；`openspec/openspec-tasks-bundle/task-08-runtimesnapshot-secrets.md` 状态头 `planned → in_progress`。
- [ ] 3.3 P2 段收口与 checklist/task-08 P2 段状态更新（本 change 后续提交）。
