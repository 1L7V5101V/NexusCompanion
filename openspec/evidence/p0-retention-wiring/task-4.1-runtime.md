# task-4.1 / 4.2 — 周期壳与 app 装配（ADR-3）

日期：2026-10-03

## 变更

| 文件 | 内容 |
| --- | --- |
| `bootstrap/retention/__init__.py` | **新** 包出口 |
| `bootstrap/retention/sweeper.py` | **新** `RetentionSweeper.run_once(dry_run=)`：单轮编排（replay 窗口 → 三条审计流 → 凭据摘要 → 文件腿），逐实体异常隔离（错误串过 `redact_text` 记入该实体报告，不中断整轮）；`EntityReport` 字段集 = `SweepReport` + `recoverability`；`RetentionRunReport` 含可恢复/不可恢复分类合计（ADR-7）。模块 docstring 记录不可删除集与"logs/*.db、tool_audit.ndjson 严禁入 root"原因（task 6.2 注释义务） |
| `bootstrap/retention/runtime.py` | **新** `RetentionRuntime`：tick 循环（**先睡后跑**——启动不执行首轮删除）、单轮异常兜底、`run_once(dry_run=True)` 演练入口、`start()` 建 `asyncio.create_task(name="retention_sweep")` + done callback（意外退出大声记录）、`stop()`；形状对齐 C6 `AttachmentLifecycleRuntime` |
| `bootstrap/app.py` | `_start_retention_runtime`（`webchat_durable` 存在 ∧ `retention.enabled` 时装配；`interval_s=0` 只建 runtime 不起循环）+ `_stop_retention_runtime`；启动接线在 attachment lifecycle 之后；shutdown 步骤序列加 `("retention.stop", ...)`（紧跟 `attachments_lifecycle.stop`） |
| `scripts/retention_run_once.py` | **新** 运维单轮入口（默认 dry-run，`--live` 实删；非 PG 后端退出码 2），task 8.6 演练用 |

## 测试证据（tests/retention/test_retention_runtime.py，9 passed）

- 启停生命周期：task 名 `retention_sweep`、重复 start 幂等、stop 干净收束；
- **启动不跑首轮**：start 后 0.3s 无任何删除发生（spec「启动不立即执行删除」）；
- 周期到点执行 + 单轮异常隔离：第一轮炸、第二轮照常触发、task 存活；
- `run_once(dry_run=True)` 演练入口零变更；
- sweeper 单实体异常隔离：一个实体失败记入报告、其余实体继续、`report.ok=False`；
- app 装配门禁（4.2）：shutdown 步骤含 `retention.stop`（源码 grep 守卫）、
  enabled+interval>0 起 task、interval_s=0 不起 task、stop 被调用；
- 文件腿见 `task-6.1-file-roots.md`。
