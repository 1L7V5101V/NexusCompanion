# C2 durable control plane — 设计冻结（P-1）

> 门禁依据：PILOT_ROADMAP §5.9.6（durable state 与 restart recovery）、§5.9.9（数据模型/唯一约束/首次启用）、§5.9.11（Ingress、canonical message、outbox 与 delivery transaction）、§10 DECIDED（Ingress/outbox/delivery：三事务边界不可合并、`sent` 必须由 ack 推进、delta 不进入恢复承诺）。
> 本文档把 §10「OPEN FOR P-1 SPEC — Exact schema/DDL」的 inbox/turn/tool/work/outbox/delivery 精确字段收敛为可执行 DDL 与约束命名。

## 1. 三事务边界（§5.9.11 冻结语义逐条落地）

| §5.9.11 冻结语义 | 本 change 落地 |
| --- | --- |
| 入站接受事务原子写入 inbox/dedupe + canonical user message + queued turn/work，提交后才算 accepted | `IngressRepository.accept_inbound()`：单事务 = dedupe 插入（冲突即整体零写入）→ canonical user message（复用 C1 sequence 取号）→ `inbox_records(status='accepted')` → `turns(status='queued')` → 可选 `background_work_items(status='queued')`；提交前不向调用方返回成功 |
| Telegram source message id 与 WebChat `client_message_id` 是强制幂等键，不允许只靠连接顺序或内存去重 | `message_deduplication_keys` 表 + 两个**部分唯一索引**（ADR-2）；重复注入返回既有身份（ADR-3），数据库约束兜底，不依赖应用内存状态 |
| 执行完成事务原子写入 final assistant message + turn terminal + outbound delivery intent | `TurnControlRepository.complete_turn_with_delivery()`：单事务 = assistant canonical message（sequence 取号）→ turn 终态 + `final_message_id` → `outbound_delivery_intents(status='pending')` |
| 模型生成完成 ≠ channel 已送达；delivery worker 单独记录状态/attempt/provider receipt/error/时间戳；`sent` 只能由 ack 推进 | `OutboundDeliveryWorker` + `DeliveryRepository`：intent/attempt 独立成表；`sent` 分支只在发送 callback 返回成功 receipt 后进入（负向测试覆盖：无 ack 永不 sent） |
| delivery 用数据库 lease 认领；`lease_ttl=60s`、heartbeat `20s`；单次 attempt 最多 5 次，退避 `1m/5m/30m/2h/6h`，终态 `dead_letter`；均为可配置初始值 | `DeliveryWorkerConfig`（frozen dataclass，字段与默认值一一对应）；claim/heartbeat/接管 SQL 见 ADR-5 |
| 重启后只重试未确认送达的 intent，不重新生成 assistant final message | final message 只在执行完成事务内创建；worker 只读 intent 表做补投（重启重放测试断言 final 消息数不变） |
| `complete_inbound` 或等价状态表示 durable acceptance/terminal processing 已收束，不表示 channel 已成功展示；用户可见 delivery failure 可从 canonical final message 补拉或由管理员重投 | `inbox_records.status`：`accepted → processed`（turn 到达终态即 processed，与投递结果无关）；补拉 = C1 `fetch_messages(after_sequence)`；重投 = `redrive_dead_letter()`（ADR-6） |
| 流式 delta 不进入恢复承诺 | 无任何 delta 持久化路径；`turns`/`canonical_messages` 只落终态与 final |

## 2. ADR 记录

### ADR-1 三事务边界不可合并，T3 是「attempt 记录 + 状态推进」而非第三个大事务

- **决策**：T1（接受）与 T2（完成）各为单个 PG 事务；T3 = 每次投递尝试独立记录 `delivery_attempts` 行并在**独立短事务**中推进 intent 状态，不与任何业务写入合并。
- **备选**：把 delivery ack 与 turn 终态合并进 T2。**不选**：§10 DECIDED 明文「三事务边界不可合并」；投递结果到达时间不确定，合并会把不确定的外部等待拖进业务事务。
- **后果**：「模型完成」与「已送达」天然可观测分离；投递可无限期重试而不影响 canonical 数据一致性。

### ADR-2 幂等双键 = `message_deduplication_keys` 单表 + 两个部分唯一索引

- **决策**：单表承载两类键，列全部 nullable，用**部分唯一索引**（partial unique index）分别强制：
  - Telegram 侧：`(source_channel, source_identity_id, source_message_id)` WHERE `source_message_id IS NOT NULL`；
  - WebChat 侧：`(account_id, client_message_id)` WHERE `client_message_id IS NOT NULL`。
