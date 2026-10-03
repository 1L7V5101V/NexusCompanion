"""Retention 重放帧消费游标：webchat_replay_counters 增列 consumed_seq（p0-retention-wiring）

p0-retention-wiring ADR-8（expand-only）+ task 3.1 结论：重放帧「已确认消费」的
唯一语义点 = 客户端 `replay {after_seq}` 声明（「我已收到 ≤ after_seq」），此前
用后即弃。本迁移为计数器行增加 `consumed_seq BIGINT NULL`：

- durable replay 服务时 `consumed_seq = GREATEST(consumed_seq, LEAST(after_seq, 水位))`
  （bootstrap/webchat_durable.py 持久化写点；只进不退，客户端声明超前水位不推高）。
- NULL = 该会话从未有过客户端游标声明 → retention 的消费侧判据不生效，
  帧只受「每会话保留下限 + 兜底年龄天花板」约束。
- 计数器行不动语义不变：next_seq 水位只增不减；retention 只删帧行。

纯加列：downgrade 只 drop 该列，不动任何既有列。

Revision ID: d0a9b7c3e1f5
Revises: b8e2f4a6c0d2
Create Date: 2026-10-03
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d0a9b7c3e1f5"
down_revision: Union[str, Sequence[str], None] = "b8e2f4a6c0d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE webchat_replay_counters
            ADD COLUMN consumed_seq BIGINT NULL
        """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE webchat_replay_counters
            DROP COLUMN consumed_seq
        """)
