# task-6.1 / 6.2 — 文件 sweep 的诚实边界（ADR-1）

日期：2026-10-03　测试：`tests/retention/test_retention_runtime.py` 文件腿三用例

## 实现

- 周期任务保留文件腿：`RetentionSweeper._sweep_files` 调 `core.telemetry.retention.sweep_roots`，
  但 **内置 root 集为空**——root 只来自 `[agent.retention] file_roots`（形如
  `operational = ["..."]`），加载期校验类别键（task 2.2）。
- 未配置 root 时输出一条 `scanned=0` 占位报告（`target="(no file_roots configured)"`），
  演练报告能看出"文件腿跑过且零动作"，而非静默缺席。
- **禁配活跃文件**：`sweeper.py` 模块 docstring 与 `config.example.toml` 注释均写明
  `workspace/logs/*.db`（活跃 SQLite 库）与 `logs/tool_audit.ndjson`（单文件持续追加，
  mtime 永远最新）不得入 root——mtime 判据对它们要么无效要么是数据销毁（ADR-1 备选③拒绝理由）。

## 测试证据

| task / spec Scenario | 用例 | 断言 |
| --- | --- | --- |
| 6.1 未配置 root → 文件腿 scanned=0 | `test_file_roots_empty_reports_zero_scanned` | 恰一条 files 占位报告，scanned=0 deleted=0 |
| spec「分片产物按档过期」 | `test_file_roots_delete_only_overaged_shards` | 日期分片目录（60d 旧片 + 新片）配为 operational root：dry-run 报将删 1 且零变更；实删仅删超龄片，scanned=2/deleted=1/kept=1/bytes>0 与磁盘一致 |
| spec「活动数据库不被当作过期文件删除」+ 6.2 负向 | `test_active_sqlite_file_not_in_roots_untouched` | 400 天前 mtime 的活跃 `.db` 放 logs 目录、该目录**未配置** root → 完整一轮后文件仍在 |
