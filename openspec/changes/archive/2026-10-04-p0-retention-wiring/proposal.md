## Why

C12 已把三档 retention（operational 30d / audit 180d / debug content 7d）作为**契约原语**交付并 verified
（`core/telemetry/retention.py`，7 项契约测试），但**至今没有任何生产调用方**：既没接进 config，也没有
进程内定时执行，PG 侧各 change 预留的清理挂接点全部悬空（`webchat_replay_frames` 的
`delete_frames_before` 只有测试调用、`alembic/versions/d8e4f2b6a9c1_*.py:11` 自证"retention/清理归
C12 §8.4"）。后果是观测与重放数据只增不减，P0 长期运行没有出口，§8.5 基线报告的前置也不成立。

另一处结构性事实：已 verified 的 `sweep_roots` 按**文件 mtime** 判过期，而真实观测产物根本不是按时间分片
的文件——`workspace/logs/tool_audit.ndjson` 是单文件持续追加（mtime 永远最新，sweep 对它无效），
`logs/{passive,proactive,drift}.db` 是活跃 SQLite 库（按 mtime 删等于删正在写的库）。真正有保留期语义的
数据在 **PG 行级**。因此本 change 的接线主体是 PG 行级裁剪，而不是把文件 sweep 硬套到不适用的产物上。

## What Changes

- 新建 capability **`data-retention`**：把"保留期必须被执行"写成可验收契约（三档归属、分批删除、
  幂等、dry-run 演练、启动不跑、周期任务可停机），并覆盖凭据行的过期处置。
- **config 接线**：新增 `[agent.retention]` 节与 `RetentionConfig`（`enabled` 默认 true、
  `interval_s` 默认 86400、三档天数覆盖冻结默认、`batch_size`、`max_batches`、`purge_grace_s`、
  补发窗口参数 `replay_keep_last_frames` / `replay_max_age_days`）；沿用
  `_load_attachment_config` 的加载期校验模式（非正即报错，未配置即冻结默认）。
- **进程内定时任务**：新增 `bootstrap/retention/` 周期壳（复用 C6 `AttachmentLifecycleRuntime`
  的形状：`create_task` + 异常隔离 + `stop()` 进 shutdown 序列），启动时不跑首轮以免叠加启动风暴。
- **PG 行级裁剪**（按 created_at/at，分批 DELETE，不动计数器与 seq 水位）：
  - `webchat_replay_frames` → **按该会话的补发窗口裁剪**（已确认消费可删、每会话保留最近 N 帧为下限、
    兜底年龄天花板），不再按全局 30d 一刀切（owner 决策，见 design ADR-8）；
  - `tool_audit_events`、`admin_audit_events`、`work_attempts` → audit 180d（`work_attempts` 归 audit 为 owner 定案）；
  - `outbound_delivery_intents` **不删除**（含 `dead_letter` 终态），dead-letter 保留待人工
    redrive/ignore，属 P3 演练依赖。
- **凭据过期处置**：对**同时满足**"已撤销 + 已过期 + 超过 `purge_grace_s`"的 `auth_sessions` /
  `access_tokens` 行抹掉凭据摘要（digest 置为不可用的已清除态），保留行本身与
  `created_at/expires_at/revoked_at/account 归属` 等 metadata 供审计核验；活跃凭据一律不触碰。
  当前两列是 `nullable=False` + `CHECK char_length(digest)=64` + `UNIQUE(digest)`，**无法置空也无法写
  占位值**（占位会撞唯一约束），因此需要一次 expand-only alembic 迁移放开该约束组合（见 design ADR-4，
  双向可逆已列为验收项）。
- **文件 sweep 的诚实边界**：周期任务仍调用 `sweep_roots`，但**只为按时间分片的目录配置 root**；
  Pilot 当前没有这类产物，因此内置 root 集为空、由显式 config 指定。不把 `tool_audit.ndjson`
  与 `*.db` 塞进 sweep root（会误删活跃文件），并把"观测产物按日分片改造"记为 Non-Goal。
- **观测面（C12 协议）**：每轮输出结构化 `SweepReport`（category/root/scanned/deleted/kept/
  bytes_freed/dry_run/errors），并**区分可恢复与不可恢复的删除量**（派生缓冲 vs 事实记录，
  owner 要求，避免事后"是不是把数据删丢了"无从判定）；到普通日志；**不新增 metrics label**，不扩事件白名单 fixture；
  自由文本经 `redact_text`。承接并在 `c12-observability-backup/tasks.md` §8.4 登记 retention 半边完成。
