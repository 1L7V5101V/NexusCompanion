"""C7 tool_audit_events — tool call 审计流（追加型，§5.8.6 统一 schema）

(openspec/changes/c7-tool-isolation/design.md ADR-6)：

- 新表 `tool_audit_events`：一行 = 一次工具调用的收束记录（终态时 INSERT，
  含执行前拒绝 `rejected`），字段对齐 §5.8.6 统一 schema；
- `tool_calls`（C2）保持 turn-bound 终态流不动——其 docstring 已声明
  「audit/idempotency 键表归 C7，不在本表」；本表 `tool_call_id` 为**软引用**
  （无 FK）：审计流 SHALL NOT 因终态流的生命周期而丢行；
- `effect_class` 不加 CHECK（七级枚举可能随 capability 演进，漂移风险由
  代码常量 + C12 schema 校验承担，与 C15 `flow` 同哲学）；
- `status` 加 CHECK：('succeeded','failed','cancelled','unknown','rejected')——
  前四项与 C2 `tool_calls` 同枚举，`rejected` 为本表新增（执行前拒绝，无终态流行）；
- 索引：`(tenant_id, created_at)` 租户下钻、`(account_id, created_at)` 账号审计。

语义门禁：PILOT_ROADMAP §5.9.9，本迁移**只做 expand**（新表，零回填、零改列），
与写入接线可分开发布。SQLite 单机模式不建此表（单机无 auth/无多租户审计面），
结构化日志兜底（design ADR-6 附录）。

seed：not_applicable。

rollback：`DROP TABLE tool_audit_events`（追加型表，无数据需要撤销）。

Revision ID: b3f7a1c5d9e2
Revises: a7f2c9e4b1d8
Create Date: 2026-09-27
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b3f7a1c5d9e2"
down_revision: Union[str, Sequence[str], None] = "a7f2c9e4b1d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tool_audit_events (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            request_id         UUID         NULL,
            account_id         UUID         NULL,
            tenant_id          VARCHAR(64)  NOT NULL,
            session_id         VARCHAR(128) NULL,
            turn_id            UUID         NULL,
            tool_call_id       UUID         NULL,
            tool_binding_id    VARCHAR(128) NULL,
            tool_name          VARCHAR(255) NOT NULL,
            effect_class       VARCHAR(32)  NOT NULL,
            status             VARCHAR(32)  NOT NULL,
            arguments_redacted TEXT         NULL,
            arguments_hash     VARCHAR(64)  NULL,
            duration_ms        INTEGER      NULL,
            error_code         VARCHAR(64)  NULL,
            created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT ck_tool_audit_events_status CHECK (
                status IN ('succeeded', 'failed', 'cancelled', 'unknown', 'rejected'))
        )
        """)
    op.execute("""
        CREATE INDEX ix_tool_audit_events_tenant
            ON tool_audit_events (tenant_id, created_at)
        """)
    op.execute("""
        CREATE INDEX ix_tool_audit_events_account
            ON tool_audit_events (account_id, created_at)
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tool_audit_events")
