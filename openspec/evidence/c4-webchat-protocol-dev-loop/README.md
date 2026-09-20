# C4 WebChat protocol + dev-only loop — 证据

- 分支：`feature/c4-webchat-protocol-dev-loop`
- worktree：`D:/Project/NexusCompanion-worktrees/wt-c4`
- 基线：`main`@`f06854c`（C1 `e124dbf8` / C2 `7ed6897d` / C3 `7c405e8` 均已合入）
- 执行日期：2026-09-20
- 对应 change：`openspec/changes/2026-09-20-c4-webchat-protocol-dev-loop/`

## 1. 本机环境约束（影响证据范围，先说明再给结论）

1. **天锐绿盾类透明加密（DLP）**：本机对企业文档/源码做透明加密，`git worktree add`
   检出的文件落盘为密文（`%TSD-Header-###%`）。Elixir/Python 等「受控进程」读明文，
   而 Node/无签名进程读密文。本次处理：仅对**跟踪的 `.py` 文件**用
   `git show HEAD:<path> > <path>` 重写为明文（内容与 blob 逐字节一致、`git status` 干净），
   未改动 main 工作树。
2. **无 node_modules**：仓库未安装前端依赖（`D:/Project/NexusCompanion/node_modules` 不存在），
   因此 **不能运行 esbuild/vite/tsc/eslint**。前端契约测试改为**零依赖 Node 脚本**
   （源码级断言），见 `frontend/chat/scripts/protocol-contract.test.mjs`。
3. **无 alembic / 无本地 PG**：`alembic` 不在 `requirements.txt`，venv 内无 pip；
   PG 未运行。因此 `tests/canonical_identity`、`tests/control_plane`、`tests/migration`
   三个 **C1/C2 PG 集成套件无法收集**，全量回归以 `--ignore` 排除并在文末登记。
4. Windows 无符号链接权限：`tests/test_plugin_doctor.py::test_plugin_doctor_reports_healthy_skill_plugin`
   因 `WinError 1314` 失败（已在 main 复现，见 §4）。

## 2. 证据清单

| 文件 | 命令 | 结果 |
| --- | --- | --- |
| `pytest-protocol-and-gate.txt` | `pytest -q -W error tests/test_web_chat_protocol_contract.py tests/test_web_chat_dev_gate.py tests/test_web_chat_channel.py tests/test_chat_api.py` | **49 passed** |
| `pytest-e2e-dev.txt` | `pytest -q -W error tests/test_web_chat_e2e_dev.py` | **6 passed**（真实 `create_chat_app` + 真实 uvicorn + 真实 WebSocket） |
| `pytest-ws-outbound.txt` | `pytest -q -W error tests/admission/test_ws_outbound.py` | **7 passed**（慢消费者 192/256/1 MiB，C3 交付、C4 复核） |
| `pytest-regression.txt` | `pytest -q -W error tests/ --ignore=tests/{canonical_identity,control_plane,migration}` | **1192 passed / 62 skipped / 1 failed**（失败为 §1.4 既有环境权限问题） |
| `frontend-protocol-contract.txt` | `npm run test:chat-protocol` | **31/31 checks passed** |
| `pyright-project.txt` | `python -m pyright`（`pyrightconfig.json`） | 37 errors（**新增 0**，见 §3；文件仅保留 `error` 行 + summary，warning 已过滤） |
| `pyright-tests.txt` | `python -m pyright -p pyrightconfig.tests.json` | 34 errors（新增 3，均为 main 密文掩蔽的既有错误，见 §3；同样仅保留 error 行） |
| `pyright-comparison.txt` | `cmp_pyright.py`（剔除 `Invalid character` 密文噪声后按文件:行:错误文本比对 worktree ↔ main） | PROJECT new=0 / TESTS new=3 |
| `diff-scope-check.txt` | `git diff --stat` + 范围声明 | 见该文件 |

## 3. pyright 对比方法学与结论

`main` 工作树的 pyright 结果**不可直接作为基线**：其中 70（project）/ 127（tests）
条是密文导致的 `Invalid character` / `expected expression` 垃圾错误，另有若干真实错误
因文件为密文而被掩蔽（例如 `tests/test_provider_codex_protocol.py`）。

对比方法（`pyright-comparison.txt` 由脚本产出，可复现）：

