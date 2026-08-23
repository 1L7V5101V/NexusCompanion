# C0 可复用面确认（task 1.2 证据）

> 目的：记录 turn 追踪/指标/dashboard/后端切换的既有接入点，作为后续收集点与挂载点的基线。
> 验证方式：全部为对当前代码的引用核查（codegraph + grep），非推测。

## 1. diagnostic_log：turn/phase ContextVar 与 diagnostic_line

- 文件：`core/common/diagnostic_log.py`
- `diagnostic_context`（:40）以 ContextVar 设置 `session`/`flow`/`phase`/`turn`/`tick`，contextmanager 语义，退出自动 reset。
- `diagnostic_line`（:30）按固定字段（event/flow/phase/session/turn/duration_ms...）生成结构化日志行。
- `current_diagnostic_context`（:67）返回当前 session/flow/phase/turn/tick 快照。
- 已贯穿 `PassiveTurnPipeline` 各 phase（见下）。

## 2. PassiveTurnPipeline：phase 边界与 turn_id

- 文件：`agent/core/passive_turn.py`
- `run`（:363）入口：`turn_id = _turn_log_id(key, msg)`（:371），外层 `with diagnostic_context(session=key, flow="passive", turn=turn_id)`（:377）。
- phase 边界（每个 phase 包裹 `with diagnostic_context(phase=...)` 且各 phase 完成/出错均打 diagnostic_line，携带 `duration_ms` 与 `turn=turn_id`）：
  - before_turn（:392）
  - before_reasoning（:432）
  - reasoner（:476，`self._reasoner.run_turn(...)`）
  - after_reasoning（:524）
  - after_turn（:560）
- 错误路径：reasoner 异常（:497 phase="reasoner"）、after_reasoning（:528）、after_turn（:568）各带 error_type/note。

## 3. lifecycle 事件：turn_id 字段

- 文件：`bus/events_lifecycle.py`
- `TurnStarted`（:36）：字段含 `turn_id: str = ""`（:42）。
- `TurnCommitted`（:66）：字段含 `tenant_id`/`turn_id`（:74-75）；在 `agent/lifecycle/phases/after_turn.py:132` 构造时取 `current_turn_id.get()`。
- `ToolCallStarted`（:121）/`ToolCallCompleted`（:133）亦带 `turn_id`。
- `bootstrap/control_execution.py` 已按 turn_id 关联事件（design.md 引用）。

## 4. dashboard 挂载点与路由注册

- 文件：`bootstrap/dashboard_api.py`
- `create_dashboard_app`（:845）：FastAPI app（`app = FastAPI(title="Nexus Dashboard API", lifespan=lifespan)`，:942）。
- 静态资源 `app.mount("/assets", StaticFiles(...))`（:949）。
- 路由以 `@app.get("/api/...")` 直接注册在 app 上（如 :980 /api/dashboard/plugins、:1030 /api/dashboard/sessions）。
- `_session_view`/`_memory_view` 经 `storage_runtime.for_tenant(...)`（:891-899）取 tenant-bound view —— 新增 `/metrics` 挂载点沿用同一 app 注册方式即可。

## 5. config storage.backend 切换

- 文件：`agent/config_models.py`
- `StorageConfig.backend: str = "sqlite"`（:90），`Config.storage: StorageConfig`（:191）。
- 文件：`infra/storage/factory.py`
- `create_memory_store`（:45-53）：按 `config.backend == "sqlite"/"postgres"` 分支，未知抛 `ValueError`。
- `create_session_store`（:57-68）：同理。
- `create_storage_runtime`（:71-96）：`config.backend == "postgres"` 走 PG，`"sqlite"` 走共享单用户 store。
- 后端切换 = 修改 config `storage.backend` 后重建 runtime，无需改代码。

## 结论

五个可复用面全部存在且与 design.md 描述一致：追踪可挂在 `PassiveTurnPipeline.run` 既有 phase 边界（2）与 lifecycle turn_id（3）；指标挂载点复用 dashboard app 路由注册（4）；后端驱动复用 `config.storage.backend` + factory（5）。无需新建日志/追踪栈。
