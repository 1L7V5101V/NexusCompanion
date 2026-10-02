# C12 §8.4 retention 接线 — 实施任务

> 对应 `openspec/changes/p0-retention-wiring/`；design.md ADR-1..ADR-7 为实现约束，偏离需先改 design。
> 分支：`feature/p0-retention-wiring`（建议 worktree `D:/1/wt-retention`，基于 `main` tip）。
> 证据统一落 `openspec/evidence/p0-retention-wiring/`（每条任务给出命令与结果路径）。
> 环境事实：本地 PG 5432（`nexus` 库，开发用）与 5433（测试实例）；集成测试经
> `NEXUS_TEST_PG_URL` + `NEXUS_REQUIRE_PG=1` 走独立 scratch 库；alembic 用
> `postgresql+psycopg://` 方言（`DATABASE_URL=... alembic upgrade head`）。
> 上游义务：C12 伴随落地协议 `openspec/changes/c12-observability-backup/tasks.md` §8.4
> （本 change 承接其 **retention 半边**；总控台聚合 API 半边不在本 change）。

## 1. 迁移与模型（ADR-4）

- [ ] 1.1 新增 alembic revision（expand-only）：`auth_sessions.session_digest` 与
  `access_tokens.token_digest` 放开 NOT NULL；`ck_access_tokens_digest_sha256` 放宽为
  `token_digest IS NULL OR char_length(token_digest)=64`；两张表的 UNIQUE 保持不动。
  `down_revision` 取当前 head（实现前先 `alembic heads` 核对，勿沿用本文写死的值）。
  验证：`DATABASE_URL=postgresql+psycopg://nexus:nexus_dev@localhost:5433/nexus alembic upgrade head`
  → `downgrade -1` → `upgrade head` 闭环通过；`\d auth_sessions`/`\d access_tokens` 显示约束如预期。
  证据：`task-1.1-migration.md`
- [ ] 1.2 模型同步 `bootstrap/db/models/auth.py`：两列改 `Mapped[str | None]`，`__init__.py` 无需变更。
  验证：`alembic check`（若可用）或 ORM metadata 与库结构比对脚本无差异；`pyright --level error` 0 errors。
  证据：并入 `task-1.1-migration.md`
- [ ] 1.3 固化 PostgreSQL 对 UNIQUE 中 NULL 的默认行为（NULLS DISTINCT）：真 PG 用例插入两条
  `session_digest IS NULL` 的行应共存。若部署版本行为不同，**停下**回到 ADR-4 改 partial unique index
  并更新 design，不得擅自换方案。验证：`tests/retention/test_credential_purge.py` 用例行 + 断言名。
  证据：`task-1.3-nulls-distinct.md`

## 2. 配置节与加载校验（proposal - What Changes）

- [ ] 2.1 `agent/config_models.py` 新增 `RetentionConfig`：`enabled=True`、`interval_s=86400`、
  `operational_days=30`、`audit_days=180`、`debug_content_days=7`、`batch_size=500`、
  `max_batches=20`、`purge_grace_s=2592000`；挂到 `Config.retention` 并入 `__all__`。
  三档天数默认值从 `core.telemetry.retention` 常量引用（单一来源，不重复写数字）。
  验证：`tests/retention/test_config.py::test_retention_config_frozen_defaults`（逐项断言 + 与
  `RetentionPolicy()` 默认一致）。证据：`task-2.1-config.md`
- [ ] 2.2 `agent/config.py` 新增 `_load_retention_config(data)` 读 `[agent.retention]`，沿用
  `_load_attachment_config` 的加载期校验（正数强制；`interval_s` 允许 0 = 不启用周期任务），
  在 `load_config` 中传入。验证：非法值（0/负数/非数字）在加载期抛 `ValueError` 且错误串含配置项名
  的用例组。证据：并入 `task-2.1-config.md`
- [ ] 2.3 `config.example.toml` 补 `[agent.retention]` 注释块（默认值 + 一句"默认开启即会删除超窗
  数据，上线前先跑 dry-run"）。验证：`tomllib` 解析该文件成功且 `load_config` 用例通过。
  证据：并入 `task-2.1-config.md`

## 3. PG 行级 sweeper（ADR-2/ADR-5）

