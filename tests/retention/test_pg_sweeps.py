"""PG 行级 sweeper 验收（tasks 3.2/3.3/3.4/3.5 + 7.1 报告分类）。

覆盖 spec「数据实体的保留档归属与不可删除集」「裁剪按时间分批且幂等，
不破坏单调水位」「演练模式零变更」「报告区分可恢复与不可恢复的删除量」。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.config_models import RetentionConfig
from bootstrap.retention import RetentionSweeper
from tests.retention.conftest import DEV_TENANT_ID

pytestmark = pytest.mark.postgres


@pytest.fixture
def sweeper(rt_factory: async_sessionmaker) -> RetentionSweeper:
    return RetentionSweeper(rt_factory, RetentionConfig())


def _make_audit_fixture(
    exec_sql: Callable[..., Any]
) -> Callable[..., None]:
    """造 N 条过期 + M 条新鲜的 tool_audit_events 行。"""

    def _seed(expired: int, fresh: int) -> None:
        for i in range(expired):
            exec_sql(
                """
                INSERT INTO tool_audit_events
                    (tenant_id, tool_name, effect_class, status, arguments_redacted, created_at)
                VALUES ('dev', %s, 'read', 'succeeded', %s, now() - interval '200 days')
                """,
                (f"tool_old_{i}", "x" * 128),
            )
        for i in range(fresh):
            exec_sql(
                """
                INSERT INTO tool_audit_events
                    (tenant_id, tool_name, effect_class, status, arguments_redacted, created_at)
                VALUES ('dev', %s, 'read', 'succeeded', %s, now())
                """,
                (f"tool_new_{i}", "y" * 64),
            )

    return _seed


def test_audit_window_keeps_fresh_deletes_expired(
    rt_reset: None, sweeper: RetentionSweeper, exec_sql: Callable[..., Any]
) -> None:
    _make_audit_fixture(exec_sql)(expired=3, fresh=2)

    report = asyncio_run(sweeper.run_once(dry_run=False))

    tool = next(e for e in report.entities if e.target == "tool_audit_events")
    assert tool.deleted == 3
    assert tool.kept == 2
    assert tool.scanned == 5
    assert tool.recoverability == "irrecoverable"
    remaining = exec_sql("SELECT count(*) FROM tool_audit_events")[0][0]
    assert remaining == 2


def test_audit_100_day_rows_kept_under_180d_window(
    rt_reset: None, sweeper: RetentionSweeper, exec_sql: Callable[..., Any]
) -> None:
    """spec「审计类保留期长于运行类」：100 天前的审计行未满 180d，保留。"""
    exec_sql(
        """
        INSERT INTO tool_audit_events
            (tenant_id, tool_name, effect_class, status, created_at)
        VALUES ('dev', 'mid_age_tool', 'read', 'succeeded', now() - interval '100 days')
        """
    )

    report = asyncio_run(sweeper.run_once(dry_run=False))

    tool = next(e for e in report.entities if e.target == "tool_audit_events")
    assert tool.deleted == 0
    assert exec_sql("SELECT count(*) FROM tool_audit_events")[0][0] == 1


def test_audit_sweep_idempotent_second_round_zero(
    rt_reset: None, sweeper: RetentionSweeper, exec_sql: Callable[..., Any]
) -> None:
    _make_audit_fixture(exec_sql)(expired=4, fresh=0)

    first = asyncio_run(sweeper.run_once(dry_run=False))
    second = asyncio_run(sweeper.run_once(dry_run=False))

    tool_first = next(e for e in first.entities if e.target == "tool_audit_events")
    tool_second = next(e for e in second.entities if e.target == "tool_audit_events")
    assert tool_first.deleted == 4
    assert tool_second.deleted == 0
    # 幂等：两轮报告字段一致可比（spec「连续两轮执行幂等」）。
    assert tool_second.scanned == tool_first.kept


def test_audit_batch_cap_converges_over_rounds(
    rt_reset: None, rt_factory: async_sessionmaker, exec_sql: Callable[..., Any]
) -> None:
    """spec「单轮删除有上限」：超量数据单轮不越界（batch×max_batches），后续轮收敛。"""
    cfg = RetentionConfig(batch_size=5, max_batches=2)
    sweeper = RetentionSweeper(rt_factory, cfg)
    for i in range(13):
        exec_sql(
            """
            INSERT INTO tool_audit_events
                (tenant_id, tool_name, effect_class, status, created_at)
            VALUES ('dev', %s, 'read', 'succeeded', now() - interval '200 days')
            """,
            (f"bulk_{i}",),
        )

    first = asyncio_run(sweeper.run_once(dry_run=False))
    tool = next(e for e in first.entities if e.target == "tool_audit_events")
    assert tool.deleted == 10  # batch_size * max_batches

    second = asyncio_run(sweeper.run_once(dry_run=False))
    tool2 = next(e for e in second.entities if e.target == "tool_audit_events")
    assert tool2.deleted == 3  # 剩余收敛

    third = asyncio_run(sweeper.run_once(dry_run=False))
    tool3 = next(e for e in third.entities if e.target == "tool_audit_events")
    assert tool3.deleted == 0


def test_dry_run_reports_without_mutating_and_matches_live(
    rt_reset: None, sweeper: RetentionSweeper, exec_sql: Callable[..., Any]
) -> None:
    """spec「演练后数据原样存在」+「随后实删轮次的删除数与演练报告一致」。"""
    _make_audit_fixture(exec_sql)(expired=6, fresh=1)

    dry = asyncio_run(sweeper.run_once(dry_run=True))
    tool_dry = next(e for e in dry.entities if e.target == "tool_audit_events")
    assert tool_dry.deleted == 6
    assert tool_dry.dry_run is True
    assert tool_dry.bytes_freed > 0
    # 演练零变更：所有数据对象仍完整存在。
    assert exec_sql("SELECT count(*) FROM tool_audit_events")[0][0] == 7

    live = asyncio_run(sweeper.run_once(dry_run=False))
    tool_live = next(e for e in live.entities if e.target == "tool_audit_events")
    assert tool_live.deleted == tool_dry.deleted


def test_admin_audit_and_work_attempts_sweep(
    rt_reset: None, sweeper: RetentionSweeper, exec_sql: Callable[..., Any]
) -> None:
    """admin 审计与 work_attempts 归 audit 180d（owner 定案，ADR-2）。"""
    exec_sql(
        """
        INSERT INTO admin_audit_events (actor, action, detail, created_at)
        VALUES ('admin-cli', 'revoke', '{"k": 1}', now() - interval '200 days')
        """
    )
    exec_sql(
        """
        INSERT INTO admin_audit_events (actor, action, detail, created_at)
        VALUES ('admin-cli', 'bootstrap', NULL, now())
        """
    )
    item_id = str(uuid.uuid4())
    exec_sql(
        """
        INSERT INTO background_work_items
            (id, tenant_id, work_kind, status, next_attempt_at)
        VALUES (%s, 'dev', 'maintenance', 'succeeded', now())
        """,
        (item_id,),
    )
    exec_sql(
        """
        INSERT INTO work_attempts (work_item_id, outcome, error, started_at)
        VALUES (%s, 'succeeded', NULL, now() - interval '200 days')
        """,
        (item_id,),
    )

    report = asyncio_run(sweeper.run_once(dry_run=False))

    admin = next(e for e in report.entities if e.target == "admin_audit_events")
    attempts = next(e for e in report.entities if e.target == "work_attempts")
    assert admin.deleted == 1
    assert attempts.deleted == 1
    assert exec_sql("SELECT count(*) FROM admin_audit_events")[0][0] == 1
    assert exec_sql("SELECT count(*) FROM work_attempts")[0][0] == 0
    # background_work_items 本体不动（ADR-2 不可删除集）。
    assert exec_sql("SELECT count(*) FROM background_work_items")[0][0] == 1


def test_no_delete_set_survives_full_round(
    rt_reset: None,
    sweeper: RetentionSweeper,
    rt_factory: async_sessionmaker,
    make_tenant: Any,
    exec_sql: Callable[..., Any],
) -> None:
    """spec「死信投递意图不被删除」「业务消息不在保留期执行范围内」。

    经真实 ingress 造 canonical message + inbox + turn（dedupe 同事务），
    另造远超任何窗口的死信投递意图，全部 backdate 到 400 天前，跑完整一轮，
    断言行数不变。
    """
    from bootstrap.db.repository.control_plane_repo import IngressRepository

    tenant = asyncio_run(make_tenant(prefix="nodelete"))
    ingress = IngressRepository(rt_factory)
    await_(
        ingress.accept_inbound(
            tenant["tenant_id"],
            tenant["conversation_id"],
            account_id=tenant["account_id"],
            client_message_id="nodelete-cmid-1",
            content="业务正文",
            replay_frame=None,
        )
    )
    message_id = exec_sql(
        "SELECT id FROM canonical_messages WHERE tenant_id = %s", (tenant["tenant_id"],)
    )[0][0]
    exec_sql(
        """
        INSERT INTO outbound_delivery_intents
            (id, tenant_id, conversation_id, message_id, idempotency_key, channel,
             target_chat_id, payload_json, status, attempt_count, next_attempt_at,
             created_at, updated_at)
        VALUES (%s, %s, %s, %s, 'nodelete-idem-1', 'telegram', 'dead-target', '{}',
                'dead_letter', 5, now(), now(), now())
        """,
        (str(uuid.uuid4()), tenant["tenant_id"], tenant["conversation_id"], str(message_id)),
    )
    # backdate：所有业务行推到 400 天前（远超任何保留窗口）。
    for table in ("canonical_messages", "inbox_records", "turns",
                  "canonical_conversations", "outbound_delivery_intents"):
        exec_sql(f"UPDATE {table} SET created_at = now() - interval '400 days'")

    before = {
        table: exec_sql(f"SELECT count(*) FROM {table}")[0][0]
        for table in (
            "outbound_delivery_intents",
            "canonical_messages",
            "canonical_conversations",
            "inbox_records",
            "turns",
        )
    }

    report = asyncio_run(sweeper.run_once(dry_run=False))

    after = {
        table: exec_sql(f"SELECT count(*) FROM {table}")[0][0]
        for table in (
            "outbound_delivery_intents",
            "canonical_messages",
            "canonical_conversations",
            "inbox_records",
            "turns",
        )
    }
    assert after == before  # 不可删除集：一轮完整执行后行数不变
    # 死信意图不出现在任何删除报告中。
    assert all(e.target != "outbound_delivery_intents" for e in report.entities)


def _seed_frames(
    exec_sql: Callable[..., Any],
    conversation_id: str,
    count: int,
    *,
    days_ago: int = 0,
    consumed_seq: int | None = None,
    keep_counter: bool = True,
) -> None:
    if keep_counter:
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
            VALUES ('dev', %s, %s, 'message.accepted', %s,
                    now() - (%s * interval '1 day'))
            """,
            (conversation_id, seq, json.dumps({"seq": seq, "type": "message.accepted"}), days_ago),
        )
    if consumed_seq is not None:
        exec_sql(
            """
            UPDATE webchat_replay_counters SET consumed_seq = %s
            WHERE conversation_id = %s
            """,
            (consumed_seq, conversation_id),
        )


