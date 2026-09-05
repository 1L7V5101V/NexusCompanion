# C1 canonical identity — 设计冻结（P-1）

> 门禁依据：PILOT_ROADMAP §5.9.2（canonical identity 语义）、§5.9.9（数据模型/首次启用）、§10 DECIDED（旧单体数据边界、DB rollout/rollback）、§3.4 实现地图。
> 本文档把 §10「OPEN FOR P-1 SPEC — Exact schema/DDL」项收敛为可执行 DDL 与约束命名。

## 1. 身份派生链（对应 §5.9.2 冻结语义）

派生链唯一合法形态：

```text
可信 principal（已通过 auth session / Telegram source identity 校验）
  → test_accounts（1 : 1）
  → tenant_id（资源边界，1 : 1）
  → canonical_conversations（1 : 1，每个 tenant 恰有一个规范会话）
```

冻结结论（§5.9.2 逐条落地）：

| §5.9.2 冻结语义 | 本 change 落地 |
| --- | --- |
| 服务端身份映射是可信查表，不是客户端传 tenant_id | resolver 只接受**服务端已验证**的 principal 输入（C1 阶段 = 已知 account_id / tenant_id；C5/C10 供上游验证后调用）；客户端 tenant/chat/session 字段永远只作为待校验输入，不进入 resolver 授权路径 |
| 无 binding 的请求必须拒绝，不能落 `DEFAULT_TENANT` | `resolve_*` 查不到账号/会话 → 抛 `IdentityResolutionError`（fail-closed）；`bootstrap/identity.py` 不 import `DEFAULT_TENANT`（由静态契约测试强制） |
| 一个 test_account ↔ 一个 tenant_id ↔ 一个 canonical_conversation | `test_accounts.tenant_id` 唯一、`canonical_conversations.tenant_id` 唯一、conversation.account_id 外键 1:1（DB 级保证，见 §3 约束） |
| 每条入站消息含服务端生成的 canonical `message_id`，保留 source/client 标识 | `canonical_messages.id` = 服务端 UUID；`source_channel` / `source_identity_id` / `source_message_id` / `client_message_id` 落列（**唯一性去重归 C2 inbox/dedupe**，本表只保存原文标识，见 Non-Goals） |
| Telegram binding 是不可信输入、绑定表归 C10 | 本 change 不建 binding 表；resolver 的上游（C5 auth session、C10 telegram binding）就绪后以「已验证 principal」身份调用本 resolver，C1 契约不变 |
| 旧单体数据不迁入、不 fallback、不双写、不反向同步 | 本 change 无任何 SQLite 读写路径；静态契约测试 grep canonical 模块无 sqlite 引用（证据 `openspec/evidence/c1-canonical-identity/grep-no-sqlite-fallback.txt`） |
| canonical conversation 从空历史开始 | migration 只建表 + dev seed（见 ADR-4）；无任何从旧库复制数据的 SQL |

## 2. ADR 记录

### ADR-1 身份解析 = 可信查表 repository，不是 `channel:chat_id` 函数

- **决策**：新增 `bootstrap/identity.py::CanonicalIdentityResolver`，输入为已验证 principal 标识（`account_id` 或 `tenant_id`），输出不可变 `CanonicalIdentity`（`account_id`/`tenant_id`/`conversation_id` 三元组）。
- **备选 1**：扩展 `infra/storage/tenancy.py::tenant_id_for_channel()` 让它查库。**不选**：该函数是 C3 的临时 admission key（§3.4、task-01 共享 seam 协议），本轮改动会拖入 admission 语义；且 `channel:chat_id` 不是 canonical identity（§5.9.1 硬冲突）。
- **备选 2**：客户端直接提交 `canonical_conversation_id`。**不选**：违反「服务端查表」冻结结论，等于把归属权交给攻击者可修改字段。
- **后果**：C2/C4/C5/C10 接线时，在各自 adapter 完成 principal 验证后调用 resolver；`session_key` 派生路径保持现状（单体兼容）。

### ADR-2 sequence 分配 = conversation 行上计数器 + 单事务 `UPDATE ... RETURNING`

