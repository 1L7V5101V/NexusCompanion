"""C1 account→N tenant（account-multi-tenant 扩展）

放宽 canonical identity 模型：一个账号（登录主体）可拥有多个 tenant，每个
tenant 对应恰好一个 canonical_conversation（一个 agent/资源域）。1:1 的唯一来源
是 `test_accounts.tenant_id` 列及其 `uq_test_accounts_tenant_id` 唯一约束，去掉它
即可让 tenant 只挂在会话行上（`canonical_conversations.tenant_id` 本就全局唯一，
天然支持一账号多行）。本 migration 即移除该账号列。

语义门禁：openspec/changes/2026-09-05-c1-account-multi-tenant/（主 spec 同步前，
1:1 措辞以已验证 C1 spec 为准）。C1 未 cutover（仅 dev seed + scratch DB），
升级删列无损；降级见 :func:`downgrade` 的边界说明。

Revision ID: c4d8f2a6e9b3
Revises: e2b4d6f8a0c2
Create Date: 2026-09-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d8f2a6e9b3"
down_revision: Union[str, Sequence[str], None] = "e2b4d6f8a0c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_test_accounts_tenant_id", "test_accounts", type_="unique")
    op.drop_column("test_accounts", "tenant_id")


def downgrade() -> None:
    # 一步降回 e2b4d6f8a0c2（1:1 账号↔tenant）只对「每账号恰好一条会话」的数据
    # 可行；多 agent 账号无损失无法表示，fail-closed 抛错（不回填猜值）。全量
    # rollback 的下一步即 C1 删三张表，本步忠实度边界在预 cutover dev 数据上足够。
    conn = op.get_bind()
    bad = conn.execute(
        sa.text(
            "SELECT count(*) FROM test_accounts a "
            "LEFT JOIN canonical_conversations c ON c.account_id = a.id "
            "GROUP BY a.id HAVING count(c.id) <> 1"
        )
    ).fetchall()
    if bad:
        raise RuntimeError(
            "downgrade 无法重建 1:1 账号↔tenant：存在会话数不为 1 的账号；"
            "account→N tenant 为预 cutover 的单向扩展，需人工合并后再降级"
        )
    op.add_column(
        "test_accounts", sa.Column("tenant_id", sa.String(length=64), nullable=True)
    )
    op.execute(
        "UPDATE test_accounts a SET tenant_id = c.tenant_id "
        "FROM canonical_conversations c WHERE c.account_id = a.id"
    )
    op.alter_column("test_accounts", "tenant_id", nullable=False)
    op.create_unique_constraint(
        "uq_test_accounts_tenant_id", "test_accounts", ["tenant_id"]
    )