def test_replay_deletion_does_not_regress_seq_watermark(
    rt_reset: None, sweeper: RetentionSweeper, rt_factory: async_sessionmaker,
    exec_sql: Callable[..., Any],
) -> None:
    """spec「删除过期帧不回退序号水位」：counter.next_seq 与 current_seq 不动。"""
    from bootstrap.db.repository.control_plane_repo import WebchatReplayRepository

    conv_id = "00000000-0000-0000-0000-000000000002"
    _seed_frames(exec_sql, conv_id, count=40, days_ago=60, consumed_seq=40)
    replay = WebchatReplayRepository(rt_factory)
    before_seq = await_(replay.current_seq(DEV_TENANT_ID, conv_id))

    report = asyncio_run(sweeper.run_once(dry_run=False))

    entity = next(e for e in report.entities if e.target == "webchat_replay_frames")
    assert entity.deleted > 0
    after_seq = await_(replay.current_seq(DEV_TENANT_ID, conv_id))
    assert after_seq == before_seq == 40  # 水位只增不减
    counter_next = exec_sql(
        "SELECT next_seq FROM webchat_replay_counters WHERE conversation_id = %s",
        (conv_id,),
    )[0][0]
    assert counter_next == 41
    # 保留下限：最近 20 帧仍在（keep_last=20 默认）。
    assert entity.deleted == 20


