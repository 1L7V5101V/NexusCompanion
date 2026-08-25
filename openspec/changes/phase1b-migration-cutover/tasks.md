# Phase 1B 迁移工具与主数据源切换 — 实施任务

> 执行环境：独立 branch/worktree（如 `feature/phase1b-migration`），不在主工作树直接实现。
> 每任务含验证方式；管理闭环：任务 checkbox → `openspec status` → evidence → SCALING_ROADMAP/CHECKLIST 仅在有证据时更新。

## 1. 基线与环境

- [x] 1.1 创建独立 worktree/branch（`feature/phase1b-migration`），记录 main 与分支的 pytest/pyright 基线到 `openspec/evidence/phase1b/baselines/`。验证：`git status --short --branch` 干净、分支正确；基线文件存在且含 main 全量 1031 passed / 0 failed（2026-08-23 清理后）记录
- [x] 1.2 准备 staging PG（复用 `docker/debug/docker-compose.yml`）+ alembic `upgrade head` + 样例 SQLite 数据（覆盖多 tenant/多通道）。验证：PG 可连、schema 就绪、`import_to_pg.py` 在基线可用（校验工具为本次新建目标，见 §3）
  - 证据：docker/debug PG 17.11 :5433 可连；`nexus` 库 alembic head `b6e9d2c4a8f1`（vector/pg_trgm 扩展已装）；样例数据 `openspec/evidence/phase1b/sample/`（60 会话×5 通道、10k 消息、2k memory_items 等，含多 tenant 映射）。本次导入工具为 `scripts/migrate/` 新建目标。

## 2. M5 批量导入（C1B）

- [x] 2.1 实现批量写入后端（独立 COPY 连接；仅 `memory_items` 需导入前按 tenant 幂等 provisioning，复用 `partition_name_for_tenant` 与 advisory-lock 双检；`sessions`/`messages`/`memory_replacements` 为普通表直接 COPY）。验证：`pytest tests/migration/` 覆盖 COPY 路由到已建 `memory_items` 分区与非分区表；1 万行样例导入行数与源一致且耗时显著低于逐行基线（记录数字到 evidence）
  - 证据：`scripts/migrate/bulk.py`（COPY 文本格式 + temp staging + `INSERT...SELECT...ON CONFLICT DO NOTHING` + `pg_advisory_lock` 幂等 provisioning + `_partition_covers` 双检）；`tests/migration/test_bulk_import.py` 覆盖分区/普通表路由与幂等 provisioning。基准 `openspec/evidence/phase1b/results/bench-20260825T011722Z-bench.json`：12,567 行 COPY 10.235s vs 逐行 INSERT 17.703s = **1.73× 加速**，逐表行数与源一致。
- [x] 2.2 实现 checkpoint 断点续传（每表主键高水位、JSON 原子写 `<run-id>.json`）。验证：构造「中途中断 → 续跑」用例，最终逐表行数与源一致；checkpoint 文件在中断后正确记录边界
  - 证据：`scripts/migrate/checkpoint.py`（temp+`os.replace` 原子写）；`tests/migration/test_checkpoint_resume.py`（monkeypatch copy_table 中断 → 续跑对齐全部 12 表；提交先于 checkpoint 的高水位不变量）。
- [x] 2.3 实现幂等重跑（保留源主键 + `ON CONFLICT DO NOTHING`）。验证：对同一源重跑导入，行数不变、无重复；测试覆盖
  - 证据：`tests/migration/test_idempotent.py`（新 run_id 重跑 → 0 新增、全 conflict-skip、PG 计数不变、消息 id 集合相等）。
- [x] 2.4 实现显式 tenant mapping 强制（mapping JSON；dry-run 列出未映射源；缺 mapping 拒绝写入）。验证：无 mapping 时工具预检终止且 PG 零写入；mapping 生效后各 tenant 数据跨 tenant 不可见（复用 `test_tenant_isolation` 模式）
  - 证据：`scripts/migrate/mapping.py`（`require_covered`）+ `importer._preflight`；`tests/migration/test_mapping.py`（缺 mapping 拒绝、未映射源拒绝、tenant 隔离、dry-run 零写入）。
- [x] 2.5 覆盖全部目标表（`memory_items`/`sessions`/`messages`/`memory_replacements`/纳入本阶段的插件数据），按 FK 顺序导入（sessions → messages → memory_items → memory_replacements）。验证：逐表行数与 SQLite 源一致
  - 证据：`scripts/migrate/importer.py` TABLE_SPECS 12 表 FK 顺序；`tests/migration/test_table_coverage.py`（逐表 parity + 0 孤儿外键）。全量 `phase1b-import-v1` 导入：12,567/12,567，10.11s。