- **备选 1**：两张键表。**不选**：§5.9.9 实体清单只有一个 `message_deduplication_keys`；两类键的生命周期与 inbox 记录一致，分表徒增 join。
- **备选 2**：全表多列 UNIQUE。**不选**：PG 对含 NULL 的唯一索引不生效（NULL ≠ NULL），全表约束无法覆盖「另一侧键为空」的行，必须用部分索引表达「该键存在时唯一」。
- **后果**：同一行恰好持有一类键（CHECK 保证至少持有一类）；约束冲突即重复注入，数据库兜底，不依赖内存。

### ADR-3 重复注入 = `ON CONFLICT DO NOTHING` + 回查返回既有身份，不抛错、零写入

- **决策**：`accept_inbound()` 先对 dedupe 表 `INSERT ... ON CONFLICT DO NOTHING`（显式指定冲突目标的 `index_where`）；未插入任何行即视为重复，回查既有 dedupe/inbox/message 身份并返回 `{"duplicate": True, ...}`，事务提交（全程零新写入）。调用方据此走幂等成功路径（如 WebChat 重发、Telegram 重投）。
- **备选**：先 SELECT 后 INSERT。**不选**：并发下有 TOCTOU 窗口，仍靠唯一约束兜底会抛 IntegrityError，把「幂等成功」误报为失败。
- **后果**：双通道并发重放同一消息只产生一条 canonical user message 与一条 inbox 记录（并发测试覆盖）。

### ADR-4 outbox intent 稳定 idempotency key；默认一条 final message 一个 intent

- **决策**：`outbound_delivery_intents.idempotency_key` NOT NULL UNIQUE。默认键由仓储层按 `msg:<canonical_message_id>` 生成（一条 final message 恰一个投递意图）；多目标分发（未来 push/schedule 场景）由调用方显式传键（如 `msg:<id>:target:<chat>`），唯一约束防重复建 intent。
- **备选**：以 `(turn_id, channel, chat_id)` 复合唯一。**不选**：复合键把「同 turn 重发到同一目标」这一真实重试语义误判为冲突，且无法覆盖无 turn 的出站。
- **后果**：delivery attempt 只推进自己的 intent；intent 重复创建被唯一约束拒绝（§5.9.9）。

### ADR-5 delivery 状态机与 lease 认领（收敛 §5.9.11 运行参数）

- **状态机**：`pending → attempting → sent`（终态）；`attempting → failed`（可重试，带 `next_attempt_at` 退避）；`failed → attempting`（到期再认领）；attempt 次数达到上限 → `dead_letter`（终态，仅管理员 redrive 可回到 `pending`）。
- **认领（claim）**：短事务 `UPDATE ... WHERE id IN (SELECT ... WHERE (status IN ('pending','failed') AND next_attempt_at <= now()) OR (status = 'attempting' AND lease_expires_at < now()) ORDER BY next_attempt_at LIMIT :batch FOR UPDATE SKIP LOCKED) SET status='attempting', lease_owner=:owner, lease_expires_at=now()+ttl, attempt_count=attempt_count+1 RETURNING ...`。`FOR UPDATE SKIP LOCKED` 让多扫描器互不阻塞；stale lease（超过 60s 无 heartbeat）由第二条分支自然接管。
- **heartbeat**：`UPDATE ... SET lease_expires_at = now()+ttl WHERE id=:id AND lease_owner=:owner AND status='attempting'`；影响行数 0 = 租约已失，本次 attempt 不得再写 `sent`（防双 worker 重复发送成功态）。
- **失败/退避**：attempt 失败 → 记 `delivery_attempts(outcome='failed')`，清空 lease；`attempt_count >= max_attempts(5)` → `dead_letter`，否则 `failed` 且 `next_attempt_at = now() + backoff[attempt_count-1]`（`1m/5m/30m/2h/6h`）。
- **成功**：attempt 回调返回 receipt → 记 `delivery_attempts(outcome='sent', provider_receipt=...)`，intent `status='sent'`、`sent_at=now()`。
- **备选**：应用内存队列 + 定时器。**不选**：重启即丢，正是本 change 要消除的形态。
- **后果**：at-least-once 语义成立；重复发送窗口由幂等键 + lease 压缩到「发送方不 ack 且租约过期」区间，Pilot 规模可接受（§5.9.11 同款默认值）。

### ADR-6 dead-letter 管理员处置：redrive 只能追加记录，不能删除/重置历史

