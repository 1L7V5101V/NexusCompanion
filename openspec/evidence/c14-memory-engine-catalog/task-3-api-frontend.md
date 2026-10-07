# task 3/4 — HTTP 端点 + WebChat selector 前端

## API（bootstrap/chat_api.py，仿 C9/C10 闭包装配模式）

- `GET /api/memory/engines`：`memory_engines` 装配时存在；auth session +
  `_resolve_endpoint_identity` 三行模式（派生失败 403）；返回服务端目录
  （engine_id/display_name/description/binding_policy/ready/selectable/active）+
  `active_engine` + `tenant_policy_revision`。
- `PUT /api/memory/engines/active`：`_EngineSelectionBody`（模块级 pydantic，
  规避 PEP 563 陷阱）；错误语义 `unknown_engine`(404)/`engine_not_selectable`
  (403)/`engine_not_ready`(409)；同 engine 幂等返回当前 revision；成功返回
  `{active_engine, tenant_policy_revision}`。客户端额外字段不参与任何授权。
- 未装配（dev/SQLite）：端点不注册 → 404 不泄露能力存在性。

## 前端（frontend/chat/src/）

- `memory.ts`：fetchMemoryEngines / setActiveEngine（404 →
  `MemoryEnginesUnavailableError`）；`MemoryEngineSelector.tsx`：header 轻量
  selector（目录/ready/active 展示、不可用禁选、错误行 4s 自清、pending 禁用）；
  功能不可用或无可选项时整体隐藏；`App.tsx` header 集成（mock/dev 模式自动隐藏）。
- 构建产物 `static/chat/` 为 gitignored 本地产物（不入库）。

## 测试证据（tests/memory_engines/test_api.py，真实 AuthRuntime + chat app）

| 验收条目 | 测试 | 结果 |
| --- | --- | --- |
| 未认证 401（GET/PUT） | `test_memory_engines_api_full_ready`（前段断言） | PASS |
| 目录只读：inspector 不出现、初始 default（验收 1/8） | 同上 | PASS |
| 客户端额外字段无授权效果（smuggled binding_policy/capabilities → 404） | 同上 | PASS |
| not ready 409（rachael 未构建场景） | `test_memory_engines_api_rachael_not_ready` | PASS |
| 切换成功 revision+1 + 同 engine 幂等 + GET 反映（验收 3/2） | `test_memory_engines_api_full_ready` | PASS |
| 用户侧切换关闭 403（GET 仍可用） | `test_memory_engines_api_selection_locked` | PASS |
| 未装配 404 不泄露 | `test_memory_engines_api_unmounted` | PASS |

## 前端命令证据

- `npm run typecheck`（tsc --noEmit）→ 0 errors
- `npm run lint` → 5 errors / 3 warnings 全部为 pulsecore/dashboard 既有问题
  （`frontend/chat/src/pulsecore/*`、`frontend/dashboard/src/main.tsx`），本次
  新增文件零告警
- `npm run build:chat` → ✓ built（产物 static/chat/index-*.js）
