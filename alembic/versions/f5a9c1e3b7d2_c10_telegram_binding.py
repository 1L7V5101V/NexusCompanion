"""C10 Telegram binding：telegram_identity_bindings / telegram_binding_codes
两表（c10-telegram-binding-sync ADR-1）

- `telegram_identity_bindings`：Telegram 私聊身份 ↔ test_account 可信绑定。
  双重唯一由**部分唯一索引**在 active 状态上表达（§5.9.2：每账号最多一个
  Telegram 身份、同一 Telegram 身份最多一个账号）；unbound 行保留供审计且
  不阻塞重绑。tenant_id 为绑定时点冻结的目标 agent（账号 created_at 首个
  canonical 会话，与 resolve_webchat_identity 同规则）。
- `telegram_binding_codes`：一次性绑定码，digest-only（沿用 access_tokens
  约定：明文不落库、不进日志/审计）；10 分钟过期、单次使用（原子兑换见
  TelegramBindingService），码与签发账号（及目标 agent/tenant）预关联。

纯 expand-only：downgrade 只 drop 两表，不动任何既有表；canonical message
schema（C1）与 WebChat 协议（C4）零改动。

Revision ID: f5a9c1e3b7d2
Revises: e1f3a5c7b9d2
Create Date: 2026-10-06
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f5a9c1e3b7d2"
down_revision: Union[str, Sequence[str], None] = "e1f3a5c7b9d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE telegram_identity_bindings (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            account_id          UUID         NOT NULL,
            tenant_id           VARCHAR(64)  NOT NULL,
            telegram_user_id    VARCHAR(64)  NOT NULL,
            telegram_chat_id    VARCHAR(64)  NOT NULL,
            status              VARCHAR(16)  NOT NULL DEFAULT 'active',
            bound_via           VARCHAR(16)  NOT NULL,
            bound_by            VARCHAR(64)  NOT NULL DEFAULT '',
            note                VARCHAR(255) NOT NULL DEFAULT '',
            bound_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
            unbound_at          TIMESTAMPTZ  NULL,
            unbound_by          VARCHAR(64)  NOT NULL DEFAULT '',
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT fk_tib_account_id
                FOREIGN KEY (account_id)
                REFERENCES test_accounts (id) ON DELETE RESTRICT,
            CONSTRAINT ck_tib_status CHECK (status IN ('active', 'unbound')),
            CONSTRAINT ck_tib_bound_via CHECK (bound_via IN ('admin', 'code'))
        )
        """)
    op.execute("""
        CREATE UNIQUE INDEX uq_tib_account_active
            ON telegram_identity_bindings (account_id)
            WHERE status = 'active'
        """)
    op.execute("""
        CREATE UNIQUE INDEX uq_tib_identity_active
            ON telegram_identity_bindings (telegram_user_id)
            WHERE status = 'active'
        """)
    op.execute("""
        CREATE INDEX ix_tib_account_history
            ON telegram_identity_bindings (account_id, created_at)
        """)
    op.execute("""
        CREATE TABLE telegram_binding_codes (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            account_id          UUID         NOT NULL,
            tenant_id           VARCHAR(64)  NOT NULL,
            code_digest         VARCHAR(64)  NOT NULL,
            digest_version      SMALLINT     NOT NULL DEFAULT 1,
            issued_by           VARCHAR(64)  NOT NULL DEFAULT '',
            note                VARCHAR(255) NOT NULL DEFAULT '',
            expires_at          TIMESTAMPTZ  NOT NULL,
            consumed_at         TIMESTAMPTZ  NULL,
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT fk_tbc_account_id
                FOREIGN KEY (account_id)
                REFERENCES test_accounts (id) ON DELETE RESTRICT,
            CONSTRAINT uq_tbc_digest UNIQUE (code_digest),
            CONSTRAINT ck_tbc_digest_sha256
                CHECK (char_length(code_digest) = 64)
        )
        """)
    op.execute("""
        CREATE INDEX ix_tbc_account_open
            ON telegram_binding_codes (account_id, created_at)
            WHERE consumed_at IS NULL
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS telegram_binding_codes")
    op.execute("DROP TABLE IF EXISTS telegram_identity_bindings")
