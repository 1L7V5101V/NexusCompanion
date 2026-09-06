# C2 合并与实际验证记录

- 日期：2026-09-06
- 分支管理：源分支 `feature/c2-durable-control-plane`（基于 `main`@`632d3006`）→ 快进合入 `main`（`git merge --ff-only`，无分叉、无 rebase）；合并后 `git push origin main`（`df647e38..7ed6897d`）
- 合并提交：`7ed6897d`（= 推送时 `origin/main` HEAD，已核对本地与远端一致）
- 关联工单：openspec change `c2-durable-control-plane`（已归档 `changes/archive/2026-09-06-c2-durable-control-plane/`；本仓库无外部 Issue 系统，沿用 C1 先例以 openspec change 为工单）

## 实际验证（由实际执行人填写）

- **执行人**：ZCode（AI 代理），受开发者委托执行并如实记录（开发者 2026-09-06 授权"实际验证项你自己看着填"）
- **验证内容与结果**（全部为 2026-09-06 实际执行，原始输出见本目录各 evidence 文件）：
  1. C2 测试套件 50 项（PG 集成 + 静态契约）：**50 passed**（`tests/control_plane/` + `tests/test_control_plane_contract.py`）
  2. 全量回归 `pytest -q -W error tests/`（基线环境 = 主仓 .venv，PG 17.5 @ localhost:5433）：**1219 passed / 0 failed / 0 error**（`pytest-regression.txt`）
  3. pyright project + tests 两配置：C2 新增文件 **0 错误**；全仓错误数与基线一致（新 venv 的 +1 为既有文件依赖漂移，见 `pytest-regression.txt` 环境性差异说明）
  4. 回滚演练 Create→Verify→Enable→rollback：**13/13 PASS**（`rollback-drill-output.txt`）
  5. 合并后验证：主工作树复跑 C2 测试 50 项通过（2026-09-06，`7ed6897d`）
- **未验证/超范围**：生产环境（ECS Docker）部署未在本记录范围；delivery worker 与真实 channel adapter 的接线归 C4，本 change 未验证端到端真实外发。

## 回滚方式

- migration 层：`alembic downgrade c4d8f2a6e9b3`（七表删除，未 cutover 无读取者；见 `tests/control_plane/test_migration.py::test_downgrade_upgrade_cycle`）
- 运行层：关闭 Pilot 入口 + 停 worker，保留 PG 数据（`rollback-drill.md`）
