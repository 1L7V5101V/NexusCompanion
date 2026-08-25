# Phase 1B 基线：main（2026-08-25 实测）

- 记录日期：2026-08-25
- main commit：`998395e0dba9d9dedd2ef60d80dbdc019befe776`（docs(scaling) C0 标记 verified，含 C0 可观测性 merge `a777ded2`）
- 说明：`feature/phase1b-migration` 分支自该 main commit 创建；记录时刻分支 == main（尚无代码改动），以下基线即 main 基线。
- 历史参考：main 全量 1031 passed / 0 failed（2026-08-23 测试债清理后，见 memory `phase1-pre-existing-test-failures.md`）；C0 合入后总量扩至 1060。

## pytest

- 命令：`.venv/Scripts/python.exe -m pytest -q -W error tests/`
- 结果：**998 passed / 62 skipped / 0 failed**（408.45s，exit 0）
- 62 skipped 均为 PG-gated 集成测试（`NEXUS_TEST_PG_URL=localhost:5433` 无 PG 运行，按现有模式 skip）；总量 1060 = 998 passed + 62 skipped，与 C0 全绿记录（1060 passed，PG 可用时）一致。
- 回归判据（任务 5.1）：本分支最终 pytest **0 failed**，且 `passed + skipped == 1060` 无收集错误。

## pyright（project 配置）

- 命令：`.venv/Scripts/python.exe -m pyright -p pyrightconfig.json`
- 结果：38 errors / 4290 warnings（error level 下 38 errors / 0 warnings）
- 均为既有错误（含 C0 新增 `core/telemetry` 的 1 项），非本分支新增。

## pyright（tests 配置）

- 命令：`.venv/Scripts/python.exe -m pyright -p pyrightconfig.tests.json`
- 结果：21 errors / 4543 warnings（error level 下 21 errors / 0 warnings）
- 均为既有错误，非本分支新增。

## 验证

- `git status --short --branch` 干净（`## feature/phase1b-migration`），分支正确。
- 分支 pytest / pyright 结果与 main 一致（记录时刻无代码改动）。
