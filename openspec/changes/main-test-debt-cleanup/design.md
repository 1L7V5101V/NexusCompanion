## Context

main 测试债务的现状与成因已在 proposal.md 说明。设计基线（2026-08-23 实测）：

- **80 项失败**集中在 archive 分类二~四的 23 个文件（定向复跑），分类一孤儿测试已随 `0a83314d` 删除，无收集错误。
- **全量 suite 会挂起**：某网络依赖测试在 asyncio `select` 上超 90s（`--timeout=120` 可截断，已复现），与 archive 环境性注意一致。
- **根因已抽丝（traceback 验证），多数是单一构造点漂移**：

| 根因 | 触发文件 | 影响量 |
|---|---|---|
| `MemoryServices.__init__() got an unexpected keyword argument 'engine'` | `test_loop_tool_visibility`(12)、`test_tool_loop_guard`(10)、`test_turn_pipelines`(6)、`test_spawn_tool_call_baseline`(2)、`test_tool_discovery_routing`(2)、`test_procedure_hint_semantic`(1)、`test_pre_execution_interceptor`(1) | ~34 |
| `proactive_v2.mcp_sources` 无 `_load_sources`（模块 API 重命名/ack 分组变更） | `test_mcp_sources_async`(8) | 8 |
| 插件 before_turn hook 未触发（`BeforeTurnCtx.extra_metadata` 为空） | `test_plugin_manager`(20)、`test_plugin_doctor`(2)、`test_plugin_config_schema`(1) | 23 |
| `channel_host` 构造漂移（字符串 channel name → 需含 `.bus` 的 channel 对象） | `test_channel_host`(2) | 2 |
| memory optimizer mock 契约漂移（`StopAsyncIteration`） | `test_memory_optimizer`(3) | 3 |
| bootstrap 工厂/工具集注册变更 | `test_bootstrap_wiring_p2`(1)、`test_bootstrap_toolsets_p1`(1)、`test_support_modules`(1) | 3 |
| 环境依赖（git/网络/凭证） | `test_plugin_install`(2)、`test_channel_clients::test_telegram_channel_paths`(1) | 3 |
| 零散断言漂移 | `test_akasha_plugin`(1)、`test_telegram_utils`(1)、`test_io_modules`(1)、`test_memory_engine_contract`(1) | 4 |

`MemoryServices` 当前为 `engines: dict[str, MemoryEngine]`（`agent/looping/ports.py:62`，M4.5 多引擎硬化），旧的 `engine=` 单引擎构造已删；测试侧 9 文件 11 处仍用旧构造。这是本 change 最大、最可机械化的单点。

## Goals / Non-Goals

**Goals:**
- 让 main 全量 pytest 恢复为可用的回归 gate（0 failed / 0 collection error，`--timeout` 下无挂起）。
- 测试代码与当前生产契约对齐；对真实生产回归保留修复通道。
- 结果可复现、可作 evidence（pytest 输出 + commit）。

**Non-Goals:**
- 不改生产行为默认；仅当测试断言的是文档/规格明确意图行为且生产确实回归时修生产，每例单独标注。
- 不做覆盖率/flake/测试基建重构（pytest-timeout 除外）。
- 不推进 Phase 1B/1C（M5–M7）实现；本 change 只恢复 main 测试基线。

## Decisions

### D1：`MemoryServices` 构造漂移——系统性一次性修复（首选机制化）

**结论**：把测试侧 11 处 `MemoryServices(engine=X)` 统一改为新契约 `MemoryServices(engines={"default": X})`；`engine=` 的位置改由 `.engine` property 兼容（该 property 仍返回第一个 engine，测试断言不变）。改完后 grep `MemoryServices(engine=` 清零，并定向跑受影响文件全绿。

**备选（不选）**：给 `MemoryServices.__init__` 加回 `engine=` 兼容参数——会掩盖生产契约、让未来测试继续用废弃构造，且与 M4.5 多引擎决策相悖。**不选理由**：欠账应还到正确侧（测试），而非给生产代码加 shim。

### D2：修复方向判断准则——「测试对齐生产」为默认，生产修复为特例

**结论**：重构（插件化、loop/turn 生命周期、M4.5 硬化）是有意的当前行为；测试漂移是维护债务。修复时若测试断言与当前生产行为不一致：先读生产代码确认当前意图行为，按当前意图改测试；仅当生产行为明显违反其文档/规格（如 docstring、spec 明确声明但代码回归）时改生产，且每个此类修复在 PR 描述标注根因。

**备选（不选）**：凡红就改生产。**不选理由**：会把有意重构回退成旧行为，放大错误方向。

### D3：删除 vs 修复——对象消失才删，接口漂移只改

**结论**：分类一（对象已删）已在 main 完成删除，本 change 不再删文件级孤儿。对 `test_plugin_install`（git 安装）、`test_channel_clients::test_telegram_channel_paths`（凭证）这类**环境依赖**项，用 skip-if-条件标记而非删除——测试逻辑仍有效，只是本地缺环境。

**备选（不选）**：直接删环境依赖测试。**不选理由**：损失有效覆盖；条件 skip 保留 CI/有环境时的回归价值。

### D4：挂起防护——`pytest-timeout` 进 dev 依赖

**结论**：`pytest-timeout` 加入 `requirements-dev.txt`；全量用 `python -m pytest -q --timeout=120 --continue-on-collection-errors` 运行。挂点测试单独定位：网络依赖用 skip 条件、真挂死用 pytest-timeout 的 `--timeout-method` 与 `@pytest.mark.timeout` 收敛。

**备选（不选）**：不加 timeout 靠人工盯。**不选理由**：全量挂起不可定位、不可复现，archived 已知问题。