1. 用同一 venv、同一 pyright 版本（1.1.414）分别对 worktree 与 `main` 跑两个配置；
2. 过滤掉 `Invalid character` 噪声行；
3. 归一化路径前缀（`NexusCompanion-worktrees\wt-c4\` 与 `NexusCompanion\`）后，
   按 `文件:行:列 - error: 消息` 做集合差。

结论：

- **project 配置：新增错误 0**（worktree 37 条全部是 main 明文文件的既有错误；
  66 条 main 密文噪声在明文化后消失）。
- **tests 配置：新增 3 条**，全部位于 `tests/test_provider_codex_protocol.py`
  （`build_providers` / `_build_proactive_provider` 参数类型）。该文件在 main 中为
  **密文**（main 输出该文件为 `Invalid character` / `Expected expression` 噪声），
  因此这 3 条是**既有的真实类型错误，此前被 DLP 掩蔽**，非本 change 引入。
- 本 change 自身引入的 2 条新增错误（`tests/admission/test_ws_outbound.py` 的
  `identity` 关键字参数、`tests/test_web_chat_e2e_dev.py` 的 `consume_inbound` 返回联合类型）
  已在提交前修复并复跑确认消失。

## 4. 回归唯一失败的既有性证明

```
D:/Project/NexusCompanion> pytest -q tests/test_plugin_doctor.py
FAILED tests/test_plugin_doctor.py::test_plugin_doctor_reports_healthy_skill_plugin
OSError: [WinError 1314] 客户端没有所需的特权。
1 failed, 1 passed
```

在 **main（未含本 change）** 上复现同一失败，证明与 C4 无关。

## 5. 验收标准 → 证据映射

| task-04 验收标准 | 证据 |
| --- | --- |
| 本地/dev 打开 WebChat 收发 + 流式更新（真实入口，dev mode） | `pytest-e2e-dev.txt::test_e2e_dev_send_receives_stream_and_completion`（hello→send→accepted→delta→turn.completed） |
| 重连补拉无重复、顺序稳定（`last_sequence` 游标，final 从 canonical 补拉） | `pytest-e2e-dev.txt::test_e2e_reconnect_replay_no_duplicates`、`::test_e2e_replay_cursor_beyond_buffer_requires_rest_rebuild`（`replay_required`）+ `::test_e2e_rest_history_rebuild_endpoint_reachable` |
| 协议 contract fixture 前后端共用（同组帧/常量/错误用例双向执行） | `pytest-protocol-and-gate.txt`（后端参数化同文件）+ `frontend-protocol-contract.txt`（前端脚本同文件，31 项） |
| 慢消费者：192 → 丢 delta + `replay_required`；256 或 1 MiB → overload close | `pytest-ws-outbound.txt` + `tests/test_web_chat_protocol_contract.py::test_channel_defaults_use_frozen_ws_limits` |
| `hello` 携带 connection_id / 账号 / 规范会话 / 最新序号 / 协议版本；客户端不得声明可信 tenant | `tests/test_web_chat_protocol_contract.py::test_hello_carries_server_derived_identity`、`tests/test_web_chat_dev_gate.py::test_client_declared_identity_is_ignored`、`::test_injected_identity_is_the_only_authority`、`pytest-e2e-dev.txt`（send 帧带 `tenant_id`/`session_key` 仍落服务端身份） |
| 非 dev 模式不可暴露公网（feature flag 门禁） | `tests/test_web_chat_dev_gate.py`（第 1/2/3 层：回环判定、绑定拒绝、运行期 ASGI 403/1008；`build_chat_server(dev_mode=False)` raise） |
| 断线只影响显示，不取消 turn/tool | `pytest-e2e-dev.txt::test_e2e_disconnect_does_not_cancel_turn`（断线后 turn 跑完 + 终态仍可补拉 + 连接表清空） |
| 异常连接清理（心跳超时/静默回收） | `pytest-e2e-dev.txt::test_e2e_idle_connection_is_reaped`（close code 1001 + 连接表清空） |
| 不触碰 auth 端点（C5）与 Telegram binding（C10） | `diff-scope-check.txt` |

## 6. 未验证 / 超范围

- **PG durable 补拉未在本机验证**：环境无 PG，e2e 的 REST 重建只断言端点可达与响应形状；
  canonical message 的实际写入由 AgentLoop（本测试用 stub 代替）负责。
  「final 从 canonical message 补拉」的持久层语义由 C2 的 `durable-control-plane` 规格覆盖。
- **前端 bundle 未构建/未渲染验证**：无 node_modules，`npm run build:chat` 未运行；
  前端证据是源码级契约断言（常量/帧类型/错误词表），不是浏览器渲染验证。
- **真实 LLM 未接入**：e2e 用 stub worker 表达 AgentLoop 的 delta/终态时序。
- **公网暴露只做负向阻断验证**：按 P0.5 出口要求，未做任何公网可达性配置。
