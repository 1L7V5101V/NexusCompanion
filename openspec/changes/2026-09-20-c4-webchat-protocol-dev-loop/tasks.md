# C4 WebChat protocol + dev-only loop — 实施任务

> 对应 `openspec/openspec-tasks-bundle/task-04-webchat-protocol-dev-loop.md`；验收标准复刻为该 change 的 checkbox。
> 分支：`feature/c4-webchat-protocol-dev-loop`（worktree `D:/Project/NexusCompanion-worktrees/wt-c4`）。
> 证据统一落 `openspec/evidence/c4-webchat-protocol-dev-loop/`（含环境约束说明与验收标准→证据映射）。

## 1. 协议契约与身份

- [x] 1.1 `infra/channels/web_chat_protocol.py`：`hello` 增加 `account_id`/`tenant_id`/`conversation_id`；新增 `DEV_ACCOUNT_ID`、`CLOSE_IDLE_TIMEOUT`、`CLOSE_DEV_ONLY`、error code 词表常量。验证：`tests/test_web_chat_protocol_contract.py` fixture 形状断言
- [x] 1.2 `hello` 只回服务端派生身份，客户端 `send` 的 `tenant_id`/`account_id`/`session_key` 被忽略。验证：`tests/test_web_chat_dev_gate.py::test_client_declared_identity_is_ignored` + `::test_injected_identity_is_the_only_authority` + e2e 带伪造 tenant 的 send
- [x] 1.3 fixture `tests/fixtures/chat_protocol_frames.json` 同步新字段 + 错误用例预期错误码 + 身份/档位常量。验证：前后端两侧消费同一文件（`pytest-protocol-and-gate.txt` + `frontend-protocol-contract.txt`）

## 2. dev-only 门禁

- [x] 2.1 `agent/config_models.py` / `agent/config.py` / `config.example.toml`：`[channels.chat]` 增加 `idle_timeout_s`（默认 90）、`allow_public_bind`（默认 false）。验证：`tests/test_web_chat_dev_gate.py` + 全量回归
- [x] 2.2 `bootstrap/chat_api.py::build_chat_server`：非 dev 模式拒绝启动；非回环 host 需显式 `allow_public_bind`。验证：`test_build_chat_server_requires_dev_mode`、`::test_build_chat_server_rejects_non_loopback_bind_without_opt_in`
- [x] 2.3 `bootstrap/chat_api.py::_DevOnlyGuardMiddleware`（纯 ASGI）运行期回环门禁：HTTP 403 / WS 1008，覆盖静态挂载点。验证：`test_runtime_guard_rejects_non_loopback_http`、`::..._websocket`、`::..._allows_loopback`、`::..._opt_in_disables_layer`
- [x] 2.4 `bootstrap/app.py`：`channels.chat.enabled` 且非 dev 模式 → fail-fast；并接线 `dev_mode` / `idle_timeout_s` / `allow_public_bind`。验证：`test_build_chat_server_requires_dev_mode` + 回归

## 3. 连接生命周期

- [x] 3.1 `WebChatChannel` 空闲读超时 → `close(CLOSE_IDLE_TIMEOUT)` + 从 `_connections` 移除 + outbound 队列回收。验证：`tests/test_web_chat_e2e_dev.py::test_e2e_idle_connection_is_reaped`
- [x] 3.2 前端 `connection.ts` keepalive `ping`（25s）+ `protocol.ts` 新类型/常量。验证：`npm run test:chat-protocol`（31/31）

## 4. 契约 fixture 双向执行

- [x] 4.1 后端 `tests/test_web_chat_protocol_contract.py`：fixture 参数化（形状 + 常量 + 重放语义 + 错误用例 → 预期错误码）。验证：`pytest-protocol-and-gate.txt`（49 passed）
- [x] 4.2 前端 `frontend/chat/scripts/protocol-contract.test.mjs` + `package.json::test:chat-protocol`：对同一 fixture 断言常量/帧类型联合/HelloFrame 字段/错误码。验证：`frontend-protocol-contract.txt`（31/31；零依赖，因本机无 node_modules）

