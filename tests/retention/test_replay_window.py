"""重放帧按会话补发窗口裁剪（tasks 3.6 / ADR-8）——spec 四 Scenario 逐一对应。

- 未消费的旧帧不因年龄被删（未超兜底天花板时）
- 已消费的旧帧可被裁剪（超保留下限的部分）
- 长期离线会话受兜底上限约束（超龄未消费帧被裁、报告如实计入、读侧判定重建）
- 每个会话保留帧数有下限（全消费 + 全超龄仍留最近 N 帧）
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.config_models import RetentionConfig
from bootstrap.db.repository.control_plane_repo import WebchatReplayRepository
from bootstrap.retention import RetentionSweeper
from tests.retention.conftest import DEV_CONVERSATION_ID, DEV_TENANT_ID

pytestmark = pytest.mark.postgres


@pytest.fixture
def replay(rt_factory: async_sessionmaker) -> WebchatReplayRepository:
    return WebchatReplayRepository(rt_factory)


@pytest.fixture
def sweeper(rt_factory: async_sessionmaker) -> RetentionSweeper:
    return RetentionSweeper(rt_factory, RetentionConfig())


def _seed(
    exec_sql: Callable[..., Any],
    conversation_id: str,
    count: int,
    *,
    days_ago: int = 0,
    consumed_seq: int | None = None,
    tenant_id: str = DEV_TENANT_ID,
) -> None:
    exec_sql(
        """
        INSERT INTO webchat_replay_counters (conversation_id, next_seq, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (conversation_id) DO NOTHING
        """,
        (conversation_id, count + 1),
    )
    for seq in range(1, count + 1):
        exec_sql(
            """
            INSERT INTO webchat_replay_frames
                (tenant_id, conversation_id, seq, frame_type, frame_json, created_at)
            VALUES (%s, %s, %s, 'message.accepted', %s, now() - (%s * interval '1 day'))
            """,
            (
                tenant_id,
                conversation_id,
                seq,
                json.dumps({"seq": seq, "type": "message.accepted"}),
                days_ago,
            ),
        )
    if consumed_seq is not None:
        exec_sql(
            "UPDATE webchat_replay_counters SET consumed_seq = %s WHERE conversation_id = %s",
            (consumed_seq, conversation_id),
        )


async def test_unconsumed_frames_within_ceiling_not_deleted(
    rt_reset: None,
    sweeper: RetentionSweeper,
    replay: WebchatReplayRepository,
    exec_sql: Callable[..., Any],
) -> None:
    """Scenario「未消费的旧帧不因年龄被删」：帧在兜底天花板窗口内、无消费声明 → 保留。"""
    _seed(exec_sql, DEV_CONVERSATION_ID, count=5, days_ago=5)  # 5d < 30d 天花板

    report = await sweeper.run_once(dry_run=False)

    entity = next(e for e in report.entities if e.target == "webchat_replay_frames")
    assert entity.deleted == 0
    frames = await replay.frames_after(DEV_TENANT_ID, DEV_CONVERSATION_ID, 0)
    assert [f["seq"] for f in frames] == [1, 2, 3, 4, 5]


async def test_consumed_old_frames_trimmed_beyond_floor(
    rt_reset: None,
    sweeper: RetentionSweeper,
    replay: WebchatReplayRepository,
    exec_sql: Callable[..., Any],
) -> None:
    """Scenario「已消费的旧帧可被裁剪」：游标已越过、超过最近 N 帧下限的部分被裁。"""
    _seed(exec_sql, DEV_CONVERSATION_ID, count=40, days_ago=1, consumed_seq=40)

    report = await sweeper.run_once(dry_run=False)

    entity = next(e for e in report.entities if e.target == "webchat_replay_frames")
    assert entity.deleted == 20  # seq 1..20（≤游标 且 超出最近 20 帧下限）
    frames = await replay.frames_after(DEV_TENANT_ID, DEV_CONVERSATION_ID, 0)
    assert [f["seq"] for f in frames] == list(range(21, 41))
    # 重连补发能力不受影响：游标 20 处重连可拿到连续的 21..40。
    tail = await replay.frames_after(DEV_TENANT_ID, DEV_CONVERSATION_ID, 20)
    assert [f["seq"] for f in tail] == list(range(21, 41))


async def test_long_offline_session_hits_ceiling_and_reports(
    rt_reset: None,
    sweeper: RetentionSweeper,
    replay: WebchatReplayRepository,
    exec_sql: Callable[..., Any],
) -> None:
    """Scenario「长期离线会话受兜底上限约束」：未消费帧超龄部分被裁并如实计入。

    读侧据水位差判定需要客户端重建（replay_required 语义），不静默补发不完整历史。
    """
    _seed(exec_sql, DEV_CONVERSATION_ID, count=40, days_ago=60, consumed_seq=None)
    # 客户端从未声明消费（consumed_seq NULL）→ 消费侧判据不生效；
    # 全部帧 created_at 早于 30d 天花板 → 除最近 20 帧下限外全部可裁。

    report = await sweeper.run_once(dry_run=False)

    entity = next(e for e in report.entities if e.target == "webchat_replay_frames")
    assert entity.deleted == 20
    assert entity.bytes_freed > 0
    frames = await replay.frames_after(DEV_TENANT_ID, DEV_CONVERSATION_ID, 0)
    assert [f["seq"] for f in frames] == list(range(21, 41))
    # 读侧：游标 0 的客户端补拉请求落在窗口之外（oldest=21 > cursor+1）→ replay_after
    # 返回 None → 调用方回 replay_required（对照 tests/auth_provisioning/
    # test_webchat_rebuild_reconcile.py 的重建语义），不静默补发不完整历史。
    oldest = await replay.oldest_seq(DEV_TENANT_ID, DEV_CONVERSATION_ID)
    assert oldest == 21
    # after_seq=0 < oldest-1=20 → 窗口外 → 需客户端重建。
    assert 0 < int(oldest) - 1


async def test_floor_keeps_last_n_when_all_consumed_and_old(
    rt_reset: None,
    sweeper: RetentionSweeper,
    replay: WebchatReplayRepository,
    exec_sql: Callable[..., Any],
) -> None:
    """Scenario「每个会话保留帧数有下限」：全消费 + 全超龄，仍保留最近 N 帧。"""
    _seed(exec_sql, DEV_CONVERSATION_ID, count=25, days_ago=90, consumed_seq=25)

    report = await sweeper.run_once(dry_run=False)

    entity = next(e for e in report.entities if e.target == "webchat_replay_frames")
    assert entity.deleted == 5  # 25 - keep_last(20)
    frames = await replay.frames_after(DEV_TENANT_ID, DEV_CONVERSATION_ID, 0)
    assert [f["seq"] for f in frames] == list(range(6, 26))


async def test_consumed_cursor_greatest_and_capped_by_watermark(
    rt_reset: None,
    replay: WebchatReplayRepository,
    exec_sql: Callable[..., Any],
) -> None:
    """消费游标持久化语义（task 3.1）：GREATEST 只进不退；声明超前水位不推高。"""
    _seed(exec_sql, DEV_CONVERSATION_ID, count=10, days_ago=0, consumed_seq=None)

    await replay.record_consumed_cursor(DEV_TENANT_ID, DEV_CONVERSATION_ID, 5)
    assert await replay.consumed_seq(DEV_TENANT_ID, DEV_CONVERSATION_ID) == 5
    # 回退声明不生效。
    await replay.record_consumed_cursor(DEV_TENANT_ID, DEV_CONVERSATION_ID, 2)
    assert await replay.consumed_seq(DEV_TENANT_ID, DEV_CONVERSATION_ID) == 5
    # 超前声明被水位封顶（水位 10）。
    await replay.record_consumed_cursor(DEV_TENANT_ID, DEV_CONVERSATION_ID, 99)
    assert await replay.consumed_seq(DEV_TENANT_ID, DEV_CONVERSATION_ID) == 10

    # 空会话（无计数器行）：记录为 no-op，不创建行、不抛错。
    other = str(uuid.uuid4())
    await replay.record_consumed_cursor(DEV_TENANT_ID, other, 3)
    assert await replay.consumed_seq(DEV_TENANT_ID, other) == 0


async def test_window_isolation_between_conversations(
    rt_reset: None,
    sweeper: RetentionSweeper,
    replay: WebchatReplayRepository,
    make_tenant: Callable[..., Awaitable[dict[str, Any]]],
    exec_sql: Callable[..., Any],
) -> None:
    """裁剪判据按会话自身进度：A 会话全消费可裁，B 会话未消费不动。"""
    tenant_b = await make_tenant(prefix="rtwin")
    _seed(exec_sql, DEV_CONVERSATION_ID, count=40, days_ago=1, consumed_seq=40)
    _seed(
        exec_sql,
        tenant_b["conversation_id"],
        count=40,
        days_ago=1,
        consumed_seq=None,
        tenant_id=tenant_b["tenant_id"],
    )

    report = await sweeper.run_once(dry_run=False)

    entity = next(e for e in report.entities if e.target == "webchat_replay_frames")
    assert entity.deleted == 20  # 只裁 A；B 的未消费帧保留
    frames_a = await replay.frames_after(DEV_TENANT_ID, DEV_CONVERSATION_ID, 0)
    frames_b = await replay.frames_after(
        tenant_b["tenant_id"], tenant_b["conversation_id"], 0
    )
    assert len(frames_a) == 20
    assert len(frames_b) == 40