- **决策**：`redrive_dead_letter(intent_id, reason)` 在 `delivery_attempts` 追加一行 `outcome='redrive'`（`error` 列存原因，`provider_receipt` 为空），随后把 intent 复位为 `pending`、`attempt_count=0`、`next_attempt_at=now()`、lease 清空。原 attempt 行、receipt、时间戳全部保留；非 `dead_letter` 状态调用 redrive 抛错。
- **备选**：单独 admin 动作表。**不选**：§5.9.9 实体清单没有该表；redrive 本质是「以管理员身份发起的一次投递周期重置」，追加进 attempt 流最贴近其审计语义。
- **后果**：§5.9.11「人工操作必须保留原 work、attempt、provider receipt 和处理原因」逐字满足；admin 查询入口/UI 归后续 change，本 change 提供仓储 API 与测试。

### ADR-7 turns/tool_calls/background_work_items 的最小集与归属边界

- **决策**：
  - `turns`：状态枚举沿用现有 `ConversationRuntime` 终态集 `queued/in_progress/completed/interrupted/failed/cancelled`（§3.1 已冻结该状态集）；带 `expected_status` CAS 推进（与应用层 `transition_turn` 同语义）；`final_message_id` 指向 T2 写入的 canonical assistant message；`inbox_record_id` 唯一 nullable FK 把 queued turn 锚到接受事务。
  - `tool_calls`：只记 `running/succeeded/failed/cancelled/unknown` 终态流（§5.9.6「active tool 最终都有 terminal/unknown 状态」）；`tool_audit_events`/`tool_idempotency_keys` 归 C7，不在本表。
  - `background_work_items`：接受事务可随 T1 建 queued work（consolidation 等以 work id 归 C12）；`idempotency_key` 唯一 nullable（幂等 work 重算由 C3/C12 调度，本表只提供身份与状态）。
- **备选**：本 change 不建 turns/tool_calls/work 表，只做 inbox/outbox。**不选**：task-02「本任务拥有」清单明确包含三表；T1/T2 的原子性验收（queued turn 同事务、turn terminal 同事务）需要它们存在。
- **后果**：C3 的恢复扫描直接 `SELECT ... WHERE status NOT IN (终态)`（`ix_turns_tenant_status` 等索引已备）；C4 的 WebChat 闭环可直接以 `inbox_records.canonical_message_id` 补拉。

### ADR-8 表/约束命名（收敛 §10「Exact schema/DDL」OPEN 项）

| 对象 | 命名 |
| --- | --- |
| 表 | `inbox_records`、`message_deduplication_keys`、`turns`、`tool_calls`、`background_work_items`、`outbound_delivery_intents`、`delivery_attempts`（沿用 §5.9.9 逻辑实体名） |
| 部分唯一索引 | `uq_message_dedup_keys_source`、`uq_message_dedup_keys_client` |
| 唯一约束 | `uq_inbox_records_dedup_key_id`、`uq_background_work_items_idempotency_key`、`uq_outbound_delivery_intents_idempotency_key` |
| 外键 | `fk_inbox_records_*`、`fk_turns_*`、`fk_tool_calls_turn_id`、`fk_background_work_items_*`、`fk_outbound_delivery_intents_*`、`fk_delivery_attempts_intent_id`（全部 `ON DELETE RESTRICT`：历史 message/turn/attempt 不级联物理删除，§5.9.9） |
| CHECK | `ck_message_dedup_keys_key_present`、`ck_inbox_records_status`、`ck_turns_status`、`ck_tool_calls_status`、`ck_background_work_items_status`、`ck_outbound_delivery_intents_status`、`ck_delivery_attempts_outcome` |
| 索引 | `ix_inbox_records_conversation`、`ix_turns_tenant_status`、`ix_tool_calls_turn`、`ix_background_work_items_tenant_status`、`ix_outbound_delivery_intents_claim`（`(status, next_attempt_at)`）、`ix_delivery_attempts_intent` |

## 3. DDL（冻结形态）

