# C0 证据：任务 5.1 全量验证（pyright + pytest 相对 main 基线无回归）

- 记录日期：2026-08-23
- 分支：`feature/c0-observability`，全部 16 任务已勾选。
- 对比基线：`openspec/evidence/c0/baselines/baseline_main.md`（main 1031 passed / pyright project 37 errors / tests 21 errors）。

## pytest

- 命令：`.venv/Scripts/python.exe -m pytest -q -W error tests/`
- 结果：**1060 passed / 0 failed**（111.16s）
- 相对 main 基线 +29 条：均为本 change 新增测试（metrics registry / export / dashboard 指标 / trace / load harness），无本分支新增失败。

## pyright（project 配置）

- 命令：`.venv/Scripts/python.exe -m pyright -p pyrightconfig.json`
- 结果：37 errors / 4288 warnings，与 main 基线（37 errors）一致。
- 实现期间 `core/telemetry/builtin.py` 曾出现 6 条本分支新增 error（`Counter | Metric` 无法传给 `Counter` 参数），已用 `assert isinstance(...)` 收窄消除，回到 37 errors 基线。

## pyright（tests 配置）

- 命令：`.venv/Scripts/python.exe -m pyright -p pyrightconfig.tests.json`
- 结果：21 errors / 4543 warnings，与 main 基线（21 errors）一致。
- `tests/test_dashboard_metrics.py` 曾出现 3 条本分支新增 error（`_MemoryAdmin` 未满足 `MemoryAdminApi` Protocol、`_strict_json_loads` 返回 `object` 不可迭代），已修复（`cast` + 返回 `Any`），回到 21 errors 基线。

## 结论

任务 5.1「pyright（project + tests 两配置）与 pytest 相对 main 基线无回归」达成：
命令结果与 `openspec/evidence/c0/baselines/` 记录的 main 基线 diff 无本分支新增失败。
