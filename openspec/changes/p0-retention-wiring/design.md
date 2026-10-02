# C12 §8.4 retention 接线 — design

> 对应 `openspec/changes/p0-retention-wiring/`；ADR-1..ADR-7 为实现约束，偏离需先改本 design。
> 上游事实来源：`openspec/PILOT_ROADMAP.md` §5.9.17（保留期默认值）、§5.9.12（审计链不物理删除）、
> §5.9.3（凭据 digest-only）、P3 出口（凭据过期清理）；契约原语 `core/telemetry/retention.py`
> （C12 已 verified，7 项测试）；归属登记 `openspec/changes/c12-observability-backup/tasks.md` §8.4。

## Context

**现状事实（已核实，非推测）**

| 事实 | 位置 |
| --- | --- |
| 三档 TTL 与文件 sweep 是纯契约，生产零调用方 | `core/telemetry/retention.py`（`grep` 无 bootstrap 侧调用） |
| 重放帧删除入口已预留但只有测试调用 | `bootstrap/db/repository/control_plane_repo.py:1056`（`delete_frames_before`），调用点仅 `tests/control_plane/test_webchat_replay_frames.py:216`、`tests/auth_provisioning/test_webchat_rebuild_reconcile.py:105` |
| 迁移自己声明清理归 §8.4 | `alembic/versions/d8e4f2b6a9c1_webchat_replay_frames.py:11` |
| 帧缺失读侧已有降级语义（不静默补发） | `bootstrap/webchat_durable.py:449-452`、`:124` |
| 单机审计兜底是**单文件持续追加** | `agent/admission/tool_audit.py:159-175` → `<workspace>/logs/tool_audit.ndjson` |
| 链路日志是**活跃 SQLite 库**、无轮转 | `bootstrap/tools.py:879-913`（`logs/{passive,proactive,drift}.db`） |
| `ContentCaptureGate` 明确不落盘 | `core/telemetry/redaction.py:255-260`（"进程内状态，不持久化"）→ 内容型 debug 档无文件 root 可接 |
| 凭据摘要列不可空且带唯一约束 | `bootstrap/db/models/auth.py:57-60`（`uq_access_tokens_digest` + `ck_access_tokens_digest_sha256`）、`:106`（`uq_auth_sessions_digest`）；`token_digest:80` / `session_digest:127` 均 `nullable=False` |
| admin 审计已有落库路径但字段形状不同 | `bootstrap/db/repository/auth_repo.py:643-662`（`AuthRepository.audit` → `admin_audit_events`，actor/target_type，非 `AdminAccessAuditEvent` 的 principal/target_kind） |
| 周期任务范式可套用 | `bootstrap/attachments/runtime.py`（tick 循环 + 异常隔离 + `stop()`）、`bootstrap/app.py:825-862`（启停）、`:914+`（shutdown 步骤序列） |
| 配置节模式 | `agent/config.py:524`（`_load_attachment_config` 的 `_int/_float/_nonneg_int` 加载期校验） |

**约束**：PG 是 Pilot 的 durable source of truth（已激活）；Pilot 单进程；不引入新依赖；
不在 §8.5 基线报告前引入 SLO/容量阈值。

## Goals / Non-Goals

**Goals:**
- 让"保留期"从纸面契约变成**默认会发生的行为**，且默认行为可被一键关闭、可先演练后实删。
- 给每类数据实体一个**唯一且可测**的保留档归属，把"不该删的东西"写成硬边界（投递意图、业务消息、活跃凭据）。
- 过期凭据的处置满足 §5.9.12 的两面性：凭据不可再用，但审计链不被物理抹平。
- 与 C12 隐私契约对齐：执行报告只是 metadata，不新增 label、不扩事件白名单。

**Non-Goals:**
- 不改造观测产物写入器做按日分片（rotation）。
- 不做总控台聚合 API、不做 §8.2 admin 内容查看端点。
- 不删投递意图（含 dead_letter）、不动消息/会话保留期。
- 不引入内容型 debug 文件的清理 root（当前无此类落盘）。

## Decisions

### ADR-1 接线主体是 PG 行级裁剪，不是文件 sweep

**结论**：周期任务同时具备两条腿，但只有 PG 行级腿配置了实体清单；文件腿保留 `sweep_roots` 调用，
内置 root 集为空，需由 config 显式指定目录才生效。

