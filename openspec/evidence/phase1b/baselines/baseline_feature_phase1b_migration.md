# Phase 1B 基线：feature/phase1b-migration 分支

## 创建时基线（分支 == main）

- 记录日期：2026-08-25
- 分支 commit：`998395e0dba9d9dedd2ef60d80dbdc019befe776`（HEAD == main，尚未产生代码改动）
- 与 main 基线差异：无（本分支从 main 创建）。

### pytest

- 命令：`.venv/Scripts/python.exe -m pytest -q -W error tests/`
- 结果：**998 passed / 62 skipped / 0 failed**（408.45s，exit 0）
- 62 skipped 为 PG-gated 集成测试（本机 5433 无 PG）。

### pyright（project 配置）

- 命令：`.venv/Scripts/python.exe -m pyright -p pyrightconfig.json`
- 结果：38 errors / 4290 warnings

### pyright（tests 配置）

- 命令：`.venv/Scripts/python.exe -m pyright -p pyrightconfig.tests.json`
- 结果：21 errors / 4543 warnings

## 最终验证（任务 5.1，2026-08-25，PG 可用）

- 记录时刻：全部 22 任务完成后，最终代码状态。
- 与 main 基线 diff 判据：pytest **0 failed** 且 `passed == 1060 + 新增`；pyright 两配置 **error 数不变**。

### pytest

- 命令：`.venv/Scripts/python.exe -m pytest -q -W error`（全量）
- 结果：**1096 passed / 0 skipped / 0 failed**（144.57s，exit 0）
- 对比：main 在 PG 可用时为 1060 passed / 0 failed；本分支新增 36 个 `tests/migration/*` 用例，1096 = 1060 + 36，无收集错误、无失败。`pytest -q -W error` 下无 warning/exception 泄漏。

### pyright（project 配置）

- 命令：`.venv/Scripts/python.exe -m pyright -p pyrightconfig.json`
- 结果：**38 errors / 4290 warnings** —— 与 main 完全一致，无新增 error。

### pyright（tests 配置）

- 命令：`.venv/Scripts/python.exe -m pyright -p pyrightconfig.tests.json`
- 结果：**21 errors / 4946 warnings** —— error 数与 main（21）完全一致；warnings 较 main 基线 +403，全部来自新增 `tests/migration/*.py` 文件（含在 `tests` 配置扫描范围内），无新增 error。

## 验证

- 分支 pytest 全量 1096 passed / 0 failed，pyright 两配置 error 数与 main 一致，无本分支新增失败。