```sql
CREATE TABLE message_deduplication_keys (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         VARCHAR(64)  NOT NULL,
    source_channel    VARCHAR(64)  NULL,
    source_identity_id VARCHAR(255) NULL,
    source_message_id VARCHAR(255) NULL,
    account_id        UUID         NULL,
    client_message_id VARCHAR(255) NULL,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_message_dedup_keys_key_present CHECK (
        (source_message_id IS NOT NULL AND source_channel IS NOT NULL AND source_identity_id IS NOT NULL)
        OR (client_message_id IS NOT NULL AND account_id IS NOT NULL))
);
CREATE UNIQUE INDEX uq_message_dedup_keys_source ON message_deduplication_keys (source_channel, source_identity_id, source_message_id)
    WHERE source_message_id IS NOT NULL;
CREATE UNIQUE INDEX uq_message_dedup_keys_client ON message_deduplication_keys (account_id, client_message_id)
    WHERE client_message_id IS NOT NULL;
ALTER TABLE message_deduplication_keys
    ADD CONSTRAINT fk_message_dedup_keys_account_id FOREIGN KEY (account_id)
        REFERENCES test_accounts (id) ON DELETE RESTRICT;

CREATE TABLE inbox_records (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           VARCHAR(64) NOT NULL,
    conversation_id     UUID        NOT NULL,
    dedup_key_id        UUID        NOT NULL,
    canonical_message_id UUID       NOT NULL,
    status              VARCHAR(32) NOT NULL DEFAULT 'accepted',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at        TIMESTAMPTZ NULL,
    CONSTRAINT uq_inbox_records_dedup_key_id UNIQUE (dedup_key_id),
    CONSTRAINT fk_inbox_records_conversation_id FOREIGN KEY (conversation_id)
        REFERENCES canonical_conversations (id) ON DELETE RESTRICT,
    CONSTRAINT fk_inbox_records_dedup_key_id FOREIGN KEY (dedup_key_id)
        REFERENCES message_deduplication_keys (id) ON DELETE RESTRICT,
    CONSTRAINT fk_inbox_records_canonical_message_id FOREIGN KEY (canonical_message_id)
        REFERENCES canonical_messages (id) ON DELETE RESTRICT,
    CONSTRAINT ck_inbox_records_status CHECK (status IN ('accepted', 'processed'))
);
CREATE INDEX ix_inbox_records_conversation ON inbox_records (tenant_id, conversation_id, created_at);

CREATE TABLE turns (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        VARCHAR(64) NOT NULL,
    conversation_id  UUID        NOT NULL,
    inbox_record_id  UUID        NULL,
    status           VARCHAR(32) NOT NULL DEFAULT 'queued',
    error_json       TEXT        NULL,
    final_message_id UUID        NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_turns_conversation_id FOREIGN KEY (conversation_id)
        REFERENCES canonical_conversations (id) ON DELETE RESTRICT,
    CONSTRAINT fk_turns_inbox_record_id FOREIGN KEY (inbox_record_id)
        REFERENCES inbox_records (id) ON DELETE RESTRICT,
    CONSTRAINT fk_turns_final_message_id FOREIGN KEY (final_message_id)
        REFERENCES canonical_messages (id) ON DELETE RESTRICT,
    CONSTRAINT ck_turns_status CHECK (
        status IN ('queued', 'in_progress', 'completed', 'interrupted', 'failed', 'cancelled'))
);
CREATE UNIQUE INDEX uq_turns_inbox_record_id ON turns (inbox_record_id) WHERE inbox_record_id IS NOT NULL;
CREATE INDEX ix_turns_tenant_status ON turns (tenant_id, status);

CREATE TABLE tool_calls (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    VARCHAR(64) NOT NULL,
    turn_id      UUID        NOT NULL,
    tool_name    VARCHAR(255) NOT NULL,
    status       VARCHAR(32) NOT NULL DEFAULT 'running',
    outcome_json TEXT        NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ NULL,
    CONSTRAINT fk_tool_calls_turn_id FOREIGN KEY (turn_id)
        REFERENCES turns (id) ON DELETE RESTRICT,
    CONSTRAINT ck_tool_calls_status CHECK (
        status IN ('running', 'succeeded', 'failed', 'cancelled', 'unknown'))
);
CREATE INDEX ix_tool_calls_turn ON tool_calls (turn_id, started_at);

CREATE TABLE background_work_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(64) NOT NULL,
    conversation_id UUID        NULL,
    work_kind       VARCHAR(64) NOT NULL,
    status          VARCHAR(32) NOT NULL DEFAULT 'queued',
    idempotency_key VARCHAR(255) NULL,
    payload_json    TEXT        NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ NULL,
    CONSTRAINT uq_background_work_items_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT fk_background_work_items_conversation_id FOREIGN KEY (conversation_id)
        REFERENCES canonical_conversations (id) ON DELETE RESTRICT,
    CONSTRAINT ck_background_work_items_status CHECK (
        status IN ('queued', 'in_progress', 'succeeded', 'failed', 'cancelled'))
);
CREATE INDEX ix_background_work_items_tenant_status ON background_work_items (tenant_id, status);

CREATE TABLE outbound_delivery_intents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(64)  NOT NULL,
    conversation_id UUID         NOT NULL,
    message_id      UUID         NOT NULL,
    turn_id         UUID         NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    channel         VARCHAR(64)  NOT NULL,
    target_chat_id  VARCHAR(255) NOT NULL,
    payload_json    TEXT         NULL,
    status          VARCHAR(32)  NOT NULL DEFAULT 'pending',
    attempt_count   INTEGER      NOT NULL DEFAULT 0,
    lease_owner     VARCHAR(128) NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    next_attempt_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_error      TEXT         NULL,
    sent_at         TIMESTAMPTZ  NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_outbound_delivery_intents_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT fk_outbound_delivery_intents_conversation_id FOREIGN KEY (conversation_id)
        REFERENCES canonical_conversations (id) ON DELETE RESTRICT,
    CONSTRAINT fk_outbound_delivery_intents_message_id FOREIGN KEY (message_id)
        REFERENCES canonical_messages (id) ON DELETE RESTRICT,
    CONSTRAINT fk_outbound_delivery_intents_turn_id FOREIGN KEY (turn_id)
        REFERENCES turns (id) ON DELETE RESTRICT,
    CONSTRAINT ck_outbound_delivery_intents_status CHECK (
        status IN ('pending', 'attempting', 'sent', 'failed', 'dead_letter'))
);
CREATE INDEX ix_outbound_delivery_intents_claim ON outbound_delivery_intents (status, next_attempt_at);

CREATE TABLE delivery_attempts (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    intent_id        UUID        NOT NULL,
    outcome          VARCHAR(32) NOT NULL,
    provider_receipt VARCHAR(255) NULL,
    error            TEXT        NULL,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ NULL,
    CONSTRAINT fk_delivery_attempts_intent_id FOREIGN KEY (intent_id)
        REFERENCES outbound_delivery_intents (id) ON DELETE RESTRICT,
    CONSTRAINT ck_delivery_attempts_outcome CHECK (outcome IN ('sent', 'failed', 'redrive'))
);
CREATE INDEX ix_delivery_attempts_intent ON delivery_attempts (intent_id, started_at);
```

