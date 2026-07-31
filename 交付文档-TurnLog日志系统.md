# TurnLog 日志系统 — 交付文档

## 概述

为 NexusCompanion 的三条处理链路（Passive / Proactive / Drift）添加了统一的 **Turn 级日志记录**。每条 LLM 调用回合（turn）完成后，自动将完整记录（prompt、response、token 用量、耗时等）写入独立的 SQLite 数据库，并通过 Dashboard Web UI 提供可视化的查询和详情查看。

---

## 架构

```
passive_turn / proactive_turn / drift_turn
         │
         ▼  turn 完成后调用
  RoutingTurnLogger.log(data)
         │
         ├── turn_type="passive"   → TurnLogger → passive.db
         ├── turn_type="proactive" → TurnLogger → proactive.db
         └── turn_type="drift"     → TurnLogger → drift.db
                │                         ▲
                │                 异步队列 + 批量 flush
                ▼                         │
         SQLite 三库              每隔 1s / 50 条
         ~/.nexus/workspace/logs/
                │
                ▼
         LogDashboardReader  (Dashboard API)
                │
                ▼
         /api/dashboard/logs      — 分页列表（多库合一排序）
         /api/dashboard/logs/{type}/{id}  — 单条详情
                │
                ▼
         Frontend: "Logs" 标签页
         表格列: Type / Session / Timestamp / Model / Tokens / Duration
         详情面板: user_prompt / system_prompt / messages / llm_response
```

---

## 模块清单

### 1. 核心日志模块 `logging/`（新增）

| 文件 | 职责 |
|------|------|
| `__init__.py` | 导出 `TurnLogData`、`TurnLogger`、`RoutingTurnLogger` |
| `models.py` | `TurnLogData` 数据类 — 统一的三链路日志结构 |
| `turn_logger.py` | `TurnLogger`（异步队列 + SQLite 批量写入）、`RoutingTurnLogger`（按 turn_type 路由到三库） |

**TurnLogger 特性：**
- 异步 `asyncio.Queue` 入队，不阻塞主流程
- 后台协程 `_flush_loop` 定时批量 flush（默认 1s 间隔 / 50 条上限）
- `start()` / `close()` 生命周期管理，close 时确保所有待写入完成
- SQLite WAL 模式，建表 + 索引自动初始化

**三库独立：** passive.db / proactive.db / drift.db，同享 `turn_logs` 表结构，通过 `turn_type` 字段区分。

### 2. 配置层

| 文件 | 变更 |
|------|------|
| `agent/config_models.py` | 新增 `LoggingConfig`（`enabled`、`passive_db`、`proactive_db`、`drift_db`） |
| `agent/config.py` | 新增 `_load_logging_config()` 解析 `[logging]` 配置段 |
| `config.example.toml` | 添加 `[logging]` 配置示例 |

```toml
[logging]
enabled = true
# passive_db = ""    # 默认 ~/.nexus/workspace/logs/passive.db
# proactive_db = ""  # 默认 ~/.nexus/workspace/logs/proactive.db
# drift_db = ""      # 默认 ~/.nexus/workspace/logs/drift.db
```

### 3. 生命周期集成

| 文件 | 变更 |
|------|------|
| `agent/lifecycle/types.py` | `AfterTurnContext` 添加 `turn_logger` 字段 |
| `agent/lifecycle/phases/after_reasoning.py` | after_reasoning 阶段记录 LLM 调用回合（input_tokens、output_tokens、llm_response 等） |
| `agent/lifecycle/phases/after_turn.py` | after_turn 阶段记录完整 turn 数据（messages、tools_schema、error 等） |
| `agent/looping/ports.py` | `AgentLoopDeps` 添加 `turn_logger` 字段 |
| `agent/looping/core.py` | AgentLoop 初始化时传递 turn_logger |
| `agent/core/types.py` | 相关类型添加 turn_logger 支持 |
| `agent/core/runtime_support.py` | runtime support 传递 turn_logger |

### 4. 三链路 Pipeline 集成

| 链路 | 文件 | 集成方式 |
|------|------|----------|
| Passive | `agent/core/passive_turn.py` | `DefaultReasoner.__init__` 接受 turn_logger，turn 完成后调用 log() |
| Proactive | `agent/core/proactive_turn.py` | `ProactiveTurnPipelineDeps` 添加 turn_logger 字段 |
| Drift | `agent/core/drift_turn.py` | `DriftTurnPipelineDeps` 添加 turn_logger 字段 |
| Drift (插件) | `plugins/drift_flow/runtime.py` | pipeline 执行完成后写入 drift 日志 |
| Drift (工厂) | `plugins/drift_flow/factory.py` | `build_drift_pipeline()` 接受 turn_logger |
| Proactive (插件) | `plugins/default_proactive/factory.py` | 传递 turn_logger 到 pipeline |
| Proactive Loop | `proactive_v2/loop.py` | ProactiveLoop 接受 turn_logger 并传入 scope |
| Proactive Scope | `proactive_v2/runtime_scope.py` | ProactiveRuntimeScope 添加 turn_logger 字段 |