**理由**：`sweep_roots` 的过期判据是**文件 mtime**（`retention.py:140`），而项目里真实观测产物要么是
单文件持续追加（`tool_audit.ndjson`，mtime 永远最新 → 永不过期），要么是仍在读写的活动 SQLite 库
（`logs/*.db`，按 mtime 删等于删正在用的库），内容型 debug 采集根本不落盘。把 sweep_roots 硬套到
这些路径上，结果只有两种：无效或误删。真正持续增长的、有明确写入时间的数据在 PG 表里。

**备选（不选）**：
- ① 同时改造写入器为按日分片：能真正启用文件语义，但要动 C7 审计适配器与 C4 链路日志的写入路径与其
  既有测试，体量与风险都不属于"接线"这一步；记为后续独立 change。
- ② 只做文件侧最小接线：看起来最保守，但 replay frames 这个真实增长点仍无主，§8.4 名义完成而实际
  什么都没收敛。
- ③ 把 `logs/*.db`、`tool_audit.ndjson` 塞进 sweep root：会删除活跃文件，属数据销毁级缺陷，拒绝。

### ADR-2 保留档归属矩阵与不可删除集

| 实体 | 档 | 判据 | 备注 |
| --- | --- | --- | --- |
| `webchat_replay_frames` | operational 30d | `created_at` | 经既有游标删除语义；**不回退 seq 水位** |
| `tool_audit_events` | audit 180d | `created_at` | C7 ADR-6 审计流 |
| `admin_audit_events` | audit 180d | `created_at` | C5 落库路径（字段形状差异见 ADR-6） |
| `work_attempts` | audit 180d | `started_at`/`created_at` | C15 ADR-2 定案为"只追加审计流"，故归 audit 而非 operational |
| `outbound_delivery_intents` | **不删** | — | 含 `dead_letter`；redrive/ignore 是人工处置与 P3 演练依赖 |
| `background_work_items` 非终态 | **不删** | — | 终态行随后续议题处理，本 change 不动，避免与 lease/recovery 语义打架 |
| `canonical_messages` / `canonical_conversations` / `inbox_records` / `turns` | **不删** | — | 消息保留期属账号生命周期（roadmap 只说"已引用 attachment 跟随 message/account retention policy"） |
| `attachments` | 由 C6 自有生命周期 | `retention_deadline` | 不归本能力，避免两条腿删同一实体 |

**理由**：审计类归 180d 的直接依据是 §5.9.17（audit metadata 建议 180 天）与 §5.9.12（封禁保留记录写
`revoked_at`、不物理删除审计链）；重放帧是**可从 canonical 重建的派生补拉缓冲**，归 operational 30d
最贴合其价值窗口，且读侧已有"帧缺失 → 客户端重建"的既定降级语义（`webchat_durable.py:449-452`）。

**备选（不选）**：① 全部统一 30d——实现最省，但审计链只留 30 天，与 §5.9.17 的 audit 180d 直接冲突；
② 连 `work_attempts` 与 intents 都不动——最保守，但 C15 的只追加尝试流会成为新的无界增长点，
把问题推给下一轮。

### ADR-3 周期壳：独立 `bootstrap/retention/`，套用 C6 形状，启动不跑首轮

**结论**：新增 `RetentionRuntime`（`enabled` + `interval_s`，默认 86400）由 `bootstrap/app.py` 装配，
`asyncio.create_task(..., name="retention_sweep")` + `add_done_callback`，停机函数注册进现有 shutdown
步骤序列；单轮内按实体顺序执行，逐实体异常隔离（记录后继续下一个），`CancelledError` 正常退出。
**启动时不执行首轮删除**；`run_once(dry_run=True)` 作为管理员显式演练入口保留。

**理由**：C6 的 `AttachmentLifecycleRuntime` 已把这套形状（tick、异常隔离、stop、dry-run 报告契约
对齐 `SweepReport`）在本仓库验证过，直接同构可降低评审成本；启动阶段已经有
`reconcile_webchat_on_startup`、分区 reconciliation、attachment 启动对账三件事，再叠一轮删除风暴
会把"删除"和"恢复"混在同一时间窗里，出问题难归因。

**备选（不选）**：① 复用 `bootstrap/scheduler`（显式用户 schedule 的运行时）：把系统级维护与用户可见
调度混在一个执行器里，权限与失败语义都会互相污染；② 每档一个独立任务：三个周期互相不知道对方在删
同库，锁与膨胀更难预测。

### ADR-4 凭据摘要抹除用 NULL + 放宽约束，expand-only 迁移