- **备份面**：无新增 manifest kind；`config`/`secrets` 条目不受影响。`admin_audit_events` 归 audit
  档 180d 需在 `tenant_workspace`/PG 一致性说明内自洽（无模板结构变更）。

**BREAKING**：无对外 HTTP/协议契约变化。内部变化是首次让数据真的会被删除（默认开启），因此
`enabled=false` 与 `dry_run` 手动入口必须同时存在，验收要求先演练后实删。
- **待 owner 填数**：补发窗口的两个参数（每会话保留下限帧数、兜底年龄天数）与凭据作废前的等待期，
  见 design Open Questions；实现阶段不得擅自使用默认数值代替决策。

## Capabilities

### New Capabilities

- `data-retention`: 观测与重放数据的保留期执行契约——三档归属、PG 行级分批裁剪、幂等与 dry-run
  演练、周期任务的启停与异常隔离、过期凭据行的摘要抹除与 metadata 保留、以及"哪些产物不适用
  文件 sweep"的边界。

### Modified Capabilities

（无）`observability-privacy` 的三档 retention 与 admin 审计条款目前**只存在于仍 active 的 change
`c12-observability-backup` 内、尚未 sync 到 `openspec/specs/`**，对其写 MODIFIED delta 没有可修改的
主体规格。本 change 以新建 capability 承接 §8.4 的行为契约，并在 c12 tasks §8.4 登记归属；c12 归档时
其 `observability-privacy` 仍按原样 sync，两者不重叠（前者定义 TTL 契约，后者定义执行与凭据处置）。

## Impact

- **代码**：`agent/config_models.py`（`RetentionConfig`）、`agent/config.py`（`_load_retention_config`）、
  `config.example.toml`、新增 `bootstrap/retention/`（周期壳 + PG sweeper + 报告形态）、
  `bootstrap/db/repository/`（`WebchatReplayRepository` 及 audit/session/token 侧新增按时间分批删除方法）、
  `bootstrap/app.py`（启动接线 + shutdown 步骤注册）。
- **迁移**：1 个新 alembic revision（expand-only，仅放开 digest 列的 NOT NULL/CHECK/UNIQUE 组合以支持
  已清除态；不删列、不改语义），head 从当前 tip 接续；双向可逆（upgrade→downgrade→upgrade）为验收项。
- **测试**：新增 `tests/retention/`（config 冻结默认与校验、三档归属矩阵、分批删除与幂等、
  dry-run 零变更、intents 不被删、活跃凭据不被触碰、过期凭据 digest 已清除而 metadata 保留、
  周期任务启停与异常隔离）。
- **运行影响**：部署后默认开启会删除窗口外的历史观测行（审计流 180d；补发缓冲按会话窗口 + 兜底
  天花板）；Pilot 当前 PG 数据量极小
  （生产 `outbound_delivery_intents` 2 行、`consolidation_events` 0 行），首轮删除量可忽略，但仍要求
  先跑 `dry_run` 并留存报告。
- **不触碰**：`canonical_messages`/`canonical_conversations`/`inbox_records`/`turns` 的保留期（消息
  retention 属账号生命周期议题）；观测产物写入器；聚合 API；SLO 阈值。

## Non-Goals

- **不做观测产物按日分片（rotation）**：`tool_audit.ndjson` 与 `logs/*.db` 的 mtime 语义使文件 sweep
  对它们无效且危险；改造写入器属独立 change，本 change 只把它们明确排除在 sweep root 之外。
- **不删除 `outbound_delivery_intents`**：含 `dead_letter`，其 redrive/ignore 人工处置是 P3 演练依赖。
- **不实现总控台聚合 API**（§8.4 的另一半）与 Dashboard 展示；不做 §8.2 admin 内容查看端点。
- **不在编码前引入 SLO/容量阈值**（§8.5 基线报告之前禁止）。
- **不扩事件字段集与 metrics label 白名单**：沿用 `work_queue_telemetry.ALLOWED_EVENT_FIELDS` 单一来源。
- **不新增 backup manifest kind**，不改校验器。
- **不物理删除审计链行**：`admin_audit_events`/`tool_audit_events` 到期是按保留期裁剪，不提前抹除；
  凭据行只抹 digest、不删行。
