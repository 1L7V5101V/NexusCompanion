"""WebChat durable replay frames（pg-durable-sot-cutover）

`webchat_replay_counters` / `webchat_replay_frames` 两表
（openspec/changes/pg-durable-sot-cutover/design.md ADR-3）。

语义门禁：webchat-durable-storage spec「durable 重放与 REST 重建」——replayable
帧（message.accepted / turn.completed / turn.failed）的 wire seq 分配与帧记录
SHALL 随对应 durable 事务（T1/T2/终态收束）持久化，重启存续；delta/tool 帧不入表。
seq 由 per-conversation 计数器行在事务内 `UPDATE ... RETURNING` 分配（与 canonical
next_sequence 同模式，行锁天然串行化同会话写入）；帧 JSON 存已盖 seq 的完整
wire 帧，补拉 = 按游标 SELECT。retention/清理归 C12 §8.4（design Risks 登记）。

downgrade 删除两表是安全操作：帧记录是重放投影，canonical 流才是内容 source of
truth；删表后客户端按协议回 `replay_required` 由 REST 从 canonical 重建。

Revision ID: d8e4f2b6a9c1
Revises: b3f7a1c5d9e2
Create Date: 2026-09-29
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d8e4f2b6a9c1"
down_revision: Union[str, Sequence[str], None] = "b3f7a1c5d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE webchat_replay_counters (
            conversation_id UUID PRIMARY KEY,
            next_seq        BIGINT       NOT NULL DEFAULT 1,
            updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT fk_webchat_replay_counters_conversation_id
                FOREIGN KEY (conversation_id)
                REFERENCES canonical_conversations (id) ON DELETE RESTRICT
        )
        """)
    op.execute("""
        CREATE TABLE webchat_replay_frames (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       VARCHAR(64)  NOT NULL,
            conversation_id UUID         NOT NULL,
            seq             BIGINT       NOT NULL,
            frame_type      VARCHAR(32)  NOT NULL,
            frame_json      TEXT         NOT NULL,
            message_id      UUID         NULL,
            turn_id         UUID         NULL,
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_webchat_replay_frames_conv_seq
                UNIQUE (conversation_id, seq),
            CONSTRAINT ck_webchat_replay_frames_type CHECK (
                frame_type IN ('message.accepted', 'turn.completed', 'turn.failed')),
            CONSTRAINT fk_webchat_replay_frames_conversation_id
                FOREIGN KEY (conversation_id)
                REFERENCES canonical_conversations (id) ON DELETE RESTRICT,
            CONSTRAINT fk_webchat_replay_frames_message_id
                FOREIGN KEY (message_id)
                REFERENCES canonical_messages (id) ON DELETE RESTRICT,
            CONSTRAINT fk_webchat_replay_frames_turn_id
                FOREIGN KEY (turn_id)
                REFERENCES turns (id) ON DELETE RESTRICT
        )
        """)
    op.execute("""
        CREATE INDEX ix_webchat_replay_frames_tenant_conv
            ON webchat_replay_frames (tenant_id, conversation_id, seq)
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS webchat_replay_frames")
    op.execute("DROP TABLE IF EXISTS webchat_replay_counters")