**结论**：一次 expand-only alembic revision：
- `auth_sessions.session_digest`、`access_tokens.token_digest` 由 `NOT NULL` 改为可空；
- `access_tokens` 的 `ck_access_tokens_digest_sha256` 由 `char_length(token_digest)=64` 改为
  `token_digest IS NULL OR char_length(token_digest)=64`；
- 两张表的 UNIQUE（`uq_auth_sessions_digest` / `uq_access_tokens_digest`）**保留不动**——
  PostgreSQL 默认把 NULL 视为互不相同，多行已清除记录可共存；
- 模型侧改为 `Mapped[str | None]`。

清除条件三者同时满足：`revoked_at IS NOT NULL` ∧（`expires_at IS NOT NULL AND expires_at < now`）
∧ `updated_at/revoked_at` 超过 `purge_grace_s`（默认 30d，跟随 audit 档，留足事后追查窗口）。
只 UPDATE 摘要为 NULL，**不删行**，保留 `account_id/principal_type/created_at/expires_at/revoked_at/
revoked_reason/digest_version` 等 metadata。

**理由**：现有列是 `nullable=False` + `CHECK char_length=64` + `UNIQUE`（`auth.py:57-60,106,80,127`），
所以"置空"被 NOT NULL 挡住、"写占位摘要"会被 UNIQUE 挡住（多行同值直接冲突），而占位值还会让
按摘要查询的校验路径存在误命中风险。NULL 是唯一同时满足"凭据不可用"与"行仍可审计"的表示。
按摘要查找的路径（`validate_session` 等）拿不到 NULL 匹配，天然 fail-closed。

**备选（不选）**：① 物理删除过期 session/token 行：与 §5.9.12"保留记录并写 `revoked_at`，不物理删除
审计链"直接冲突，恢复演练时也失去可核验性；② 写 64 个 0 之类的占位摘要：撞 UNIQUE，且语义上仍是
"一个合法形状的摘要"，未来若有按摘要去重的逻辑会误判；③ 新增 `purged_at` 布尔列而不动 digest：
既要多一列又要保证"purged=true 时 digest 仍可用"的不变式，复杂度高于 NULL；④ 改 partial unique
index（`WHERE digest IS NOT NULL`）：可行但多一次索引重建，且依赖默认 NULLS DISTINCT 已足够，
本 ADR 以真实 PG 测试固定该行为（见测试策略），不提前动索引。

### ADR-5 分批删除 + 计数下推 + 单轮上限

**结论**：每个实体的删除方法按时间列 `ORDER BY <ts> ASC LIMIT batch_size` 选取并 DELETE，返回删除行数；
`batch_size` 默认 500、可配置；单轮对同一实体最多循环 `max_batches`（默认 20），超出留给下一轮。
时间比较一律用数据库时钟（与 `claim`/`now()` 同源，避免应用时钟漂移）。报告如实记 scanned/deleted/kept。

**理由**：与刚完成的 C6 加固同源——`list_expired` 此前在应用层全表取回再过滤，已作为质量问题修掉；
保留期执行不能重犯。分批 + 上限把单次事务规模与锁持有时间封顶，也天然满足"幂等：第二轮删除为 0"。

**备选（不选）**：一条 `DELETE ... WHERE ts < cutoff` 全量删：实现最短，但在审计流上会产生长事务、
索引膨胀与锁放大，且失败即整批回滚无部分收敛。

### ADR-6 admin 审计的字段形状差异本 change 不统一

**结论**：保留期执行按 `admin_audit_events` 现有列（actor/action/target_type/target_id/detail）裁剪，
**不引入 `AdminAccessAuditEvent`→表的映射改造**。

**理由**：字段对齐属 §8.2（内容查看端点落地时才有真实写入方），本 change 若顺手改表语义会把两个
未定承载方的条目耦合成一次变更。design 记录该分歧，避免后人误以为是遗漏。

### ADR-7 观测面：只出结构化报告，不扩指标与事件白名单

**结论**：每轮以现有 logger 输出每实体一条 `SweepReport` 形态记录（category/target/scanned/deleted/
kept/bytes_freed/dry_run/errors），自由文本（错误串）过 `core/telemetry/redaction.redact_text`。
**不新增 metrics 计数器与 label**；不复用 `work_queue_telemetry.ALLOWED_EVENT_FIELDS` 之外的字段集。