- [ ] 3.1 仓储侧新增按时间分批删除方法（每类实体一个，返回删除行数；时间比较用 DB 时钟
  `func.now()`，`ORDER BY <ts> ASC LIMIT :batch`）：`webchat_replay_frames`（复用
  `WebchatReplayRepository.delete_frames_before` 的游标语义，按 `created_at` 计算每会话的
  `before_seq`）、`tool_audit_events`、`admin_audit_events`、`work_attempts`。
  验证：`tests/retention/test_pg_sweeps.py` 逐项断言"窗口内保留 / 窗口外删除 / 计数如实 /
  第二轮为 0"。证据：`task-3.1-pg-sweeps.md`
- [ ] 3.2 不可删除集写成显式守卫：`outbound_delivery_intents`（含 `dead_letter`）、
  `canonical_messages`/`canonical_conversations`/`inbox_records`/`turns`、`attachments`
  不进入任何删除清单。验证：负向用例——构造远超任何窗口的死信投递意图与业务消息行，跑完整一轮后
  断言行数不变（spec「死信投递意图不被删除」「业务消息不在保留期执行范围内」）。
  证据：`task-3.2-no-delete-set.md`
- [ ] 3.3 重放帧删除不回退 seq 水位：删除最旧帧后 `current_seq()`/`oldest_seq()` 与计数器行为符合
  design ADR-2（水位只增不减；读侧据水位差判"需客户端重建"）。验证：PG 用例 + 复用
  `tests/auth_provisioning/test_webchat_rebuild_reconcile.py` 的 replay_required 语义作对照。
  证据：并入 `task-3.1-pg-sweeps.md`
- [ ] 3.4 单轮上限与分批收敛：`batch_size`/`max_batches` 生效，超量数据单轮不越界、剩余留待下轮。
  验证：造 `batch_size*3 + 余数` 条过期行，断言首轮删除 ≤ `batch_size*max_batches`、报告计数与实际
  一致、后续轮次继续收敛。证据：并入 `task-3.1-pg-sweeps.md`

## 4. 周期壳与装配（ADR-3）

- [ ] 4.1 新增 `bootstrap/retention/`：`RetentionRuntime`（tick 循环、单实体异常隔离、
  `CancelledError` 正常退出、`run_once(dry_run=...)` 演练入口、`stop()`），形状对齐
  `bootstrap/attachments/runtime.py`；报告形态对齐 `core.telemetry.retention.SweepReport`
  （`to_dict`/`dry_run`/`errors`）。验证：`tests/retention/test_runtime.py`（启停、异常不阻断下一轮、
  **启动不跑首轮删除**）。证据：`task-4.1-runtime.md`
- [ ] 4.2 `bootstrap/app.py` 接线：`config.retention.enabled` 为真时装配 runtime 并 `create_task(...,
  name="retention_sweep")` + done callback；停机函数加入现有 shutdown 步骤序列（与
  `_stop_attachment_lifecycle` 同处注册）。验证：装配用例断言 enabled=false 时不建 task、
  enabled=true 时 task 存在且停机被调用（`grep` 检查 shutdown 步骤含 retention）。
  证据：`task-4.2-app-wiring.md`

## 5. 凭据过期处置（ADR-4）

- [ ] 5.1 清除路径实现：仅对 `revoked_at IS NOT NULL` ∧ `expires_at < now`（token 的 `expires_at`
  为 NULL 视为不过期、不清除）∧ 超过 `purge_grace_s` 的行，UPDATE digest 为 NULL，**不删行**，
  保留归属与时间 metadata。验证：`tests/retention/test_credential_purge.py` 覆盖
  spec「过期凭据只抹除摘要并保留审计可核验的元数据」三个 Scenario（已撤销+过期+超宽限清除；
  活跃不触碰；已撤销未过期不清除）。证据：`task-5.1-credential-purge.md`
- [ ] 5.2 fail-closed 校验：清除后按原摘要调用 `validate_session`/token 校验路径必然失败（401 语义），
  不得因 NULL 摘要误命中；`count_active_admin_sessions` 等既有查询不受影响。验证：负向用例逐条断言。
  证据：并入 `task-5.1-credential-purge.md`
- [ ] 5.3 幂等与批次：已清除行不重复计入删除数；清除也走 `batch_size`/`max_batches`。
  验证：第二轮 cleared=0 的用例。证据：并入 `task-5.1-credential-purge.md`

