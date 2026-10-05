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

## 业务口径（决策对运营意味着什么）

- **清理默认开启**，每天一轮，开机不跑首轮；上线前必须先跑一轮"只报告不删除"的演练并留存报告。
- **工作项尝试流水按审计留 180 天**（owner 定案，与 C15"只追加审计流"定案一致）。
- **登录凭据的过期处置保留在本轮**：作废凭据指纹但保留记录行，代价是一次只放宽、不删列的
  数据库变更（ADR-4）。
- **补发缓冲不再按全局 30 天一刀切**：改按"这台设备是否还需要补发"判定，并按会话保留最小缓冲、
  设兜底天花板（ADR-8）。窗口数值待 owner 确认。
- **报告必须能回答"这次删掉的数据里有多少是永久丢失的"**：可恢复与不可恢复分开计量（ADR-7）。

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
| `webchat_replay_frames` | **按会话补发窗口**（非全局年龄） | 消费确认游标 + 兜底上限 | 经既有游标删除语义；**不回退 seq 水位**；见 ADR-8 |
| `tool_audit_events` | audit 180d | `created_at` | C7 ADR-6 审计流 |
| `admin_audit_events` | audit 180d | `created_at` | C5 落库路径（字段形状差异见 ADR-6） |
| `work_attempts` | audit 180d | `started_at`/`created_at` | C15 ADR-2 定案为"只追加审计流"，故归 audit 而非 operational |
| `outbound_delivery_intents` | **不删** | — | 含 `dead_letter`；redrive/ignore 是人工处置与 P3 演练依赖 |
| `background_work_items` 非终态 | **不删** | — | 终态行随后续议题处理，本 change 不动，避免与 lease/recovery 语义打架 |
| `canonical_messages` / `canonical_conversations` / `inbox_records` / `turns` | **不删** | — | 消息保留期属账号生命周期（roadmap 只说"已引用 attachment 跟随 message/account retention policy"） |
| `attachments` | 由 C6 自有生命周期 | `retention_deadline` | 不归本能力，避免两条腿删同一实体 |

**理由**：审计类归 180d 的直接依据是 §5.9.17（audit metadata 建议 180 天）与 §5.9.12（封禁保留记录写
`revoked_at`、不物理删除审计链）；`work_attempts` 按 owner 决策归 audit（与 C15 ADR-2"只追加审计流"
定案一致）。会话重放帧**不按全局年龄归档**——它是"设备还没收到"的缓冲，用统一年龄一刀切会删掉仍被
需要的补发数据，改按会话补发窗口判定，见 ADR-8。

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

### ADR-7 观测面：只出结构化报告并区分可恢复性，不扩指标与事件白名单

**结论**：每轮以现有 logger 输出每实体一条 `SweepReport` 形态记录（category/target/scanned/deleted/

kept/bytes_freed/dry_run/errors），错误串过 `core.telemetry.redaction.redact_text`；报告额外给出

**可恢复性分类**：`recoverable`（派生数据，可从 canonical 消息等真源重建——补发缓冲）与

`irrecoverable`（不可恢复的事实记录——三条审计流与凭据摘要作废）分别计数与体量。

**不新增 metrics 计数器与 label**；不复用 `work_queue_telemetry.ALLOWED_EVENT_FIELDS` 之外的字段集。

**理由**：owner 明确要求"本可恢复的数据量"进报告，动机是事后判定"这次清理有没有造成永久丢失"

不该靠记忆或争论。分类不引入新数据源：每条被删记录属于哪个实体已知，映射到可恢复/不可恢复是纯

静态归类，成本为零而证据价值高。指标化仍禁止（§5.9.17 高基数 label 红线），Pilot 阶段结构化日志

足以支撑排障与 §8.5 基线采集。

**备选（不选）**：只写一句"删除 N 条"——出问题时无法回答"其中多少不可恢复"，正是 owner 要避免的

扯皮场景。

### ADR-8 会话重放帧按"该会话的补发窗口"裁剪，不按全局年龄

**结论**：重放帧的删除判据改为**以会话自身进度为准**，三条规则同时生效：
1. 只裁剪**已被客户端确认消费**（游标已越过）的帧；未确认消费的帧不因年龄被删；
2. 每个会话**保留最近 N 帧**作为下限（`replay_keep_last_frames`），保证重连永远有缓冲；
3. 一个**兜底年龄天花板**（`replay_max_age_days`）约束长期离线会话，超限部分可被裁剪，
   读侧据 seq 水位差走既有的"需要客户端重建"路径（`webchat_durable.py:449-452`）。
