# C0 基线：feature/c0-observability 分支

- 记录日期：2026-08-23
- 分支 commit：`9455faef39e03fe398ff253027f4d419d90db636`（HEAD == main，尚未产生代码改动）
- 与 main 基线差异：无（本分支从 main 创建，仅 docs 变更；代码改动将在后续任务中落地）。

## pytest

- 命令：`.venv/Scripts/python.exe -m pytest -q -W error tests/`
- 结果：**1031 passed / 0 failed**（108.13s）

## pyright（project 配置）

- 命令：`.venv/Scripts/python.exe -m pyright -p pyrightconfig.json`
- 结果：37 errors / 4278 warnings

## pyright（tests 配置）

- 命令：`.venv/Scripts/python.exe -m pyright -p pyrightconfig.tests.json`
- 结果：21 errors / 4488 warnings

## 验证

- `git status --short --branch` 干净（`## feature/c0-observability`），分支正确。
- 分支 pytest 全量 1031 passed / 0 failed，与 main 一致。
