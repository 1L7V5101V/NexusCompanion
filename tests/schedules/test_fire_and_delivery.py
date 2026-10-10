"""6.3 幂等 + 6.9 投递链路：一次 occurrence 只一份副作用，副作用经 C2 outbox。

断言的是「收束事务写了哪四样」：canonical assistant message（含 scheduler metadata）、
durable `turn.completed` 重放帧（WebChat 投递读它）、pending intent
（幂等键 `sched:<execution_id>`）、execution/job 终态；以及重复认领同一 occurrence
时这些计数都不变。
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bootstrap.db.repository.schedule_repo import ScheduleRepository

pytestmark = pytest.mark.postgres


async def test_instant_fire_writes_message_frame_intent_and_terminal(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    exec_sql,
) -> None:
    tenant = await make_tenant()
    job = await make_job(
        tenant, fire_at=clock.now + timedelta(seconds=1), message="该吃药了"
    )
    service = build_service()

    clock.advance(2)
    await run_ticks(service)

    executions = await repo.list_executions(job_id=job["id"])
    assert len(executions) == 1
    execution = executions[0]
    assert execution["status"] == "succeeded"
    assert execution["skip_reason"] is None
    # 触发时的时区/计划版本快照（§5.9.14）。
    assert execution["schedule_timezone"] == "UTC"
    assert execution["schedule_revision"] == 1
    assert execution["attempt_count"] == 1

    messages = exec_sql(
        "SELECT role, content, metadata_json FROM canonical_messages ORDER BY sequence"
    )
    assert len(messages) == 1
    role, content, metadata = messages[0]
    assert (role, content) == ("assistant", "该吃药了")
    assert json.loads(metadata)["source"] == "scheduler"
    assert json.loads(metadata)["execution_id"] == execution["id"]

    intents = exec_sql(
        "SELECT idempotency_key, channel, target_chat_id, status, turn_id, message_id"
        " FROM outbound_delivery_intents"
    )
    assert len(intents) == 1
    key, channel, target, status, turn_id, message_id = intents[0]
    assert key == f"sched:{execution['id']}"
    assert (channel, target, status) == ("chat", str(tenant["tenant_id"]), "pending")
    # schedule 输出不属于任何用户 turn。
    assert turn_id is None
    assert str(execution["delivery_message_id"]) == str(message_id)
    assert str(execution["delivery_intent_id"])  # 非空

    # WebchatDeliveryAdapter 从 durable 重放帧取内容；缺帧会退避到死信，
    # 所以收束事务必须同事务写 turn.completed 帧（ADR-3 的投递前置）。
    frames = exec_sql(
        "SELECT frame_type, frame_json, message_id FROM webchat_replay_frames"
    )
    assert len(frames) == 1
    frame_type, frame_json, frame_message = frames[0]
    assert frame_type == "turn.completed"
    assert json.loads(frame_json)["content"] == "该吃药了"
    assert json.loads(frame_json)["turn_id"] == f"sched:{execution['id']}"
    assert str(frame_message) == str(message_id)

    refreshed = await repo.get_job(job["id"])
    assert refreshed["last_outcome"] == "succeeded"
    # one-shot 收束后无待执行 occurrence。
    assert refreshed["fire_at"] is None


async def test_soft_fire_generates_content_via_agent_loop(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    exec_sql,
) -> None:
    """soft tier：内容来自 `process_direct`，且禁用自我推送/记忆写入工具。"""
    tenant = await make_tenant()
    loop = SimpleNamespace(process_direct=AsyncMock(return_value="AI 生成的日程摘要"))
    job = await make_job(
        tenant,
        tier="soft",
        prompt="总结今天的日程",
        message=None,
        fire_at=clock.now + timedelta(seconds=1),
    )
    service = build_service(agent_loop_provider=lambda: loop)

    clock.advance(2)
    await run_ticks(service)

    assert (await repo.list_executions(job_id=job["id"]))[0]["status"] == "succeeded"
    assert exec_sql("SELECT content FROM canonical_messages")[0][0] == "AI 生成的日程摘要"
    kwargs = loop.process_direct.call_args.kwargs
    assert kwargs["session_key"] == f"scheduler:{job['id']}"
    assert kwargs["omit_user_turn"] is True
    assert "message_push" in kwargs["disabled_tools"]
    # soft 的耗时样本进入 P90 追踪（预触发提前量的来源）。
    assert service.tracker._samples


@pytest.mark.parametrize("empty", ["", "   "])
async def test_soft_empty_ai_response_fails_without_delivery(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    exec_sql,
    empty: str,
) -> None:
    """AI 返回空内容 → execution `failed`，不落 message/intent（ADR-3）。"""
    tenant = await make_tenant()
    loop = SimpleNamespace(process_direct=AsyncMock(return_value=empty))
    job = await make_job(
        tenant, tier="soft", prompt="p", fire_at=clock.now + timedelta(seconds=1)
    )
    service = build_service(agent_loop_provider=lambda: loop)

    clock.advance(2)
    await run_ticks(service)

    execution = (await repo.list_executions(job_id=job["id"]))[0]
    assert execution["status"] == "failed"
    assert execution["error"] == "empty_ai_response"
    assert exec_sql("SELECT count(*) FROM canonical_messages")[0][0] == 0
    assert exec_sql("SELECT count(*) FROM outbound_delivery_intents")[0][0] == 0
    assert (await repo.get_job(job["id"]))["last_outcome"] == "failed"


async def test_duplicate_claim_of_same_occurrence_yields_one_side_effect(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    exec_sql,
) -> None:
    """把 `next_scheduled_for` 拨回同一 occurrence 再认领：唯一键使副作用只发生一次。"""
    tenant = await make_tenant()
    occurrence = clock.now + timedelta(seconds=1)
    job = await make_job(tenant, fire_at=occurrence, message="幂等")
    service = build_service()

    clock.advance(2)
    await run_ticks(service)
    assert exec_sql("SELECT count(*) FROM outbound_delivery_intents")[0][0] == 1

    # 模拟崩溃重放/并发 tick：调度态被拨回同一名义时刻。
    exec_sql(
        "UPDATE scheduled_jobs SET next_scheduled_for = %s WHERE id = %s",
        (occurrence, job["id"]),
    )
    claimed = await repo.claim_due_jobs(
        clock.now + timedelta(seconds=5), advance=lambda _job, _after: None
    )
    assert claimed == []
    assert len(await repo.list_executions(job_id=job["id"])) == 1
    assert exec_sql("SELECT count(*) FROM outbound_delivery_intents")[0][0] == 1
    assert exec_sql("SELECT count(*) FROM canonical_messages")[0][0] == 1


async def test_delivery_failure_does_not_rewrite_execution_terminal(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    exec_sql,
) -> None:
    """投递是 execution 终态之后的独立状态机：intent 失败退回不影响 execution。"""
    tenant = await make_tenant()
    job = await make_job(tenant, fire_at=clock.now + timedelta(seconds=1))
    service = build_service()
    clock.advance(2)
    await run_ticks(service)
    execution = (await repo.list_executions(job_id=job["id"]))[0]

    # 投递层自己的失败推进（模拟 worker 记 attempt 失败）。
    exec_sql(
        "UPDATE outbound_delivery_intents SET status='failed', attempt_count=1,"
        " last_error='provider 503' WHERE idempotency_key = %s",
        (f"sched:{execution['id']}",),
    )

    assert (await repo.list_executions(job_id=job["id"]))[0]["status"] == "succeeded"
    assert (await repo.get_job(job["id"]))["last_outcome"] == "succeeded"


async def test_two_jobs_of_same_tenant_fire_independently(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    exec_sql,
) -> None:
    """同租户两个 job 各自一条 intent（幂等键互不冲突）。"""
    tenant = await make_tenant()
    first = await make_job(tenant, message="A", fire_at=clock.now + timedelta(seconds=1))
    second = await make_job(
        tenant, message="B", fire_at=clock.now + timedelta(seconds=1), name="第二个"
    )
    service = build_service()
    clock.advance(2)
    await run_ticks(service)

    assert exec_sql("SELECT count(*) FROM outbound_delivery_intents")[0][0] == 2
    assert len(await repo.list_executions(job_id=first["id"])) == 1
    assert len(await repo.list_executions(job_id=second["id"])) == 1
    assert (await repo.get_job(first["id"]))["last_outcome"] == "succeeded"
    assert (await repo.get_job(second["id"]))["last_outcome"] == "succeeded"