具体数值由 owner 确认后填入 config（见 Open Questions），实现不得自定。

**理由**：补发缓冲的性质是"这台设备还没收到"，与数据本身的老新无关。按全局 30 天一刀切，
会在一台离线 31 天的设备上删掉它唯一缺的那段历史——用户视角就是"消息丢了"，而系统里正式消息
其实完好，扯不清。按会话窗口裁剪把判据换成"是否还需要补发"，语义与用途一致；保留下限与兜底
上限分别封住"重连拿到空缓冲"和"离线会话无限堆积"两个反向失效。

**备选（不选）**：① 全局 30d 硬删（本 change 初稿方案）——被 owner 否决，理由同上；
② 只按消费确认删、不设天花板：长期离线/永不重连的会话会成为无界增长点，且没有任何机制回收；
③ 永不删补发缓冲：把问题原样留给备份与存储成本，等于 §8.4 未落地。

## Risks / Trade-offs

- **默认开启即开始删数据**：首次部署会删除窗口外的历史重放帧与审计行。缓解：Pilot 当前数据量极小
  （生产 `consolidation_events` 0 行、`outbound_delivery_intents` 2 行），验收要求先以 `dry_run`
  存证报告再放开实删；`enabled=false` 一键回退。
- **触及 C5 安全表约束**：digest 由 NOT NULL 改可空是对凭据模型的一次放宽。缓解：expand-only、
  双向可逆实测、活跃凭据路径不读 NULL、`digest_version` 与状态列保留轮换可追溯性；验收含
  "两条已清除行可共存"与"清除后校验必然失败"的负向测试。
- **按会话裁剪补发缓冲的代价**：判据从单一时间列变成"消费确认 + 每会话下限 +
  兜底上限"三条，实现与测试复杂度上升；且每轮需要按会话取游标，扫描成本高于纯年龄删除。
  Pilot 会话数小可接受，正式放量前与 C1D 一并复评。兜底天花板仍是无界堆积的最后防线。
- **消费游标为 per-conversation MAX**：同一会话多设备时，落后设备的未消费帧可能因另一设备
  的 `replay.after_seq` 声明被裁，重连走既有 `replay_required` → REST 重建降级（canonical
  仍为真源，无数据丢失）。Pilot 以单账号单设备为主；若多设备成为常态，改 per-session
  MIN 游标属独立小迁移（列在 counters 上加 session 维度或换表），不影响本 change 契约。
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

**owner 数值（2026-10-06 已确认）**：owner 已确认四项**结构性**决策（git `adf282d7`）；
三个**数值**由 owner 于 2026-10-06 确认按 design 候选值定为初始值：
`replay_keep_last_frames=20`、`replay_max_age_days=30`、`purge_grace_s=30d`。
均为纯 config 参数，后续调整只改 `[agent.retention]` 不需要改代码。

**实现期需核实的事实（已核实，见 evidence `task-3.1-cursor-source.md`）**：

- 部署 PostgreSQL 对 UNIQUE 中 NULL 的处理（ADR-4 依赖默认 NULLS DISTINCT，测试固化；不成立则回到
  ADR-4 改 partial unique index 并更新本 design）。
- `work_attempts` 的时间列：确认为 `started_at`（NOT NULL，server_default now()），排序用。
- ~~"已确认消费"的游标来源~~ **已核实（2026-10-03）**：`current_seq`/`oldest_seq`/hello
  `latest_seq` 均为服务端水位或窗口边界，**不等于**"客户端已收到"；唯一语义匹配的是客户端
  `replay {after_seq}` 声明，但此前不持久化。落地：`webchat_replay_counters` 增列
  `consumed_seq BIGINT NULL`（第二个 expand-only revision `d0a9b7c3e1f5`），
  `WebchatDurableService.replay_after` 服务补拉前持久化 `GREATEST(既有, LEAST(after_seq, 水位))`
  （声明超前水位不推高；持久化失败不阻断补拉，下次 replay 声明自愈）；NULL = 从未声明 → 消费侧
  判据不生效，帧只受下限/天花板约束。ADR-8 第 1 条"游标已越过"即指该持久化游标。
