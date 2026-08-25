# PITR 恢复与回滚 Runbook（Phase 1B M6 / C1C）

> 演练工具：`python -m scripts.migrate.cli pitr-rehearsal`（scratch DB
> `nexus_pitrtest` 恢复到指定时间点）。
> 本 runbook 记录生产环境的真实恢复/回滚步骤。SLO：RPO≤5min、
> RTO≤30min。

## 1. 恢复前提（已在 staging 验证）

- 目标库开启 WAL 归档（`archive_mode=on`、`archive_command` 上传至对象存储/异地盘）。
- 保留足够 WAL（`wal_keep_size` / 备份 base 的 `pg_basebackup`），覆盖 RPO 窗口。
- 周期性 `pg_basebackup`（如每 6h）+ 对应 WAL 段，作为恢复的 base。

## 2. 恢复到指定时间点（PITR）

1. 用 `pg_basebackup` 还原 base 到目标目录（或克隆实例）。
2. 配置 `recovery.signal` 与 `restore_command`（从归档取 WAL）。
3. 在 `postgresql.conf` 写 `recovery_target_time = '<恢复点 ISO>'`。
4. 启动实例进入 recovery，达到目标时间后自动停在目标点（`recovery_target_action=promote`）。
5. 验证数据：`verify` 全维度（行数/hash/引用/语义抽样）+ 业务侧冒烟。
6. 升级对外连接串指向恢复实例，完成切换。

演练（离线、scratch DB）验证口径：T0 基线快照 → T0 后写入 marker →
恢复后 marker 缺失、基线各表行数一致 → 证明数据与指定时间点一致。

## 3. 回滚步骤

- S0/S1 内：保持 SQLite primary，弃用 PG staging 副本（`rollback` 命令），
  无数据丢失。
- S2 起：无 PG→SQLite 反向同步，回滚指应用回退版本后**仍指向 PG**；
  先做 PITR 恢复到上一个稳定点，再回退应用版本（forward-fix 优先，
  破坏性 schema 变更按 expand/contract）。

## 4. SLO 依据

- RPO≤5min：WAL 归档周期 + 备份周期内可恢复的最坏数据窗口。
- RTO≤30min：base 还原 + WAL 回放 + 校验冒烟的时间预算
  （离线演练实测记录在 pitr 报告 `elapsed_sec`）。
