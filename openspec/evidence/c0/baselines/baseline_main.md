# C0 基线：main（2026-08-23 清理后）

- 记录日期：2026-08-23
- main commit：`9455faef39e03fe398ff253027f4d419d90db636`（docs(scaling) 新增 C0 前置 change，等同分支基线的父级）
- 说明：`feature/c0-observability` 分支自该 main commit 创建，仅含 docs 变更（openspec/ 下），不含代码改动；以下基线即 main 基线。

## pytest

- 命令：`.venv/Scripts/python.exe -m pytest -q -W error tests/`
- 结果：**1031 passed / 0 failed**（108.13s）
- 依据 memory `phase1-pre-existing-test-failures.md`：main 已于 2026-08-23 清理，全量 suite 恢复 0 failed。

## pyright（project 配置）

- 命令：`.venv/Scripts/python.exe -m pyright -p pyrightconfig.json`
- 结果：37 errors / 4278 warnings（error level 下 37 errors / 0 warnings）
- 均为既有错误，非本分支新增。

## pyright（tests 配置）

- 命令：`.venv/Scripts/python.exe -m pyright -p pyrightconfig.tests.json`
- 结果：21 errors / 4488 warnings（error level 下 21 errors / 0 warnings）
- 均为既有错误，非本分支新增。