## 6. 文件 sweep 的边界（ADR-1）

- [ ] 6.1 runtime 保留文件腿调用 `sweep_roots`，但**内置 root 集为空**，root 由 config 显式给出
  （`[agent.retention] file_roots`，形如 `operational=[...]`）。验证：未配置 root 时文件腿报告
  scanned=0；配置一个按日期分片的临时目录后仅超龄分片被删（spec「分片产物按档过期」）。
  证据：`task-6.1-file-roots.md`
- [ ] 6.2 明确不把 `workspace/logs/*.db` 与 `logs/tool_audit.ndjson` 纳入任何 root，并在代码注释
  写明原因（mtime 判据对活跃文件无效且危险）。验证：负向用例——把活跃 SQLite 文件放入某目录并
  将该目录配为 root 时，sweep 因未配置而不删；注释与 Non-Goal 一致。证据：并入 `task-6.1-file-roots.md`

## 7. 观测报告与隐私边界（ADR-6/ADR-7）

- [ ] 7.1 每轮输出每实体一条结构化报告（category/target/scanned/deleted/kept/bytes_freed/dry_run/
  errors），错误串经 `core.telemetry.redaction.redact_text`。验证：用例断言报告字段集与
  `SweepReport` 一致、日志中不含被删对象内容片段/凭据摘要。证据：`task-7.1-reports.md`
- [ ] 7.2 不新增 metrics label、不扩 `work_queue_telemetry.ALLOWED_EVENT_FIELDS`：
  `tests/observability_privacy/` 既有白名单与 fixture 契约测试保持全绿，且本 change 未新增 label 注册。
  验证：`grep` 证明无新 `validate_label_names` 调用点 + 白名单契约测试通过。证据：并入 `task-7.1-reports.md`
- [ ] 7.3 **C12 §8.4 承接登记**：在 `openspec/changes/c12-observability-backup/tasks.md` §8.4 标注
  retention 半边由本 change（`p0-retention-wiring`，日期）落地并保留聚合 API 半边为未勾选
  （写法参照 §8.3 由 C6 承接的先例）。验证：`openspec validate c12-observability-backup` 通过；
  双向引用可见。证据：`task-7.3-c12-registration.md`

## 8. 测试闸门与状态回填

- [ ] 8.1 定向回归：`NEXUS_REQUIRE_PG=1 pytest -q -W error tests/retention/ tests/auth_provisioning/
  tests/control_plane/ tests/observability_privacy/`。验证：全绿且无 skip；证据：
  `task-8.1-targeted.txt`
- [ ] 8.2 全量回归：`NEXUS_REQUIRE_PG=1 pytest -q -W error tests/`。验证：对照当前基线
  **1849 passed / 0 failed** 不新增失败；证据：`task-8.2-full-regression.txt`（附非绿采样的定性）
- [ ] 8.3 pyright：改动文件 `--level error` 0 errors；project 全局对照 C6 基线 **38 errors** 不新增
  （分批跑，避免 node 堆溢出）。证据：`task-8.3-pyright.txt`
- [ ] 8.4 `openspec validate p0-retention-wiring` 通过；`openspec status --change p0-retention-wiring`
  四件套 done。证据：命令输出并入 `task-8.1-targeted.txt` 末尾
- [ ] 8.5 状态回填（仅在 8.1–8.4 有证据后）：`PILOT_ROADMAP_PROJECT_CHECKLIST.md` 的 §5.9.17 门禁行
  （"retention 可配置且 job 生效"半边由契约层 → 执行层）、current focus/next decision 中
  C12 §8.4 的归属更新；`openspec/openspec-tasks-bundle/task-12-observability-backup.md` 对应验收行
  补注执行层落地。`SCALING_ROADMAP.md` 仅在存在对应能力行时更新（本 change 无则不动，并在证据中说明）。
  证据：`task-8.5-checklist.md` + git diff
- [ ] 8.6 上线前运维步骤（交付物而非代码）：在部署环境先跑一轮 `run_once(dry_run=True)` 并把报告
  存进 `openspec/evidence/p0-retention-wiring/`，确认"将删除"量符合预期后再放开实删；写入 runbook 片段
  （如何一键 `enabled=false` 回退）。证据：`task-8.6-dryrun-before-live.md`
