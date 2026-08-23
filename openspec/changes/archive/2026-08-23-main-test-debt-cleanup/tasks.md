## 1. 基线确认与工具

- [x] 1.1 复跑全量 suite 记录当前基线：`python -m pytest -q --timeout=120 --continue-on-collection-errors`，确认 80 项失败清单（archive 分类二~四）与无收集错误；挂起测试是否仍存在
- [x] 1.2 将 `pytest-timeout` 加入 `requirements-dev.txt`，`pip install -r requirements-dev.txt` 成功，`python -m pytest --help | grep timeout` 可见 `--timeout` 选项
- [x] 1.3 建立临时分支（推荐 `fix/main-test-debt`，按团队惯例亦可直接 main），记录 baseline 输出为 evidence 起点

## 2. MemoryServices 构造漂移（~34 项，单一根因优先）

- [x] 2.1 全量核对 `grep -rn "MemoryServices(engine=" tests/`，把 11 处旧构造改为新契约 `MemoryServices(engines={"default": <engine>})`；涉及 `test_loop_tool_visibility`、`test_tool_loop_guard`、`test_turn_pipelines`、`test_spawn_tool_call_baseline`、`test_tool_discovery_routing`、`test_procedure_hint_semantic`、`test_pre_execution_interceptor`（另含 `test_spawn_completion_flow`、`test_agent_core_foundation` 未失败但同模式，一并核对）
- [x] 2.2 验证：`grep -rn "MemoryServices(engine=" tests/` 结果为零；定向跑上述 7 个失败文件 `python -m pytest <files> --timeout=120` 全绿
- [x] 2.3 若个别文件改后仍红，逐个确认是 D2 特例（生产真实回归）还是断言漂移，按 D2 准则处理并标注

## 3. mcp_sources 模块 API（8 项）

- [x] 3.1 对照 `proactive_v2/mcp_sources.py` 当前 API（`_load_sources` 已改名、ack 按 ack_server 分组、fetch 支持 list 参数），更新 `test_mcp_sources_async.py` 的 8 个失败用例
- [x] 3.2 验证：`python -m pytest tests/test_mcp_sources_async.py --timeout=120` 全绿

## 4. 插件族（23 项）

- [x] 4.1 用 traceback 归类 `test_plugin_manager.py` 20 项失败：确认是否共享同一构造点根因（before_turn/tool hooks 接线、config 注入、生命周期），按当前 `agent/plugins/manager` 与 `bootstrap/tools.py` 契约对齐
- [x] 4.2 修复 `test_plugin_doctor.py`（2）与 `test_plugin_config_schema.py`（1）对插件 doctor/config 契约的断言
- [x] 4.3 验证：`python -m pytest tests/test_plugin_manager.py tests/test_plugin_doctor.py tests/test_plugin_config_schema.py --timeout=120` 全绿
  - D7 特例：插件族修复含 **一处** 生产修复 `agent/plugins/manager.py`（恢复 `_bind_handlers` 接线 + `_load_plugin_config` 消费 `plugin_configs`），根因与回归证据见 design.md D7。恢复后 `test_plugin_install.py` 并入验证，39 passed。

## 5. bootstrap 工厂/工具集（3 项）

- [x] 5.1 修复 `test_bootstrap_wiring_p2.py::test_build_loop_deps_uses_context_factory`、`test_bootstrap_toolsets_p1.py::test_scheduler_toolset_provider_registers_expected_tools`、`test_support_modules.py::test_loop_trigger_and_main_entry_cover_paths`，对齐工厂/工具集注册与 loop 触发当前行为
- [x] 5.2 验证：上述 3 个文件 `python -m pytest <files> --timeout=120` 全绿

## 6. 通道体系（4 项）