## 5. 端到端 dev 闭环

- [x] 5.1 `tests/test_web_chat_e2e_dev.py`：真实 `create_chat_app` + 真实 uvicorn + 真实 WS 客户端，dev 模式 hello/send/accepted/delta/turn.completed。验证：`pytest-e2e-dev.txt::test_e2e_dev_send_receives_stream_and_completion`
- [x] 5.2 重连补拉：断线 → 重连 → `replay{after_seq}` 补拉无重复、顺序稳定；gap 超出 buffer → `replay_required` → REST 重建通路可达。验证：`::test_e2e_reconnect_replay_no_duplicates`、`::test_e2e_replay_cursor_beyond_buffer_requires_rest_rebuild`、`::test_e2e_rest_history_rebuild_endpoint_reachable`
- [x] 5.3 断线不取消 turn/tool：断线后服务端 turn 继续跑完并保留终态供补拉。验证：`::test_e2e_disconnect_does_not_cancel_turn`（负向语义断言）
- [x] 5.4 慢消费者默认档位矩阵：soft 192 丢 delta + `replay_required`；hard 256 / 1 MiB → overload close 1013。验证：`tests/admission/test_ws_outbound.py`（7 passed）+ `test_channel_defaults_use_frozen_ws_limits`

## 6. 回归与状态

- [x] 6.1 修复 `tests/test_chat_api.py` 第三方 anyio 弃用告警导致的收集失败（import 点局部抑制，不改全局 `-W error`）。验证：`pytest-protocol-and-gate.txt` 含 `tests/test_chat_api.py` 全部用例通过
- [x] 6.2 `pyright`（project + tests 两配置）无**本 change 引入**的新错误。验证：`pyright-project.txt` / `pyright-tests.txt` / `pyright-comparison.txt`（PROJECT new=0；TESTS new=3，全部为 main 密文掩蔽的既有错误，见 evidence/README.md §3）
- [x] 6.3 `pytest -q -W error` 回归：1192 passed / 62 skipped / 1 failed（唯一失败为 Windows 无符号链接权限，已在 main 复现）。验证：`pytest-regression.txt` + evidence/README.md §4
- [x] 6.4 PR diff 范围检查：未触碰 auth 端点（C5）与 Telegram binding（C10）。验证：`diff-scope-check.txt`
- [x] 6.5 task-04 状态更新 + evidence 落 `openspec/evidence/c4-webchat-protocol-dev-loop/`

## 7. 已知缺口与超范围（供 reviewer / 下游 change 参考）

- **PG durable 补拉未在本机验证**：环境无本地 PG（`alembic` 亦不在 `requirements.txt`），
  `tests/canonical_identity`、`tests/control_plane`、`tests/migration` 三个 C1/C2 PG 集成套件
  无法收集，已从回归中 `--ignore` 并在 evidence 登记。e2e 的 REST 重建只断言端点可达与响应形状；
  canonical message 的实际写入由 AgentLoop 负责（本测试用 stub worker 代替），
  持久层「final 从 canonical message 补拉」语义由 C2 规格覆盖。
- **前端 bundle 未构建/未渲染验证**：本机无 `node_modules`（且 venv 无 pip），
  `npm run build:chat` / `tsc` / `eslint` 未运行。前端证据是**源码级契约断言**
  （常量、帧类型联合、HelloFrame 字段、错误码词表），不是浏览器渲染或类型检查证据。
- **真实 LLM 未接入**：e2e 用 stub worker 表达 AgentLoop 的 delta/终态时序，不验证真实模型链路。
- **公网暴露只做负向阻断验证**：按 P0.5 出口要求未做任何公网可达性配置；
  P1 接 C5 认证时应以 principal 认证替换 `_DevOnlyGuardMiddleware` 的运行期回环层，
  保留配置层与绑定层作为额外兜底（design ADR-3）。
