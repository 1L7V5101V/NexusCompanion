"""C6 attachment model ↔ migration 一致性契约。

验证 ORM model（bootstrap/db/models/attachment.py）与 alembic revision
e6f1a3b5c7d9 声明的表结构一致：列集合、索引、CHECK/UNIQUE 约束名。
"""

from __future__ import annotations

import re

from bootstrap.db.models.attachment import (
    ATTACHMENT_STATUSES,
    AttachmentModel,
    MessageAttachmentModel,
)

_MIGRATION_PATH = "alembic/versions/e6f1a3b5c7d9_c6_attachment_media.py"


def _migration_sql() -> str:
    with open(_MIGRATION_PATH, encoding="utf-8") as fh:
        return fh.read()


def _create_block(sql: str, table: str) -> str:
    start = sql.index(f"CREATE TABLE {table}")
    end = sql.index('"""', start)
    return sql[start:end]


def _declared_columns(block: str) -> set[str]:
    return {
        m.group(1)
        for m in re.finditer(
            r"^\s+(\w+)\s+(UUID|BIGINT|VARCHAR|TIMESTAMPTZ|INTEGER|CHAR)(?!\\s*\()",
            block,
            re.M,
        )
    }


def test_attachment_model_columns_match_migration() -> None:
    sql = _migration_sql()
    model_cols = set(AttachmentModel.__table__.columns.keys())
    assert "attachments" in sql
    # migration 中显式声明的列从 CREATE TABLE 块抽取
    block = _create_block(sql, "attachments")
    declared = _declared_columns(block)
    assert model_cols == declared, f"model/migration 列不一致: {model_cols ^ declared}"


def test_message_attachment_model_columns_match_migration() -> None:
    sql = _migration_sql()
    model_cols = set(MessageAttachmentModel.__table__.columns.keys())
    block = _create_block(sql, "message_attachments")
    declared = _declared_columns(block)
    assert model_cols == declared, f"model/migration 列不一致: {model_cols ^ declared}"


def test_attachment_status_enum_matches_migration() -> None:
    assert "(" + ", ".join(f"'{s}'" for s in ATTACHMENT_STATUSES) + ")" in _migration_sql()


def test_storage_key_unique_declared() -> None:
    assert "uq_attachments_storage_key" in _migration_sql()
    for constraint in AttachmentModel.__table__.constraints:
        if constraint.name == "uq_attachments_storage_key":
            cols = [c.name for c in constraint.columns]
            assert cols == ["storage_key"]
            break
    else:
        raise AssertionError("model 缺 uq_attachments_storage_key")