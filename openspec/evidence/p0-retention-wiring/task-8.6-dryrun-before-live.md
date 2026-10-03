# task-8.6 — 上线前运维步骤（交付物）：先演练后实删 + 一键回退

日期：2026-10-03

## Runbook（部署环境操作序列）

```bash
# ① 演练（零变更）：确认"将删除"量符合预期，报告存证进本目录
python scripts/retention_run_once.py --config config.toml \
    | tee openspec/evidence/p0-retention-wiring/dryrun-<日期>.json

# ② 人工核对报告：逐实体 recoverable_deleted / irrecoverable_deleted 是否符合预期
#    （首次启用会清掉窗口外历史重放帧与审计行——当前 Pilot 生产数据量极小）

# ③ 确认无误后放开实删（仍可先在低峰手工跑一轮实删观察）：
python scripts/retention_run_once.py --config config.toml --live

# ④ 一键回退（运行期）：config.toml [agent.retention] enabled = false 并重启进程
#    → 周期任务不装配，零删除；已删数据不可恢复（审计流 irrecoverable），
#    补发缓冲可由客户端 REST 重建（canonical 真源完好）
```

注意：`backend != postgres` 时脚本退出码 2（retention 只接线 PG durable 路径）；
`interval_s = 0` 时进程内周期循环不启动，但演练/实删脚本入口不受影响。

## 本地预演证据

`task-8.6-dryrun-local.txt`：本地 5433 `nexus` 库（迁移已至 `d0a9b7c3e1f5`）实跑
`run_once(dry_run=True)`——六实体报告齐全（session_replay / tool_audit_events /
admin_audit_events / work_attempts / credentials / files 占位）、零删除、零错误；
credentials scanned=2（迁移闭环测试留下的 2 条凭据行，均在窗口内 → 未清除，符合预期）。

**部署环境（阿里云 ECS 生产容器）的 dry-run 存证在部署本 change 时补录**
（`dryrun-<日期>.json`）——该步骤属部署 runbook，不阻塞代码合入。
