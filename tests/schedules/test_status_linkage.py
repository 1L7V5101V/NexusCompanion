"""6.6 状态联动：账号 suspended/revoked、Telegram binding 失效、C7 RevocationGate。

判据是「不产生投递副作用」与「不留静默空洞」两半：挂起期间不逐 tick 刷 skip 行，
解绑/封禁/吊销各有结构化 `skip_reason` 或 job 终态可查。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.admission.revocation import RevocationGate, RevocationRejected, TenantStatus
from bootstrap.db.repository.schedule_repo import (
    ScheduleRepository,
    ScheduleTransitionError,
)

pytestmark = pytest.mark.postgres


async def test_suspended_account_holds_without_skip_flood(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    set_account_status,
    exec_sql,
) -> None:
    """账号挂起：不插 execution、不前进调度态（避免逐 tick 刷 skip）。"""
    tenant = await make_tenant()
    job = await make_job(tenant, fire_at=clock.now + timedelta(seconds=1))
    set_account_status(str(tenant["account_id"]), "suspended")
    service = build_service()

    for _ in range(5):
        clock.advance(2)
        await run_ticks(service)

    assert await repo.list_executions(job_id=job["id"]) == []
    assert exec_sql("SELECT count(*) FROM outbound_delivery_intents")[0][0] == 0
    held = await repo.get_job(job["id"])
    assert held["status"] == "active"
    assert held["fire_at"] is not None, "挂起期间保持到期态，等待恢复后按规则判定"


async def test_resumed_account_settles_by_misfire_rules(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    set_account_status,
) -> None:
    """挂起 10 分钟后恢复：one-shot 已超宽限 → `missed`（不是补执行），且只记一次。"""
    tenant = await make_tenant()
    due = clock.now + timedelta(seconds=1)
    job = await make_job(tenant, fire_at=due)
    set_account_status(str(tenant["account_id"]), "suspended")
    service = build_service()

    for _ in range(3):
        clock.advance(2)
        await run_ticks(service)
    assert await repo.list_executions(job_id=job["id"]) == []

    set_account_status(str(tenant["account_id"]), "active")
    clock.advance(600)
    await run_ticks(service)

    executions = await repo.list_executions(job_id=job["id"])
    assert len(executions) == 1
    assert executions[0]["status"] == "missed"
    assert executions[0]["skip_reason"] == "misfire_grace_exceeded"


async def test_revoked_account_marks_jobs_revoked(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    set_account_status,
    exec_sql,
) -> None:
    """账号封禁联动：job 置 `revoked` 禁用，无 execution、无副作用。"""
    tenant = await make_tenant()
    job = await make_job(tenant, fire_at=clock.now + timedelta(seconds=1))
    set_account_status(str(tenant["account_id"]), "revoked")
    service = build_service()

    clock.advance(2)
    await run_ticks(service)

    revoked = await repo.get_job(job["id"])
    assert revoked["status"] == "revoked"
    assert await repo.list_executions(job_id=job["id"]) == []
    assert exec_sql("SELECT count(*) FROM outbound_delivery_intents")[0][0] == 0

    # 批量入口（账号生命周期事件侧的联动）同样幂等：再次调用不新增处置。
    assert await repo.revoke_jobs_by_account(tenant["account_id"]) == 0


async def test_revoked_job_cannot_be_resumed(
    make_tenant, make_job, repo: ScheduleRepository
) -> None:
    """`revoked` 是终态：resume 被拒（禁用 ≠ 暂停），不静默改回 active。"""
    tenant = await make_tenant()
    job = await make_job(tenant)
    await repo.set_job_status(job["id"], "revoked")
    with pytest.raises(ScheduleTransitionError):
        await repo.set_job_status(job["id"], "active")
    assert (await repo.get_job(job["id"]))["status"] == "revoked"


async def test_suspend_resume_bumps_revision(
    make_tenant, make_job, repo: ScheduleRepository
) -> None:
    """状态处置推进 `revision`，execution 记录触发时快照（计划版本可解释）。"""
    tenant = await make_tenant()
    job = await make_job(tenant)
    assert job["revision"] == 1

    suspended = await repo.set_job_status(job["id"], "suspended")
    assert suspended["status"] == "suspended" and suspended["revision"] == 2
    resumed = await repo.set_job_status(job["id"], "active")
    assert resumed["revision"] == 3
    # 同态重复处置不再 +1。
    again = await repo.set_job_status(job["id"], "active")
    assert again["revision"] == 3


async def test_telegram_binding_unbound_skips_without_delivery(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    exec_sql,
) -> None:
    """Telegram 渠道触发时重验 binding：已解绑 → `skipped(binding_inactive)`、零投递。"""
    tenant = await make_tenant()
    job = await make_job(
        tenant,
        delivery_channel="telegram",
        delivery_target="12345",
        fire_at=clock.now + timedelta(seconds=1),
    )
    service = build_service(telegram_channel_name="telegram")

    clock.advance(2)
    await run_ticks(service)

    execution = (await repo.list_executions(job_id=job["id"]))[0]
    assert execution["status"] == "skipped"
    assert execution["skip_reason"] == "binding_inactive"
    assert exec_sql("SELECT count(*) FROM canonical_messages")[0][0] == 0
    assert exec_sql("SELECT count(*) FROM outbound_delivery_intents")[0][0] == 0


async def test_telegram_binding_active_delivers(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    exec_sql,
) -> None:
    """绑定仍在位时同一个 Telegram job 正常投递（重验不是无条件拒）。"""
    tenant = await make_tenant()
    exec_sql(
        "INSERT INTO telegram_identity_bindings (account_id, tenant_id,"
        " telegram_user_id, telegram_chat_id, status, bound_via)"
        " VALUES (%s, %s, 'u-1', '12345', 'active', 'code')",
        (str(tenant["account_id"]), str(tenant["tenant_id"])),
    )
    job = await make_job(
        tenant,
        delivery_channel="telegram",
        delivery_target="12345",
        fire_at=clock.now + timedelta(seconds=1),
    )
    service = build_service(telegram_channel_name="telegram")

    clock.advance(2)
    await run_ticks(service)

    assert (await repo.list_executions(job_id=job["id"]))[0]["status"] == "succeeded"
    intents = exec_sql(
        "SELECT channel, target_chat_id FROM outbound_delivery_intents"
    )
    assert intents == [("telegram", "12345")]


async def test_revocation_gate_rejects_before_side_effects(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    exec_sql,
) -> None:
    """C7 触发前重校验：吊销租户的 instant job 不得推送（fail-closed）。"""
    tenant = await make_tenant()
    job = await make_job(tenant, fire_at=clock.now + timedelta(seconds=1))
    gate = SimpleNamespace(
        check=AsyncMock(
            side_effect=RevocationRejected(
                tenant_id=tenant["tenant_id"],
                action="scheduler_execute",
                status=TenantStatus.REVOKED,
            )
        )
    )
    service = build_service(revocation_gate=gate)

    clock.advance(2)
    await run_ticks(service)

    execution = (await repo.list_executions(job_id=job["id"]))[0]
    assert execution["status"] == "skipped"
    assert execution["skip_reason"] == "revocation_gate"
    assert exec_sql("SELECT count(*) FROM outbound_delivery_intents")[0][0] == 0
    gate.check.assert_awaited_once()


async def test_revocation_gate_allows_active_tenant(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
) -> None:
    """门禁放行（provider=None 的 dev_open 形态）不影响正常执行。"""
    tenant = await make_tenant()
    job = await make_job(tenant, fire_at=clock.now + timedelta(seconds=1))
    gate = RevocationGate(None, source="pilot")
    service = build_service(revocation_gate=gate)

    clock.advance(2)
    await run_ticks(service)

    assert (await repo.list_executions(job_id=job["id"]))[0]["status"] == "succeeded"


async def test_soft_gate_recheck_after_ai_call(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    exec_sql,
) -> None:
    """soft 的 AI 调用可能长达数十秒：落库前二次重验，吊销则不写 message/intent。"""
    tenant = await make_tenant()
    job = await make_job(
        tenant, tier="soft", prompt="p", fire_at=clock.now + timedelta(seconds=1)
    )

    calls: list[str] = []

    async def _check(tenant_id: str, *, action: str) -> None:
        calls.append(action)
        if action == "scheduler_soft_execute":
            raise RevocationRejected(
                tenant_id=tenant_id, action=action, status=TenantStatus.SUSPENDED
            )

    loop = SimpleNamespace(process_direct=AsyncMock(return_value="内容已生成"))
    service = build_service(
        agent_loop_provider=lambda: loop,
        revocation_gate=SimpleNamespace(check=_check),
    )

    clock.advance(2)
    await run_ticks(service)

    assert loop.process_direct.await_count == 1
    assert calls == ["scheduler_execute", "scheduler_soft_execute"]
    execution = (await repo.list_executions(job_id=job["id"]))[0]
    assert execution["status"] == "skipped"
    assert execution["skip_reason"] == "revocation_gate"
    assert exec_sql("SELECT count(*) FROM canonical_messages")[0][0] == 0


async def test_tool_suspend_resume_roundtrip(
    c11_factory,
    clock,
    make_tenant,
    repo: ScheduleRepository,
) -> None:
    """工具面 suspend→resume 可逆：暂停后不执行，恢复后按宽限判定。"""
    from agent.scheduler import ScheduleManager
    from agent.tools.context import ToolExecutionContext
    from agent.tools.registry import ToolRegistry
    from agent.tools.schedule import ResumeScheduleTool, SuspendScheduleTool
    from bootstrap.schedule_durable import DurableSchedulerService

    service: ScheduleManager = DurableSchedulerService(
        session_factory=c11_factory, _now_fn=clock
    )
    tenant = await make_tenant()
    job = await repo.create_job(
        tenant_id=tenant["tenant_id"],
        trigger="at",
        tier="instant",
        when="14:30",
        fire_at=clock.now + timedelta(seconds=1),
        timezone="UTC",
        delivery_channel="chat",
        delivery_target=tenant["tenant_id"],
        message="暂停我",
        name="暂停测试",
    )

    registry = ToolRegistry()
    registry.register(SuspendScheduleTool(service), risk="write")
    registry.register(ResumeScheduleTool(service), risk="write")
    ctx = ToolExecutionContext(
        request_id="req-1",
        account_id=str(tenant["account_id"]),
        tenant_id=str(tenant["tenant_id"]),
        session_id="chat:1",
        turn_id="turn-1",
        channel="chat",
        chat_id=str(tenant["tenant_id"]),
        principal_type="user",
    )

    suspended = str(
        await registry.execute(
            "suspend_schedule", {"name": "暂停测试"}, context=ctx
        )
    )
    assert "已暂停 1 个任务" in suspended
    assert (await repo.get_job(job["id"]))["status"] == "suspended"

    # 暂停期间到点不执行。
    clock.advance(60)
    assert await repo.claim_due_jobs(
        clock.now, advance=lambda _j, _a: None
    ) == []

    resumed = str(
        await registry.execute("resume_schedule", {"id": job["id"][:8]}, context=ctx)
    )
    assert "已恢复 1 个任务" in resumed
    assert (await repo.get_job(job["id"]))["status"] == "active"


async def test_tool_suspend_rejects_foreign_tenant(
    c11_factory, clock, make_tenant, repo: ScheduleRepository
) -> None:
    """跨租户暂停被拒（与 cancel 同归属判定），对方 job 状态不动。"""
    from agent.tools.context import ToolExecutionContext
    from agent.tools.schedule import SuspendScheduleTool
    from bootstrap.schedule_durable import DurableSchedulerService

    mine = await make_tenant("mine")
    other = await make_tenant("other")
    job = await repo.create_job(
        tenant_id=other["tenant_id"],
        trigger="at",
        tier="instant",
        when="14:30",
        fire_at=clock.now + timedelta(seconds=1),
        timezone="UTC",
        delivery_channel="chat",
        delivery_target=other["tenant_id"],
        message="别人的",
    )
    service = DurableSchedulerService(session_factory=c11_factory, _now_fn=clock)
    tool = SuspendScheduleTool(service)
    ctx = ToolExecutionContext(
        request_id="req-2",
        account_id=str(mine["account_id"]),
        tenant_id=str(mine["tenant_id"]),
        session_id="chat:2",
        turn_id="turn-2",
        channel="chat",
        chat_id=str(mine["tenant_id"]),
        principal_type="user",
    )

    result = await tool.execute(id=job["id"], **ctx.tool_kwargs())
    assert "code=task_foreign_tenant" in str(result)
    assert (await repo.get_job(job["id"]))["status"] == "active"