- [x] 2.6 import 机器可读结果入库（`openspec/evidence/phase1b/results/`）。验证：结果文件存在、字段完整、可复现（重跑后结果一致）
  - 证据：`openspec/evidence/phase1b/results/phase1b-import-v1-import.json`（meta/plan/tables/ok/elapsed_sec 完整）；`tests/migration/test_results_evidence.py`（字段完整性 + 两次干净导入逐表 inserted 一致）。

## 3. M5 机器可读校验（C1B）

- [x] 3.1 实现逐表行数 + 关键字段 hash 校验（按主键排序后对规范字段做 hash）。验证：构造行数/字段差异样例 → 报告指出差异表与维度、退出码非零
  - 证据：`scripts/migrate/verify.py`（`_row_count_dim` + `_field_hash_dim`；SHA-256 流式 hash，规范字段排除被注入的 created_at/updated_at 与 embedding，时间戳统一 ISO 规范化使源/目标逐值可比）；`tests/migration/test_verify.py::test_row_count_diff_detected`/`test_field_hash_diff_detected`（删行/篡改 content → 对应维度报错、exit_code=1）。
- [x] 3.2 实现引用完整性校验（`messages.session_key` 命中 sessions、`memory_replacements` 外键命中）。验证：孤儿引用被报告为失败
  - 证据：`verify.FK_CHECKS` 8 条同 tenant 外键检查（session_key→sessions.key、old/new_item_id→memory_items.id、tick_step_log.tick_id→tick_log.tick_id）；`test_orphan_reference_detected`（删 session → `messages.session_key` 孤儿被报）。
- [x] 3.3 实现语义抽样校验（同一查询下源/目标向量 top-k 命中与排序、关键词搜索命中集合、session `next_seq`）。验证：抽样查询源/目标结果一致；允许差异有记录
  - 证据：`verify._semantic_dim` 镜像 memory2/store 语义——L2 归一化 KNN（≡cosine，PG `<=>`）、summary OR-LIKE 命中集合（含不存在词项空集对照）、sessions.next_seq 全量比对；`test_semantic_next_seq_diff_detected`/`test_vector_topk_diff_detected`。
- [x] 3.4 校验报告 JSON 输出 + evidence 入库（`openspec/evidence/phase1b/results/`）。验证：报告文件存在、各维度字段完整、退出码语义正确
  - 证据：`openspec/evidence/phase1b/results/phase1b-verify-v1-verify.json`（meta 含 hash_columns/fk_checks、四维 `ok`、整体 `ok`、`exit_code=0`、`evidence_path`）；`test_verify_passes_on_aligned` 覆盖字段完整性与对齐通过。

## 4. M6 S0-S4 主数据源切换（C1C）

- [x] 4.1 实现状态持久化（JSON 状态文件 + 原子写 + `status` 命令）。验证：状态推进可观察、中断后从持久化状态恢复、不重复已完成步骤
  - 证据：`scripts/migrate/state.py`（`CutoverState` 四元结构 + `save_state` 用 tempfile.mkstemp + `os.replace` 原子写 + `load_state` 对损坏文件抛 `StateError` + `transition` 过渡表 S0→S1→S2→S3→S4）+ CLI `status` 子命令；`tests/migration/test_state.py::test_status_initial_s0`/`test_state_atomic_save_and_reload`/`test_state_transition_progression`/`test_import_enters_s1_and_persists`。
- [x] 4.2 实现 S1 进入/退出条件 gate（import + verify 证据齐备才允许推进 S2）。验证：校验未通过时 `promote` 被拒并给出缺失证据
  - 证据：`state.promote_gate`（要求 current==S1、S1 enter_evidence 存在、S1 exit_evidence 存在且 verify 报告 `ok`）+ `record_verify_evidence`（非全绿不记录退出条件证据）；`test_promote_rejected_without_verify`/`test_promote_rejected_when_verify_failed`（缺失证据项列在 `StateError.missing` 并含「校验」）。
- [x] 4.3 实现 S1→S2 promote（config 切 `backend="postgres"` + staging 全链路 smoke + 对账报告）。验证：staging 全链路可用、新写入以 PG 为准、对账报告入库
  - 证据：`state.run_promote`（gate → `_write_promote_config` 产出 promote-config.toml 切 `[storage] backend="postgres"` + `postgresql+psycopg://` URL → `_run_smoke` staging 全链路 → dual-store 声明 → 报告 → S2，报告 `state` 落盘）；`test_promote_passes_after_verify`/`test_promote_config_flip_and_pg_writes`（config 翻转、PG `sessions.next_seq=2`/2 消息/1 memory_item、SQLite 源零写入）。
