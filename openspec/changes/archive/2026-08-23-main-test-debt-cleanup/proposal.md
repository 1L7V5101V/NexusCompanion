## Why

main 分支的测试套件不可用：近几次重构（proactive_v2 → plugins/ 插件化、loop 快照与 turn 生命周期、插件系统、通道体系、mcp 源 ack 分组）改了生产代码但没有同步更新对应测试。2026-08-23 在干净 main 上实测：分类一（孤儿测试，引用已删模块）已随 `0a83314d` 合入时删除；剩余 **80 项失败** 分布在 23 个测试文件（针对 archive 分类二~四的定向复跑），全量 pytest 无法作为回归 gate。`scaling-governance` 将「代码·测试·证据」列为三大事实来源，套件不绿意味着 Phase 1B/1C（M5–M7）合入 main 时无法用 pytest 判定零回归。必须先恢复 main 测试套件健康，再承接后续 phase 合并。

## What Changes

- **修复过期测试**（分类二）：测试对象仍在，但断言/接口随重构漂移。逐文件对齐到当前生产语义：
  - `test_turn_pipelines.py`（6）、`test_loop_tool_visibility.py`（12）——turn 生命周期接线与 LRU 工具可见性
  - `test_tool_loop_guard.py`（10）、`test_tool_discovery_routing.py`（2）——loop guard / 工具路由
  - `test_mcp_sources_async.py`（8）、`test_bootstrap_wiring_p2.py`（1）、`test_bootstrap_toolsets_p1.py`（1）
  - `test_support_modules.py::test_loop_trigger_and_main_entry_cover_paths`（1）
- **修复插件系统测试**（分类三）：`test_plugin_manager.py`（20）、`test_plugin_doctor.py`（2）、`test_plugin_config_schema.py`（1）、`test_plugin_install.py`（2）——tool hooks 接线、config 注入、插件生命周期；`test_plugin_install.py` 含 git 安装，需一并排查网络/凭证依赖。
- **修复其他零散失败**（分类四）：`test_channel_host.py`（2）、`test_channel_clients.py`（1）、`test_telegram_utils.py`（1）、`test_io_modules.py`（1）、`test_memory_optimizer.py`（3）、`test_memory_engine_contract.py`（1）、`test_pre_execution_interceptor.py`（1）、`test_procedure_hint_semantic.py`（1）、`test_spawn_tool_call_baseline.py`（2）、`test_akasha_plugin.py`（1）。
- **挂起防护**：安装/登记 `pytest-timeout`，用 `--timeout` 运行使结果可复现、可定位挂点。
- **验收口径**：main 上全量 `python -m pytest -q --timeout=90 --continue-on-collection-errors` 达到 0 failed / 0 collection error，输出与 commit 作为 evidence。
- **Non-Goals（显式排除）**：
  - 不重写测试框架或引入新的测试基建（beyond pytest-timeout）。
  - 不改动生产行为：以「测试对齐当前已发布行为」为默认；仅在测试断言的是文档/规格中明确声明的意图行为、且生产代码确实回归时才修生产代码，且每个此类修复单独标注并附回归原因。
  - 不处理与本次失败清单无关的其他测试优化（覆盖率、flake、测试基建重构）。
  - 不在本 change 内推进 Phase 1B/1C（M5–M7）实现；本 change 只恢复 main 测试基线。

## Capabilities

### New Capabilities
无（测试维护/工具修复，无生产行为变更，`skip_specs: true`）。

### Modified Capabilities
无（`openspec/specs/scaling-governance/spec.md` 的 12 条 requirement 不变；本 change 是让 main 测试基线满足既有「代码·测试·证据」事实来源，而非变更 requirement）。

## Impact

- **测试**：约 23 个测试文件修改/删除过期用例；新增 `test_storage_factory.py`、`test_storage_parity.py` 为 Phase 1 存储层测试（已存在，不属本 change 范围）。
- **依赖**：`pytest-timeout` 加入开发依赖（防全量挂起，需可复现）。
- **生产代码**：默认不改；若某失败测试揭示真实回归，按上述 Non-Goals 边界规则逐例修复并记录。
- **工具/CI**：全量 pytest 命令恢复为可用 gate；不涉及部署或配置。
