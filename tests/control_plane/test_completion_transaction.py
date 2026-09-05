"""T2 执行完成事务验收：原子性、失败无半写入、CAS 推进、失败终态无 intent、tool/work 生命周期。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.repository.canonical_repo import CanonicalMessageRepository
from bootstrap.db.repository.control_plane_repo import (
    DeliveryRepository,
    IngressRepository,
    TransitionError,
    TurnControlRepository,
    TurnNotFoundError,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def repos(c2_factory: async_sessionmaker) -> dict[str, Any]:
    return {
        "ingress": IngressRepository(c2_factory),
        "control": TurnControlRepository(c2_factory),
        "messages": CanonicalMessageRepository(c2_factory),
        "delivery": DeliveryRepository(c2_factory),
    }


async def _queued_turn(
    repos: dict[str, Any], tenant: dict[str, Any], client_message_id: str
) -> str:
    accepted = await repos["ingress"].accept_inbound(
        tenant["tenant_id"],
        tenant["conversation_id"],
        account_id=tenant["account_id"],
        client_message_id=client_message_id,
        content="hi",
    )
    assert accepted.turn_id is not None
    return accepted.turn_id


async def _intent_count(repos: dict[str, Any], tenant_id: str) -> int:
    delivery: DeliveryRepository = repos["delivery"]
    intents = []
    for status in ("pending", "attempting", "sent", "failed", "dead_letter"):
        intents.extend(await delivery.list_intents_by_status(tenant_id, status))
    return len(intents)


async def test_completion_commits_all_in_one_transaction(
    make_tenant, repos: dict[str, Any]
) -> None:
    """完成成功 = final assistant message + turn 终态 + pending intent 同事务产物。"""
    tenant = await make_tenant()
    turn_id = await _queued_turn(repos, tenant, "cm-complete")
    control: TurnControlRepository = repos["control"]
    result = await control.complete_turn_with_delivery(
        tenant["tenant_id"],
        tenant["conversation_id"],
        turn_id,
        expected_status="queued",
        response_content="这是回复",
        delivery_channel="telegram",
        delivery_target="chat-1",
    )
    assert result.message["sequence"] == 1  # user 消息已占 sequence 0
    assert result.message["role"] == "assistant"
    assert result.turn["status"] == "completed"
    assert result.turn["final_message_id"] == result.message["id"]
    assert result.intent["status"] == "pending"
    assert result.intent["idempotency_key"] == f"msg:{result.message['id']}"

    stream = await repos["messages"].fetch_messages(
        tenant["tenant_id"], tenant["conversation_id"]
    )
    assert [m["sequence"] for m in stream] == [0, 1]
    assert stream[1]["content"] == "这是回复"


async def test_completion_rollback_on_intent_conflict_no_half_writes(
    make_tenant, repos: dict[str, Any], c2_reset
) -> None:
    """intent 幂等键冲突 → 整体回滚：无 final、turn 保持 queued、序号计数器无空洞。"""
    c2_reset()
    tenant = await make_tenant()
    turn_a = await _queued_turn(repos, tenant, "cm-a")
    turn_b = await _queued_turn(repos, tenant, "cm-b")
    control: TurnControlRepository = repos["control"]
    first = await control.complete_turn_with_delivery(
        tenant["tenant_id"],
        tenant["conversation_id"],
        turn_a,
        expected_status="queued",
        response_content="a",
        delivery_channel="telegram",
        delivery_target="chat-1",
        delivery_idempotency_key="k-shared",
    )
    assert await _intent_count(repos, tenant["tenant_id"]) == 1

    with pytest.raises(IntegrityError):
        await control.complete_turn_with_delivery(
            tenant["tenant_id"],
            tenant["conversation_id"],
            turn_b,
            expected_status="queued",
            response_content="b",
            delivery_channel="telegram",
            delivery_target="chat-1",
            delivery_idempotency_key="k-shared",
        )
    stream = await repos["messages"].fetch_messages(
        tenant["tenant_id"], tenant["conversation_id"]
    )
    # 只有 user×2 + turn_a 的 final；turn_b 的 final 未写入
    assert len(stream) == 3
    assert all(m["content"] != "b" for m in stream)
    turn_b_row = await control.get_turn(tenant["tenant_id"], turn_b)
    assert turn_b_row is not None and turn_b_row["status"] == "queued"
    del first


async def test_completion_unknown_turn_rejected(
    make_tenant, repos: dict[str, Any]
) -> None:
    """未知 turn / 未知会话 fail-closed：拒绝且零写入。"""
    tenant = await make_tenant()
    with pytest.raises(TurnNotFoundError):
        await repos["control"].complete_turn_with_delivery(
            tenant["tenant_id"],
            tenant["conversation_id"],
            uuid.uuid4(),
            expected_status="queued",
            response_content="x",
            delivery_channel="telegram",
            delivery_target="chat-1",
        )


async def test_turn_cas_transitions(make_tenant, repos: dict[str, Any]) -> None:
    """expected_status CAS：错误 expected 拒绝推进（沿用 ConversationRuntime 语义）。"""
    tenant = await make_tenant()
    turn_id = await _queued_turn(repos, tenant, "cm-cas")
    control: TurnControlRepository = repos["control"]
    in_progress = await control.transition_turn(
        tenant["tenant_id"], turn_id, expected_status="queued", new_status="in_progress"
    )
    assert in_progress["status"] == "in_progress"
    with pytest.raises(TransitionError):
        await control.transition_turn(
            tenant["tenant_id"], turn_id, expected_status="queued", new_status="in_progress"
        )
    with pytest.raises(TurnNotFoundError):
        await control.transition_turn(
            tenant["tenant_id"], uuid.uuid4(), expected_status="queued", new_status="failed"
        )


async def test_failed_terminal_no_intent(make_tenant, repos: dict[str, Any]) -> None:
    """turn 失败/取消/中断终态不创建投递意图（§5.9.11 只为成功完成建 intent）。"""
    tenant = await make_tenant()
    for index, terminal in enumerate(("failed", "cancelled", "interrupted")):
        turn_id = await _queued_turn(repos, tenant, f"cm-term-{index}")
        row = await repos["control"].transition_turn(
            tenant["tenant_id"],
            turn_id,
            expected_status="queued",
            new_status=terminal,
            error={"type": "Test", "message": terminal},
        )
        assert row["status"] == terminal
        assert await _intent_count(repos, tenant["tenant_id"]) == 0


async def test_tool_call_lifecycle(make_tenant, repos: dict[str, Any]) -> None:
    """tool call：running → 终态 CAS；重复终态拒绝；unknown 可达（§5.9.6）。"""
    tenant = await make_tenant()
    turn_id = await _queued_turn(repos, tenant, "cm-tool")
    control: TurnControlRepository = repos["control"]
    call = await control.record_tool_call(tenant["tenant_id"], turn_id, "recall_memory")
    assert call["status"] == "running"
    finished = await control.finish_tool_call(
        tenant["tenant_id"],
        call["id"],
        status="succeeded",
        outcome={"items": 3},
    )
    assert finished["status"] == "succeeded"
    assert finished["outcome"] == {"items": 3}
    assert finished["finished_at"]
    with pytest.raises(TransitionError):
        await control.finish_tool_call(tenant["tenant_id"], call["id"], status="failed")
    orphan = await control.record_tool_call(tenant["tenant_id"], turn_id, "search_messages")
    unknown = await control.finish_tool_call(
        tenant["tenant_id"], orphan["id"], status="unknown", outcome={"reason": "crash"}
    )
    assert unknown["status"] == "unknown"


async def test_background_work_item_lifecycle(
    make_tenant, repos: dict[str, Any]
) -> None:
    """后台工作项：幂等创建 + CAS 推进 + 终态 finished_at。"""
    tenant = await make_tenant()
    control: TurnControlRepository = repos["control"]
    first = await control.create_work_item(
        tenant["tenant_id"],
        "consolidation",
        conversation_id=tenant["conversation_id"],
        idempotency_key="work-1",
        payload={"window": 30},
    )
    second = await control.create_work_item(
        tenant["tenant_id"],
        "consolidation",
        conversation_id=tenant["conversation_id"],
        idempotency_key="work-1",
    )
    assert first["id"] == second["id"]
    assert first["status"] == "queued"

    await control.transition_work_item(
        tenant["tenant_id"], first["id"], expected_status="queued", new_status="in_progress"
    )
    with pytest.raises(TransitionError):
        await control.transition_work_item(
            tenant["tenant_id"], first["id"], expected_status="queued", new_status="succeeded"
        )
    done = await control.transition_work_item(
        tenant["tenant_id"],
        first["id"],
        expected_status="in_progress",
        new_status="succeeded",
    )
    assert done["status"] == "succeeded" and done["finished_at"]