- [x] 4.4 实现 turn control dual-store（PG-primary 下 SessionManager 持 SQLite turns store，`control_store` 不再抛 RuntimeError）。验证：PG-primary smoke 下 agent turn 关键路径可跑、turn 记录落 SQLite 审计副本；切换记录声明该边界与恢复/回滚策略
  - 证据：`session/manager.py`（runtime 分支 `self._control_store = SessionStore(workspace / "turn_audit.db")`、`control_store` property、`close()` 关自有审计副本）；promote 报告 `dual_store` 字段（`primary="postgres"`/`turn_control="sqlite-audit-copy"`/审计窗口 S2/S3 声明/回滚策略含 S1）；`test_promote_turn_lands_in_sqlite_audit_copy`（turn 可回读自 `turn_audit.db`）/`test_promote_dual_store_declaration`。
- [x] 4.5 实现 S2→S3 audit（完成至少一个业务周期对账、停止 shadow、SQLite 归档只读）。验证：对账报告入库、shadow 停止后 SQLite 只读、S3 证据文件存在
  - 证据：`state.run_audit`（写一轮 cycle sessions/turns/memory → `cycle_reconciliation` 逐项对账 next_seq/消息数/turn 回读 → 写 `sqlite_archive.json` 归档只读标记 → S3，报告 `state` 落盘）；`test_audit_to_s3`/`test_audit_requires_s2`。
- [x] 4.6 实现回滚（S1 内回 SQLite primary；S2 起禁止 rollback-to-SQLite）。验证：S1 回滚演练数据完整；S2 后调用回切命令被拒并提示正确口径
  - 证据：`state.run_rollback`（S1→S0 弹出 S1 阶段可重 import；S0 幂等 no-op；S2 起抛 `StateError`「不可无损回切」）；`test_rollback_s1_to_s0`（回滚后重 import 重新进入 S1）/`test_rollback_identity_at_s0`/`test_rollback_forbidden_after_s2`。
- [x] 4.7 PITR 恢复演练 + runbook。验证：恢复数据与指定时间点一致；runbook 记录恢复/回滚步骤与 RPO≤5min/RTO≤30min
  - 证据：`state.run_pitr_rehearsal`（重建 scratch `nexus_pitrtest` → alembic head → `provision_partitions` → 记 T0 基线 → `_copy_table` 12 表 FK 序（分区表走 `COPY (SELECT)` + temp staging INSERT 路由）→ 写 `pitr_marker` 于 T0 后 → 恢复校验 marker 缺失 + 基线行数一致 → 清理 + `DROP DATABASE` 兜底 try/finally → 产出 `runbook-pitr.md` 含 `pg_basebackup`/`recovery_target_time`/RPO≤5min/RTO≤30min）；`test_pitr_rehearsal_restores_point_in_time`/`test_pitr_requires_import`。

## 5. 收口与证据

- [x] 5.1 全量验证：pyright（project + tests 两配置）与 pytest 相对 main 基线无回归。验证：命令结果 + 与 `openspec/evidence/phase1b/baselines/` 记录的 main 基线 diff，无本分支新增失败
  - 证据：`openspec/evidence/phase1b/baselines/baseline_feature_phase1b_migration.md`（最终验证节）。pytest 全量 **1096 passed / 0 skipped / 0 failed**（`-W error`，144.57s）：main 在 PG 可用时为 1060 passed / 0 failed，新增 36 个 `tests/migration/*` 用例，无收集错误、无失败。pyright project 配置 **38 errors / 4290 warnings** 与 main 完全一致；pyright tests 配置 **21 errors**（与 main 一致，warnings +403 来自新增 `tests/migration/*`，无新增 error）。修复了 alembic env.py `fileConfig` 在 pytest 进程内 disable 现有 logger 导致 caplog 断言的 5 用例回归：`scripts/migrate/alembic_util.py`（`upgrade_head` 升级前后快照/恢复日志配置）供 `tests/migration/conftest.py::mig_pg_url` 与 `state._upgrade_scratch` 复用。
- [ ] 5.2 更新 change 状态与跟踪文档：tasks 全勾选 → `openspec validate` 通过 → `openspec status` 显示 apply 完成 → sync-specs 把 `storage-migration` 增量合入主 spec → archive change。验证：`openspec validate` 通过、change archived
- [ ] 5.3 更新 `SCALING_ROADMAP.md` 与 `PROJECT_CHECKLIST.md`：仅在 evidence 齐全（merge commit + 可复现测试/基准）时把 C1B/C1C 标 `verified`，Phase 1B 移入已完成节。验证：两文档状态与 evidence 一致、`verified` 满足 §8 规则