- **决策**：`canonical_conversations.next_sequence BIGINT NOT NULL DEFAULT 0`；`CanonicalMessageRepository.append_message()` 在**同一个数据库事务**内先 `UPDATE canonical_conversations SET next_sequence = next_sequence + 1 WHERE id = :id AND tenant_id = :tenant RETURNING next_sequence - 1` 取号，再 INSERT `canonical_messages`，提交后返回。
- **语义**：0-based（首个消息 sequence = 0）；per-conversation 独立（B 会话不受 A 计数影响）；BIGINT；同一事务原子分配 + 写入。取号语句的行锁天然把同一 conversation 的并发 append 串行化，跨 conversation 互不阻塞。UPDATE 零行（会话不存在/tenant 不符）→ 抛错回滚，不落任何行。
- **备选 1**：沿用单体 `SessionStore.next_seq` 的「SELECT 读号 → 应用加一 → UPDATE 回写 → 单独提交」。**不选**：§5.9.2 明文禁止（两步操作在并发下会重号）。
- **备选 2**：每会话一个 PG `CREATE SEQUENCE`。**不选**：DDL per conversation 与 provisioning 事务耦合，且 `currval/nextval` 无法与会话行的事务性回滚一致（sequence 取号不随事务回滚，回滚会产生空洞）；行计数器随事务回滚，保证 sequence 严格连续无空洞，利于断线补拉游标语义。
- **备选 3**：`INSERT ... ON CONFLICT (conversation_id, sequence) DO NOTHING` 应用侧重试。**不选**：把唯一性当作分配算法，高并发下重试风暴，且无法保证连续。
- **后果**：并发正确性由「行锁 + 同事务 + UNIQUE(conversation_id, sequence)」三重保证；并发测试证明唯一、连续、0-based、跨会话独立。

### ADR-3 主键与 ID 生成

- **决策**：三表主键均为 `UUID`，由服务端/DB 生成（`gen_random_uuid()` server default，PG 13+ 内置）；repository 层可显式传 id 便于测试与幂等 upsert。
- **备选**：沿用单体 `String(64)` 应用生成 id。**不选**：§5.9.1「表约束与序号」要求 Pilot DB design 冻结主键策略；canonical identity 是新模型，直接采用 PG 原生 UUID，避免继续承担 `f"{session_key}:{seq}"` 复合字符串的历史包袱。

### ADR-4 seed = 仅 dev 单用户规范身份（显式 `dev` tenant）

- **决策**：migration seed 插入一行 `test_accounts`（`tenant_id='dev'`、`status='active'`、`display_name='Pilot Dev Account'`）与其唯一 `canonical_conversations`，供 P0.5 dev WebChat 闭环与本任务测试在 C5 provisioning 存在之前使用。**不使用 `default` tenant**：canonical 链对 `DEFAULT_TENANT` 的态度是「禁止隐式回退」，seed 显式命名为 `dev`，与单体的显式 single-user 兼容语义区分。
- **备选**：不 seed（`not_applicable`）。**不选**：P0.5 需要一个先于 provisioning 的可用规范身份；§5.9.13「不由第一个用户 turn 隐式触发 provisioning」也要求 dev 环境有预置身份。
- **边界**：生产受邀账号一律由 C5 provisioning 流程创建（复用 `create_account_with_conversation`，事务幂等），不从 seed 复制。

### ADR-5 账号/会话状态机最小集（§5.9.9 冻结枚举）

- **决策**：`test_accounts.status` / `canonical_conversations.status` 均为 `String(32)` + CHECK 约束。账号枚举：`provisioning / active / suspended / revoked`（§5.9.9 冻结）；会话枚举：`active / archived`（C1 只需区分可用与归档；`archived` 为未来 retention 语义预留，不做任何行为）。
- **后果**：C5 接手 provisioning readiness 时直接使用账号枚举；`revoked` 终态语义（不删历史、终止新 work）在 C5/C7 落地，C1 不实现行为。

### ADR-6 首次启用 = Create → Verify → Enable；rollback = 关入口 + 保留 PG 数据

- **决策**（§5.9.9 / §10 DECIDED 原样落地）：
  1. **Create**：`alembic upgrade head` 在空 PG 创建三表 + 约束 + seed；不连接旧 SQLite、不复制旧数据。
  2. **Verify**：空库基线断言（表/约束/seed 存在、默认行数正确）+ 并发 sequence + 唯一约束 + 负向拒绝测试（`pytest tests/canonical_identity/`，测试数据与正式 test account 分离——专用 scratch DB `nexus_c1test`）。
  3. **Enable**：通过单一 feature/config flag 开放 Pilot provisioning（C5）与入口；C1 自身无入口开关，仅提供 schema 与 seam。
- **Rollback**：关闭 Pilot 入口 + 停止新 provisioning，**保留已产生的 PG 数据**用于修复后继续；不反向同步 SQLite，不把旧单体库当回滚目标；业务数据恢复用 PG backup/PITR。演练记录见 `openspec/evidence/c1-canonical-identity/rollback-drill.md`。
- **schema evolution**：上线后按 Expand → Backfill → Verify → Cutover → Compatibility → Contract；C1 首版不涉及 backfill（`not_applicable`）。

### ADR-7 表/约束命名（收敛 §10「Exact schema/DDL」OPEN 项）