def test_report_classifies_recoverable_vs_irrecoverable(
    rt_reset: None, sweeper: RetentionSweeper, exec_sql: Callable[..., Any]
) -> None:
    """spec「报告区分可恢复与不可恢复的删除量」：补发缓冲 vs 审计流水分别计量。"""
    conv_id = "00000000-0000-0000-0000-000000000002"
    _seed_frames(exec_sql, conv_id, count=40, days_ago=60, consumed_seq=40)
    exec_sql(
        """
        INSERT INTO tool_audit_events
            (tenant_id, tool_name, effect_class, status, created_at)
        VALUES ('dev', 't', 'read', 'succeeded', now() - interval '200 days')
        """
    )

    report = asyncio_run(sweeper.run_once(dry_run=False))

    assert report.recoverable_deleted == 20
    assert report.irrecoverable_deleted >= 1
    # 汇总数与分类数一致（ADR-7）。
    total = sum(e.deleted for e in report.entities)
    assert report.recoverable_deleted + report.irrecoverable_deleted == total
    # 补发缓冲归 recoverable，审计归 irrecoverable。
    frames = next(e for e in report.entities if e.target == "webchat_replay_frames")
    assert frames.recoverability == "recoverable"
    tool = next(e for e in report.entities if e.target == "tool_audit_events")
    assert tool.recoverability == "irrecoverable"


def test_report_free_text_errors_redacted(
    rt_reset: None, rt_factory: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec「报告不落敏感内容」：错误串经 redact_text，不落原始内容。"""
    cfg = RetentionConfig()
    sweeper = RetentionSweeper(rt_factory, cfg)

    async def _boom(*args: Any, **kwargs: Any) -> tuple[int, int]:
        raise RuntimeError(
            "delete failed: password=hunter2 session_digest=ab12 path=/tmp/secret/x"
        )

    monkeypatch.setattr(sweeper._tool_audit, "delete_expired_audit_batch", _boom)
    report = asyncio_run(sweeper.run_once(dry_run=False))
    tool = next(e for e in report.entities if e.target == "tool_audit_events")
    assert tool.errors
    # 错误串经 redact_text：本地路径与密码值脱敏，原始片段不落报告。
    joined = chr(10).join(tool.errors)
    assert "/tmp/secret/x" not in joined
    assert "[REDACTED:local_path]" in joined
    assert "hunter2" not in joined


def asyncio_run(awaitable: Any) -> Any:
    import asyncio

    return asyncio.run(awaitable)


def await_(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