### 5. Bootstrap 组装

| 文件 | 变更 |
|------|------|
| `bootstrap/tools.py` | 新增 `_build_turn_logger()` 工厂函数；`build_core_runtime()` 中创建 RoutingTurnLogger 并传入 `CoreRuntime.turn_logger`；传入 `_build_loop_deps()` |
| `bootstrap/app.py` | app 启动时传递 turn_logger |
| `bootstrap/proactive.py` | proactive 启动时传递 turn_logger |
| `bootstrap/memory.py` | memory 初始化时传递 turn_logger |

核心代码在 `bootstrap/tools.py` 中：

```python
# 约第 635 行
turn_logger: RoutingTurnLogger | None = None
logging_cfg = config.logging
if logging_cfg.enabled:
    turn_logger = _build_turn_logger(logging_cfg, workspace)

loop_deps = _build_loop_deps(
    ...
    turn_logger=turn_logger,
)
```

### 6. Dashboard 后端 API

| 文件 | 内容 |
|------|------|
| `bootstrap/dashboard_api.py` | 新增 `LogDashboardReader` 类 + `/api/dashboard/logs` 路由 |

**LogDashboardReader（约 489–630 行）：**
- `list_logs(page, page_size, turn_type, session_key, ts_from, ts_to, sort_order)` — 多库联合分页查询，结果按 timestamp 排序
- `get_log_detail(turn_type, log_id)` — 单条完整记录（含 messages / llm_response / system_prompt 等大字段）

**FastAPI 路由（约 1434–1465 行）：**

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/dashboard/logs` | GET | 分页日志列表，支持 `turn_type`、`session_key`、`ts_from`、`ts_to` 筛选 |
| `/api/dashboard/logs/{turn_type}/{log_id}` | GET | 单条日志详情 |

生命周期管理：`LogDashboardReader` 在 `create_dashboard_app()` 中通过 `get_log_reader()` 惰性创建，lifespan shutdown 时关闭所有连接。

### 7. Dashboard 前端

| 文件 | 变更 |
|------|------|
| `frontend/dashboard/src/types.ts` | 新增 `LogRow` 接口；`BuiltinView` 添加 `"logs"` |
| `frontend/dashboard/src/main.tsx` | 新增 LogViewer 组件（状态管理 / 数据加载 / 表格 / 详情 / 筛选） |
| `frontend/dashboard/src/styles.css` | 新增 `.mode-logs` 网格列布局 |
| `frontend/dashboard/src/design/ui.tsx` | 注入 LogRow 类型声明 |

**UI 组成：**
- 侧边栏 "Logs" 标签页
- `turn_type` 筛选项（全部 / passive / proactive / drift）
- 表格列：Type (pill) / Session / Timestamp / Model / Tokens / Duration
- 详情面板：type / session / timestamp / model / token 统计 / duration + user_prompt / system_prompt / messages / llm_response

---

## 配置

在 `config.toml` 中添加：

```toml
[logging]
enabled = true
# 以下为可选，默认路径为 ~/.nexus/workspace/logs/<turn_type>.db
# passive_db = ""
# proactive_db = ""
# drift_db = ""
```

---

## 数据表结构

三个库（passive.db / proactive.db / drift.db）同享以下表结构：

```sql
CREATE TABLE turn_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key      TEXT NOT NULL,
    channel          TEXT,
    chat_id          TEXT,
    turn_type        TEXT NOT NULL,
    timestamp        TEXT NOT NULL,
    skill_names      TEXT,          -- JSON array
    retry_attempts   TEXT,          -- JSON array
    messages         TEXT NOT NULL, -- JSON array (LLM 对话消息)
    tools_schema     TEXT,          -- JSON array
    llm_model        TEXT,
    llm_response     TEXT,          -- LLM 原始响应
    tool_calls       TEXT,          -- JSON array
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    cache_hit_tokens INTEGER,
    turn_duration_ms INTEGER,
    error            TEXT,
    metadata         TEXT,          -- JSON object
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_turn_logs_turn_type ON turn_logs(turn_type);
CREATE INDEX idx_turn_logs_timestamp ON turn_logs(timestamp);
CREATE INDEX idx_turn_logs_session  ON turn_logs(session_key);
```

---

## 验收检查

- [x] `logging/` 模块独立可用（`TurnLogger` + `RoutingTurnLogger`）
- [x] Config 层支持 `[logging]` 配置段，可启用/禁用
- [x] 三条链路（passive / proactive / drift）turn 完成后自动写入日志
- [x] 异步队列 flush 不阻塞主流程，close 时确保全部写入
- [x] Dashboard API 提供 `/api/dashboard/logs` 分页查询 + 单条详情
- [x] Dashboard 前端 "Logs" 标签页支持表格浏览、筛选、详情查看
- [x] TypeScript 类型检查通过（`tsc --noEmit`）
- [x] Vite 生产构建通过（`vite build`）
- [x] Pyright 类型检查通过（`pyright --level error` on changed files）
- [x] 5 个 commit 已推送至 `main`