- [x] 6.1 修复 `test_channel_host.py` 2 项：channel 启动失败隔离与逆序停止——对齐 `bootstrap/channel_host.py` 现契约（channel 需为含 `.bus` 的对象，非字符串名）
- [x] 6.2 `test_telegram_utils.py::test_send_thinking_block_splits_long_content`：对齐 Telegram utils 当前分块断言
- [x] 6.3 `test_channel_clients.py::test_telegram_channel_paths`：若为本地凭证/网络依赖，用 skip-if 条件标记（保留逻辑，非删除）
- [x] 6.4 验证：`python -m pytest tests/test_channel_host.py tests/test_telegram_utils.py tests/test_channel_clients.py --timeout=120` 全绿或按条件 skip

## 7. memory / io / 零散（13 项）

- [x] 7.1 修复 `test_memory_optimizer.py` 3 项（`StopAsyncIteration`：mock 契约对齐当前 optimizer 异步接口）与 `test_memory_engine_contract.py` 1 项（consolidation window 前进）
- [x] 7.2 修复 `test_io_modules.py`（1）、`test_akasha_plugin.py`（1）、`test_spawn_tool_call_baseline.py`（2）——其中 spawn/pre_execution/procedure_hint 若已随 2.1 解决则复核即可
- [x] 7.3 验证：上述文件 `python -m pytest <files> --timeout=120` 全绿（73 passed）

## 8. 挂点定位与收敛

- [x] 8.1 在全量 timeout 输出中定位挂死测试（asyncio `select` 超时），区分网络依赖（skip 条件）与真死循环（`@pytest.mark.timeout` 收敛或修复）
  - 挂点：`test_runtime_smoke.py::test_serve_smoke_loads_config_and_runs_shutdown` —— 非网络依赖、非真死循环，而是 stale 测试：只 no-op 了 `agent_loop.run`/`bus.dispatch_outbound`/`scheduler.run` 三个组件，当前 `AppRuntime.run()` 还 await 了 dashboard/plugin_watcher 等 supervised task，导致永不完成。D2 修复：`_FakeDashboardServer.serve()` 立即返回（当前 serve() 契约：任一 supervised task 完成即触发 run()→shutdown()）。修复后 `--timeout=30` 全量扫描 0 Timeout、完整跑完。
- [x] 8.2 验证：全量 `python -m pytest -q --timeout=120 --continue-on-collection-errors` 完整跑完、无挂起截断
  - 全量跑完无截断：`1031 passed in 105.56s`，0 Timeout、0 collection error。

## 9. 全量验证、evidence 与管理闭环

- [x] 9.1 全量复跑 `python -m pytest -q --timeout=120 --continue-on-collection-errors`，确认 `0 failed / 0 collection error`，输出存档为 evidence
  - **evidence（full-suite）**：`1031 passed in 105.56s (0:01:45)`，0 failed / 0 collection error / 0 挂起。基线对比：clean main = 83 failed + 5 collection errors → 本 change 后 1031 passed，合并零回归。evidence 同时保留在 git（本 change commit）。
- [x] 9.2 提交修复（commit 不加 Co-Authored-By、无 emoji），merge 到 main；记录 merge commit 为 evidence
  - commit `e67e56e4`（branch `fix/main-test-debt`）→ fast-forward merge 到 main（main tip = `e67e56e4`）；merge 后工作树仅剩无关的 phase1b/roadmap 改动。evidence：commit + tasks/design 记录。
- [x] 9.3 `openspec status --change main-test-debt-cleanup` 确认 artifacts 齐备；更新本 tasks 全部 checkbox
  - `openspec status`：proposal.md / design.md / tasks.md 齐备，specs 按 `.openspec.yaml` skip_specs 正确缺席；全部 checkbox 已勾。
- [x] 9.4 若有 roadmap 相关行的证据引用受影响才更新 `openspec/SCALING_ROADMAP.md`（本 change 不新增/改动 capability 行；默认无更新）
  - 本 change 不新增/改动 capability 行，SCALING_ROADMAP.md 无更新（其工作树修改为 phase1b 无关内容，未触碰）。
