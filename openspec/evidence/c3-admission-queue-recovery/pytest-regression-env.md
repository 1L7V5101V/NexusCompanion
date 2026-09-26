# C3 §5.2 全量回归 — 环境说明与判读（2026-09-20）

## 本次运行

- 命令：`.venv/Scripts/python.exe -m pytest -q -W error tests/`（`pytest.ini` 已含 `addopts = -W error`）
- 工作树：`D:/Project/NexusCompanion`（`main` @ `5e959e1`）
- 结果：**1 failed, 1193 passed, 166 skipped**（422.57s）；原始输出见同目录 `pytest-regression.txt`
- 采集总数 1360 = 1193 + 166 + 1（`pytest --collect-only -q tests/`）

## 与既往证据的差异（均为环境，非 C3 回归）

| 项 | 既往证据（2026-09-06，C3 worktree `d:/1/wt-nexus-c3`） | 本次（2026-09-20，main） |
| --- | --- | --- |
| 通过 | 1255 | 1193 |
| 失败 | 0 | 1（环境性，见下） |
| 跳过 | 0 | 166（环境性，见下） |
| PostgreSQL | 在线（localhost:5433，pgvector/pg17） | **不可用**（5432/5433/5434 均关闭；无 docker；PG18 装在 `C:/Program Files/PostgreSQL/18` 但无 data dir、无 pgvector 扩展、无服务） |
| symlink 权限 | 可用 | **不可用**（未开 Windows 开发者模式） |

## 失败项判读：`tests/test_plugin_doctor.py::test_plugin_doctor_reports_healthy_skill_plugin`

- 原因：`OSError [WinError 1314] 客户端没有所需的特权`。测试用 `Path.symlink_to(..., target_is_directory=True)` 建目录符号链接（`tests/test_plugin_doctor.py:39`），本机非管理员且未开启开发者模式，无法创建符号链接。
- 独立复现：直接调用 `os.symlink(dir, link, target_is_directory=True)` 同样抛 WinError 1314，与插件代码无关。
- 与 C3 无关：该测试 2026-07-05 由插件外置化引入（`82d1e88`，PR #96），2026-08-23 债务清理（`e67e56e`）后仍在；C3 只改 `agent/admission/**`、`tests/admission/**`、`bus/queue.py` 等 admission 路径，不触碰 plugin doctor。
- 判读：**环境性、既有、非 C3 归因**。该测试缺少「符号链接不可用则 skip」的守卫，属既有测试债务（无开发者模式的 Windows 上必然失败，与代码无关）。

## 跳过判读：166 项跳过全部为环境性

- 117 项为 `postgres` marker（`pytest --collect-only -q -m postgres` 计数 = 117/1360）。
- 其余为测试内 `pytest.skip("本地 PG 不可用…")` 守卫（`tests/test_storage_factory.py`、`test_storage_parity.py`、`test_storage_pool.py`、`test_storage_runtime.py`、`test_provisioning.py`、`test_provisioning_worker.py`、`test_provisioning_turn_gate.py`、`test_partition_readiness.py`、`test_tenant_isolation.py`、`test_memory_undo.py`、`test_dashboard_api.py`、`test_dashboard_undo_readiness.py`、`test_trace_backend_parity.py`），以及 `tests/canonical_identity/conftest.py`、`tests/control_plane/conftest.py`、`tests/migration/conftest.py` 的整组 skip。
- 另有 `tests/test_fast_rebuild_parity.py`（rachael host 依赖缺失，模块级 skip）。
- **无任何测试被显式 disable/ignore 以掩盖失败。**

## 结论

- 本次运行**无 C3 归因的新增失败**，§5.2「全量回归无新增失败」成立。
- 但基线本身存在两项未清的**环境依赖**，导致「全量绿」无法在本机复现，且不在 C3 范围内：
  1. **无 PG** ⇒ C1/C2 identity/control-plane 集成测试（117+ 项）整体跳过，公网 durability 相关断言未被本次运行覆盖。建议在带 PG（pgvector/pg17）的环境复跑，或在 CI 固化 PG 服务。
  2. **`test_plugin_doctor` 依赖 symlink 权限且无守卫** ⇒ 无开发者模式的 Windows 上必失败。建议补 `os.symlink` 可用性守卫（属测试债务，单独处置）。
- 此两项按「source of truth：已验证代码与测试证据」记录在案，不作为 C3 的阻塞项；如需「全量绿」基线证据，须先清此两项。
