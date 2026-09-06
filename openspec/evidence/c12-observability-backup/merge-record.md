# C12 合并与实际验证记录

- 日期：2026-09-06
- 分支管理：源分支 `feature/c12-observability-backup`（基于 `main`@`632d3006`）→ **先 rebase 到 `main`@`49f088dc`（含 C2，rebase 8/8 无冲突）** → 快进合入 `main`（`git merge --ff-only`）；合并后 `git push origin main` + `git push origin feature/c12-observability-backup`
- 合并提交：`83d54bc8`（= 合并时分支 HEAD，fast-forward 后即 `main` HEAD）
- 关联工单：openspec change `c12-observability-backup`（**保持 active 不归档**：贯穿型 change，§8 伴随落地条目 6 项与 P0/P3 出口未完成，任务计划 task-12 置 `in_progress`；本仓库无外部 Issue 系统，沿用 C1/C2 先例以 openspec change 为工单）

## 实际验证（由实际执行人填写）

- **执行人**：ZCode（AI 代理），受开发者委托执行并如实记录（开发者 2026-09-06 授权"实际验证项你自己看着填"）
- **验证内容与结果**（全部为 2026-09-06 实际执行，原始输出见本目录各 evidence 文件）：
  1. C12 契约测试 58 项（redaction/content gate/label 白名单/事件 schema 契约/retention/manifest 校验/SLO 静态负向）：**58 passed**（`tests/observability_privacy/` + `tests/backup_manifest/`，`pytest-contract.txt`）
  2. 合并前全量回归 `pytest -q -W error tests/`（rebase 到含 C2 的基线，PG 17.5 @ localhost:5433 在线、零 skip）：**1277 passed / 0 failed**（1169 基线 + C2 50 + C12 58，`pytest-regression-post-rebase.txt`）
  3. pyright 全仓两配置：project **36 errors** / tests **31 errors**，逐条核对均为既有基线（与 2026-09-05 基线记录一致），C12 新增文件 **0 错误**（scoped `pyright-project.txt`/`pyright-tests.txt`，全仓 `pyright-project-full.txt`/`pyright-tests-full.txt`）
  4. `openspec validate c12-observability-backup`：**通过**（`openspec-validate.txt`）
  5. 合并后验证：主工作树（main HEAD）复跑 C12 58 项 + C2 50 项 = **108 passed**（2026-09-06）
- **未验证/超范围**：总控台聚合 API、config.toml 接线与进程内定时 retention、基线报告、恢复演练为伴随落地条目（tasks.md §8），本记录不宣称；生产环境（ECS Docker）部署不在本记录范围。

## 回滚方式

- 全部为新增独立模块/fixture/测试，无 DB 变更、无既有路径修改：`git revert 632d3006..83d54bc8`（或整体 revert merge 范围）即可移除；后续 Cxx 若已基于契约实现，按 design.md §Rollback 保留模块只回滚行为变更。

## 集成推送补记（2026-09-06）

- 首次推送 main 被拒：远端已由 GitHub PR #1 合入 C3（`49f088dc..e2408e5b`）。按不重写已推送历史原则，以 merge commit `d6cc2619` 将 origin/main 并入本地 main（无冲突；feature/c12-observability-backup 已推送、hash 引用保持有效）。
- 集成后全量回归（C2+C3+C12，PG 在线）：**1312 passed / 1 failed**；唯一失败 `tests/test_chat_api.py::test_index_returns_status_json_without_bundle` 为已知环境性失败（2026-09-05 基线在案）：主工作树 `static/` 根下存在历史构建产物 `index.html`/`index-*.js`（gitignore，c12 worktree 无此产物，同一代码 5/5 通过），与 C12/C3 改动无关（`pytest-regression-integrated.txt`）。
- 推送：`git push origin main`（`49f088dc..`集成 tip）+ `git push origin feature/c12-observability-backup`（新分支）。