**理由**：§5.9.17 禁止高基数身份字段进 label，而"存储标识/对象标识"正是这类维度；Pilot 阶段一条
结构化日志足以支撑排障与 §8.5 基线采集，指标化留待聚合 API（§8.4 另一半）统一设计。

## Risks / Trade-offs

- **默认开启即开始删数据**：首次部署会删除窗口外的历史重放帧与审计行。缓解：Pilot 当前数据量极小
  （生产 `consolidation_events` 0 行、`outbound_delivery_intents` 2 行），验收要求先以 `dry_run`
  存证报告再放开实删；`enabled=false` 一键回退。
- **触及 C5 安全表约束**：digest 由 NOT NULL 改可空是对凭据模型的一次放宽。缓解：expand-only、
  双向可逆实测、活跃凭据路径不读 NULL、`digest_version` 与状态列保留轮换可追溯性；验收含
  "两条已清除行可共存"与"清除后校验必然失败"的负向测试。
- **重放帧被删后补拉能力下降**：30d 之外重连只拿到"需客户端重建"信号。这是 §5.9.6 既有语义
  （canonical 仍是真源），不是新行为；风险在于长离线客户端体验，留待 §8.5 基线观察。
- **表膨胀与 autovacuum**：分批 UPDATE 置 NULL + DELETE 会产生死元组。Pilot 规模可忽略，正式启用前
  与 roadmap C1D 生产规模验证一并复评。
- **两条腿删同一实体的隐患**：已通过 ADR-2 明确 `attachments` 归 C6、重放帧/审计流归本能力，
  边界写进 spec 的"不可删除集"场景，防后续漂移。
- **`work_attempts` 归 audit 180d 的判断可辩**：C15 ADR-2 定案它是"只追加审计流"，但生命周期上
  更像 operational。本 design 采纳 audit（与"追加型审计"定案一致），若后续按 operational 重判，
  改 config 即可，不需改代码。

## Rollback

- 运行期回退：`[agent.retention] enabled = false` → 周期任务不装配，零删除。
- 代码回退：本 change 单一线性 commit 链（迁移 / sweeper / 接线 / 测试各自独立），revert 即回到
  "契约存在但无调用方"的现状，不影响其他能力。
- Schema 回退：迁移的 downgrade 恢复 NOT NULL 与原始 CHECK；回退前需确保没有 NULL digest 残留
  （downgrade 内先断言/清理，见 tasks 验证项）。

## 测试策略

- **契约层（新增 `tests/retention/`）**：三档归属矩阵（重放帧 30d 删 / 审计 180d 留 100d 行 /
  intents 永不删 / 业务消息不动 / attachments 不由本能力删）；分批与单轮上限（超量剩余额留待下轮，
  计数如实）；幂等（第二轮 deleted=0）；时间判据用 DB 时钟（注入未来时间验证边界）；
  dry-run 零变更且与实删计数一致；逐条失败不中断；周期任务启停 + 单实体异常隔离 + 启动不跑首轮；
  非法 config 加载期报错；`enabled=false` 零删除。
- **凭据面（真实 PG 集成）**：已撤销+已过期+超宽限 → digest 为 NULL 而 metadata 完整可查；
  清除后按原摘要校验必然失败（fail-closed）；活跃会话/未过期 token 原样；已撤销但未过期不清除；
  **两条 NULL digest 行可共存**（固化 PG NULLS DISTINCT 行为，绑定部署的 PG 版本）；
  `count_active_admin_sessions` 等既有查询不受影响。
- **迁移**：`upgrade → downgrade → upgrade` 闭环实跑，记录于 `openspec/evidence/p0-retention-wiring/`。
- **回归与静态**：`NEXUS_REQUIRE_PG=1 pytest -q -W error tests/` 对照当前 1849 基线不新增失败；
  改动文件 `pyright --level error` 0 errors，project 全局对照 38 基线不新增。
- **C12 协议**：`openspec/changes/c12-observability-backup/tasks.md` §8.4 登记 retention 半边由本
  change 承接（参照 §8.3 由 C6 承接的写法），并在本 change tasks 内双向引用。

## Open Questions

（无遗留决策项。以下两点为实现期需在真实环境**核实而非重新决策**的事实：
① 部署 PG 版本对 `UNIQUE` 中 NULL 的处理（本 ADR-4 依赖默认 NULLS DISTINCT，测试固化）；
② `work_attempts` 时间列在现有模型中的准确名称与是否可空，决定排序列选择。）
