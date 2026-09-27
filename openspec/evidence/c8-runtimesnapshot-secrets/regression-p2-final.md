# C8 P2 收口回归记录（2026-09-27）

## 全量 pytest（PG 5433 已启动）

```
.venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/pilot_loadtest_bench.py -p no:cacheprovider
→ 1373 passed in 287.81s (0:04:47)
```

- 0 failed / 0 skipped（本分支全部 1373 项，含 PG 依赖集成测试全部通过）。
- 对照 main 基线（1312 passed + 1 既有 chat_api 环境性失败）：无回归，且本次
  chat_api 既有环境性失败未出现（环境依赖项，见 main-verification 记录）。
- C8 新增 60 项（tests/c8/）全部通过。

## pyright

```
.venv/Scripts/pyright
→ 36 errors, 4868 warnings, 0 informations
```

- 与 main 既有 36 错误基线一致（未新增类型错误）。

## P2 段测试矩阵（task-08 验收标准 → 证据）

| 验收标准 | 测试 | 结果 |
| --- | --- | --- |
| gate/interceptor fail-closed；fanout 有界 timeout 不改终态 | tests/c8/test_hook_failure_policy.py（12 项） | pass |
| contribution_id 稳定 + 固定类型 + binding_policy + plan 外不可见 | tests/c8/test_tenant_runtime_plan.py（8 项） | pass |
| tenant secret 静态加密；不进日志；rotation/撤销边界 | tests/c8/test_secret_box.py（10 项） | pass |
| compile/publish 失败保留旧 committed snapshot | tests/c8/test_snapshot_publish_fallback.py（4 项） | pass |
| 安装只登记不执行代码 | tests/c8/test_dormant_install.py（2 项） | pass |

## P0 段测试矩阵（详见 lease-coverage-audit.md）

| 验收标准 | 测试 | 结果 |
| --- | --- | --- |
| 全入口 lease 绑定（每类入口一条测试） | tests/c8/test_lease_coverage.py | pass |
| 进行中 work 不切 snapshot | tests/c8/test_lease_coverage.py | pass |
| 旧 snapshot 无法绕过 revocation | tests/c8/test_revocation_gate.py（含 test_old_snapshot_cannot_bypass_revocation） | pass |

## 修复记录

- 执行中修正：post_tool_error hook 失败原会掩盖工具错误终态 → 改为 fail_open
  仅记录（test_hook_failure_policy::test_post_error_hook_timeout_does_not_mask_tool_error）。
- 环境记录：便携 PG（C:\Users\HP\.localpg）以 `-o "-p 5433"` 启动后 166 项
  PG 依赖测试从 skip 转为通过。
