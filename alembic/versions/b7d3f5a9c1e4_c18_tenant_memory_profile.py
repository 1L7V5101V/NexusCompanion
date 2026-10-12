"""C18 记忆画像层：tenant_memory_profile_sections / tenant_memory_profile_entries
两张画像专表（c18-tenant-scoped-memory ADR-13）

画像层另立专表，与记忆召回（`memory_items`）彻底分表。两条理由：

1. 召回侧的类型过滤不是结构性的——`_resolve_memory_types` 只在 `filters.kinds`
   非空或 `intent == "procedure"` 时返回类型白名单，其余意图不加过滤，而
   `memory_items.memory_type` 是无 CHECK 约束的 String(64)。画像行与之共表时，
   一次普通召回就能把长期画像整段当检索结果返回。分表后画像表不出现在任何
   召回 SQL 里。
2. 单行整块类与追加日志类的物理需求相反：前者每租户恒定三行、频繁整行覆写；
   后者只追加、按时间倒序读、体量随对话增长。共表会让两套访问模式共用一份
   索引策略。

两张表按行形状分：

- `tenant_memory_profile_sections`：单行整块，`UNIQUE (tenant_id, category)` 即
  「每租户每类只一行」的 schema 表达，写入为 upsert。三类
  `long_term` / `recent_context` / `relationship_state`。CHECK 白名单把
  relationship_state 也收进来，是因为它属于画像存储层——它的**访问路径**仍归
  persona 那条缝（ADR-9），组装侧只经 persona 快照读它，不直查本表。
- `tenant_memory_profile_entries`：追加日志，`happened_at NOT NULL`。三类
  `pending` / `history` / `journal`。append-only 所以没有 `updated_at`。
  `pending` 的消费是一条 `DELETE ... RETURNING content`，取走与删除同事务，
  回滚即事务中止——文件后端那套「原子重命名 + 崩溃恢复」在此无从表达也不需要。

两个刻意的取舍：

- `dedup_key` 用 TEXT 而非定长：迁移的幂等去重键里嵌消息 ID 的 JSON 数组
  （id 形如 `<session_key>:<seq>`），三条以上消息就超过 128 字符。原计划借用
  `app_configs` 承接它，而那张表主键只有 `key String(128)` 一列、`tenant_id`
  非主键——既有长度上限又跨租户全局（租户 B 会被租户 A 的同名键静默判重）。
  本表 `UNIQUE (tenant_id, dedup_key)` 同时修掉这两点。
- 唯一约束写成**无条件**的表级约束而不是 `WHERE dedup_key IS NOT NULL` 的偏索引：
  PG 与 SQLite 里 UNIQUE 约束都把 NULL 视为互不相等，语义已经正确（NULL 即
  「不参与去重」，可重复插入），而偏索引语法会增加跨后端结构一致性测试的比对面。

两张表都带 `tenant_id`（SQLite 侧同样带，ADR-10），所以跨租户隔离断言在两种后端
上都可执行——`memory_items` 在 SQLite 侧没有该列，单机「隔离」只等于「只有一个
租户」。本表**不分区**，因此 `infra/storage/provisioning.py` 的每租户分区 DDL
不涉及这两张表，控制面零改动。

纯 expand-only：downgrade 只 drop 两张表，不动 `memory_items` 与 persona 三表；
`memory_items` 里遗留的 `memory_type='self'` 旧行不清理（另开 change，proposal
Non-Goals）。

Revision ID: b7d3f5a9c1e4
Revises: e8b4c2a6d9f1
Create Date: 2026-10-12
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b7d3f5a9c1e4"
down_revision: Union[str, Sequence[str], None] = "e8b4c2a6d9f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tenant_memory_profile_sections (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           VARCHAR(64)  NOT NULL,
            category            VARCHAR(32)  NOT NULL,
            content             TEXT         NOT NULL,
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_tenant_memory_profile_sections_tenant_category
                UNIQUE (tenant_id, category),
            CONSTRAINT ck_tenant_memory_profile_sections_category
                CHECK (category IN ('long_term', 'recent_context',
                                    'relationship_state'))
        )
        """)
    op.execute("""
        CREATE TABLE tenant_memory_profile_entries (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           VARCHAR(64)  NOT NULL,
            category            VARCHAR(32)  NOT NULL,
            content             TEXT         NOT NULL,
            happened_at         TIMESTAMPTZ  NOT NULL,
            dedup_key           TEXT         NULL,
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT ck_tenant_memory_profile_entries_category
                CHECK (category IN ('pending', 'history', 'journal')),
            CONSTRAINT uq_tenant_memory_profile_entries_dedup
                UNIQUE (tenant_id, dedup_key)
        )
        """)
    op.execute("""
        CREATE INDEX ix_tenant_memory_profile_entries_time
            ON tenant_memory_profile_entries
               (tenant_id, category, happened_at DESC)
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_memory_profile_entries")
    op.execute("DROP TABLE IF EXISTS tenant_memory_profile_sections")