### D5：验证策略——分组修复 + 全量绿 + evidence

**结论**：每个根因/文件组修复后定向跑该组（`pytest <files> --timeout=120`）；全部修完后全量复跑记录 `0 failed`。evidence = 全量 pytest 输出 + 修复 commit。修复顺序按「先单点根因（D1 的 MemoryServices）→ 再插件/mcp 等中簇 → 后零散与环境项」，让最大影响最先绿。

### D6：与 in-flight M5–M7 的冲突边界

**结论**：本 change 只动 `tests/` 与 `requirements-dev.txt`。phase1b（`feature/scaling-phase1-storage` worktree + `phase1b-migration-cutover` change）聚焦 storage 层；若后续合并时共享 turn/loop 测试文件冲突，以「本 change 修的 main 当前语义」为准并在 merge 时人工核对。

**备选（不选）**：等 M5–M7 合完再做。**不选理由**：内存与 archive 记录该清理即为「M5–M7 合入前恢复基线」的决策（合并零回归判定依赖可用 suite）；现在做不阻塞 phase1b，只降低其合并噪声。

### D7：本 change 唯一生产修复——`agent/plugins/manager.py` 插件系统回归修复（D2 特例）

**结论**：按 D2 准则（仅当生产行为明显违反其文档/规格、确为回归时修生产），本 change 含**一处**生产修复，根因与回归证据如下：

- **修复**：`PluginManager._publish_plugin` 恢复 `self._bind_handlers(instance, mp, scope)` 调用（在 `_register_tools` 前）；`_load_plugin_config` 增加 `overrides` 参数，`_load_module` 处以 `self._plugin_configs.get(name, {})` 注入插件 config 覆盖。
- **根因一（lifecycle hook 未接线）**：插件系统重构时把 `_bind_handlers(` 调用点删掉，仅剩定义（HEAD `manager.py:2585`）。`git log -S "_bind_handlers(" -- agent/plugins/manager.py` 证实调用点存在于 `9154a408`/`c4be5413`，HEAD 已无调用 → before_turn / after_step / on_tool_call / on_tool_result 等生命周期 hook 在 HEAD 生产完全失效。
- **根因二（config 覆盖未消费）**：`PluginManager.__init__` 接受并存储 `plugin_configs`（`manager.py:167`）但从未消费；`git log -S "_plugin_configs.get"` 证实消费逻辑存在于 `0ad9c13a`/`82d1e883`/`c4be5413`，HEAD 已丢失 → 程序化 config 注入（含 bare-name config fallback）失效。
- **回归证据**：撤修复后 9 项插件测试失败（`test_before_turn_hook_fires`、`test_after_step_tap_hook_fires`、`test_counter_increments_extra_metadata`、`test_kv_store_persists_across_manager_instances`、`test_installed_plugin_uses_bare_name_config_fallback`、`test_on_tool_call_fires_before_tool_execution`、`test_on_tool_result_fires_after_tool_execution`、`test_tool_hooks_fire_through_real_reasoner`、`test_plugin_config_model_validates_and_injects_config`）；恢复后 `tests/test_plugin_manager.py test_plugin_doctor.py test_plugin_config_schema.py test_plugin_install.py` 39 项全绿。
- **不修的理由被否决**：若保留 HEAD 死代码、把 9 项测试改为断言「hook 不触发 / config 不注入」，等于把插件系统文档化的核心能力删掉，丢失真实回归覆盖，与 D3「保留有效覆盖」相悖。

## Risks / Trade-offs

- **R1 误把生产 bug 当测试漂移覆盖** → D2 准则 + 每例生产修复单独标注根因与回归证据。
- **R2 修 MemoryServices 时漏改/改错 call-site**（9 文件 11 处）→ 修复后 `grep -rn "MemoryServices(engine=" tests/` 清零核对 + 受影响文件定向全绿。
- **R3 全量 suite 挂点若不收敛则 CI 仍不可用** → D4 用 timeout 定位 + 环境项 skip；挂死项单独 PR 记录。
- **R4 `test_plugin_install`（git）与 `test_telegram_channel_paths`（凭证）本地不可测** → D3 条件 skip；在有环境的 CI/机器复跑确认。
- **R5 与 phase1b 分支测试文件冲突** → D6 边界；merge 时人工核对。
- **R6 pytest.ini `-W error`（addopts）可能把 warning 计为失败** → 修复时若遇 warning-errors 一并处理（修 source 或按需过滤），不靠放宽 `-W error`。
- **回滚**：本 change 的改动范围 = `tests/` + `requirements-dev.txt` + **一处** `agent/plugins/manager.py` 生产修复（D7，恢复插件系统两处被重构丢弃的接线，使 9 项插件测试从红转绿）。回滚 = revert 相关 commit；manager.py 若 revert 会让上述 9 项测试复红，故 revert 需连同 D7 测试一并处理。证据链保留在 git。

## Migration Plan

1. D1 优先：MemoryServices 构造 11 处一次性改齐 → 定向跑 7 个受影响文件。
2. mcp_sources（8）→ 插件族（23）→ bootstrap（3）→ 其余零散（4）→ 环境依赖项 skip（3），逐组修复 + 定向验证。
3. 挂点定位并收敛（D4）。
4. 全量复跑记录 `0 failed / 0 collection error`，pytest 输出存档 evidence。
5. 更新 `requirements-dev.txt`（pytest-timeout）；`openspec validate` 通过。

## Open Questions

- 插件族 20 项失败是否共享同一构造点根因（可一次修齐）还是逐例接线差异——apply 时按 traceback 归类，不影响本方案结构，可安全延后。
- 是否存在 archive 清单之外的其它失败文件——全量复跑输出为准（挂起收敛后），不改变分类方法与任务结构。