| 对象 | 命名 |
| --- | --- |
| 表 | `test_accounts`、`canonical_conversations`、`canonical_messages`（沿用 §5.9.9 逻辑实体名） |
| 唯一约束 | `uq_test_accounts_tenant_id`、`uq_canonical_conversations_tenant_id`、`uq_canonical_messages_conversation_sequence` |
| 外键 | `fk_canonical_conversations_account_id`（→ `test_accounts.id`）、`fk_canonical_messages_conversation_id`（→ `canonical_conversations.id`） |
| CHECK | `ck_test_accounts_status`、`ck_canonical_conversations_status`、`ck_canonical_messages_role` |
| 索引 | `ix_canonical_messages_tenant_conversation`（`(tenant_id, conversation_id, sequence)`，服务按 tenant 过滤 + 游标拉取） |
| FK 删除策略 | `ON DELETE RESTRICT`（历史 message/turn/audit 不级联物理删除，§5.9.9） |

## 3. DDL（冻结形态）

```sql
CREATE TABLE test_accounts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   VARCHAR(64)  NOT NULL,
    status      VARCHAR(32)  NOT NULL DEFAULT 'provisioning',
    display_name VARCHAR(255) NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_test_accounts_tenant_id UNIQUE (tenant_id),
    CONSTRAINT ck_test_accounts_status CHECK (
        status IN ('provisioning', 'active', 'suspended', 'revoked'))
);

CREATE TABLE canonical_conversations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     VARCHAR(64)  NOT NULL,
    account_id    UUID         NOT NULL,
    status        VARCHAR(32)  NOT NULL DEFAULT 'active',
    next_sequence BIGINT       NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_canonical_conversations_tenant_id UNIQUE (tenant_id),
    CONSTRAINT fk_canonical_conversations_account_id
        FOREIGN KEY (account_id) REFERENCES test_accounts (id) ON DELETE RESTRICT,
    CONSTRAINT ck_canonical_conversations_status CHECK (status IN ('active', 'archived'))
);

CREATE TABLE canonical_messages (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          VARCHAR(64)  NOT NULL,
    conversation_id    UUID         NOT NULL,
    sequence           BIGINT       NOT NULL,
    role               VARCHAR(32)  NOT NULL,
    content            TEXT         NULL,
    source_channel     VARCHAR(64)  NULL,
    source_identity_id VARCHAR(255) NULL,
    source_message_id  VARCHAR(255) NULL,
    client_message_id  VARCHAR(255) NULL,
    metadata_json      TEXT         NULL,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_canonical_messages_conversation_sequence
        UNIQUE (conversation_id, sequence),
    CONSTRAINT fk_canonical_messages_conversation_id
        FOREIGN KEY (conversation_id) REFERENCES canonical_conversations (id) ON DELETE RESTRICT,
    CONSTRAINT ck_canonical_messages_role CHECK (role IN ('user', 'assistant', 'system', 'tool'))
);

CREATE INDEX ix_canonical_messages_tenant_conversation
    ON canonical_messages (tenant_id, conversation_id, sequence);
```

> 注意：`sequence` 列名是 SQL 语境下的非保留字（`SEQUENCE` 非保留），可直接使用；SQLAlchemy 侧统一 `quote` 防御。

## 4. Risks / Trade-offs

| 风险 | 缓解 |
| --- | --- |
| 行计数器把同一会话的并发 append 串行化，高写入会话吞吐受限 | 与 §5.9.5「tenant 内单 active work 串行」一致，Pilot 规模（10–30 账号）不构成瓶颈；证据记录并发测试吞吐 |
| `next_sequence` 行更新 + 消息插入同事务，事务时长增加 | 两条语句、无跨库调用；压测归 C2/C3 |
| seed 的 `dev` 账号误用于生产 | tenant_id 显式 `dev`，非 `default`；C5 provisioning 不会复用该行（tenant 唯一约束天然隔离）；文档与 evidence 双重标注 |
| resolver 输入接口过窄（当前只支持 account/tenant 直查） | 有意为之：C1 只冻结「查表」seam；Telegram binding / auth session 入口分别在 C10/C5 的 change 中接入，不预建半成品接口 |
| 旧 `channel:chat_id` 路径与 canonical 路径并存造成双身份 | 按 §3.4 保留单体兼容；`tenant_id_for_channel()` 仍是 C3 admission key；切换在后续 change 按「实现地图行」执行，本 change 不碰 |

## 5. Rollback 策略

- **migration 层**：`downgrade()` 删除三表（C1 未 cutover、无人读取，符合 §5.9.9「downgrade 只负责尚未 cutover 的新对象」）；`sequence` 计数器列随表删除。
- **运行层**：关闭 Pilot 入口 + 停 provisioning，保留 PG 数据；不反向同步 SQLite（ADR-6）。
- **演练**：Create→Verify→Enable→（入口关闭）→ 数据保留验证，步骤与输出存 `openspec/evidence/c1-canonical-identity/rollback-drill.md`。