> Migration seed：`not_applicable`（§5.9.9——dev tenant 无存量 work；`uq_turns_inbox_record_id` 为部分唯一索引，PG 无法以普通 UNIQUE 约束表达，故走 `CREATE UNIQUE INDEX`）。

## 4. Risks / Trade-offs

| 风险 | 缓解 |
| --- | --- |
| at-least-once 下「发送成功但 ack 前崩溃」会重复投递 | lease + 幂等键把窗口压缩到单次 attempt 内；§5.9.11 接受该残差（Pilot 单 worker 场景极小）；channel 侧自身幂等（WebChat `client_message_id` 幂等帧）兜底 |
| 部分唯一索引的 `ON CONFLICT` 目标必须精确匹配谓词 | 仓储层显式携带 `index_where`；迁移/约束测试断言索引存在（`pg_indexes`） |
| `attempt_count` 与 `delivery_attempts` 行数在 redrive 后不一致 | 有意为之：前者是「本周期」计数（redrive 复位），后者是全量审计流（只追加）；ADR-6 记录语义 |
| delivery worker 与既有 `MessageBus.dispatch_outbound` 并存造成双投递路径 | 本 change 不接线既有 dispatcher（Non-Goals）；worker 只被新 seam（C4）调用；静态契约测试确认不 import 单体 bus |
| turn CAS 状态推进与应用层 `SessionStore.transition_turn` 双存储 | C2 只落 PG durable 副本，不改 `ConversationRuntime` 行为（单体兼容）；cutover 在后续 change 按实现地图执行（§3.4「Turn control plane」行） |
| claim 索引 `(status, next_attempt_at)` 在大 backlog 下扫描效率 | Pilot 规模（10–30 账号）不构成瓶颈；`FOR UPDATE SKIP LOCKED` + LIMIT 批量已定；扩容归 Redis Streams 评估（§2 明确不做） |

## 5. Rollback 策略

- **migration 层**：`downgrade()` 删除七表（本 change 未 cutover、无运行时读取者，符合 §5.9.9「downgrade 只负责尚未 cutover 的新对象」）；无 backfill（`not_applicable`）。
- **运行层**：worker 停止即不再投递（intent 保留原状态，重启续跑）；关闭 Pilot 入口 + 保留 PG 数据（同 C1 ADR-6）。
- **演练**：Create→Verify→Enable→（入口关闭）→ 数据保留验证，步骤与输出存 `openspec/evidence/c2-durable-control-plane/rollback-drill.md`。
