"""6.4 misfire + 6.5 recurring 不回放 + 6.7 重启恢复。

这四条是本次改造的产品级差异：legacy 路径超过 5 分钟宽限就把 one-shot **静默删除**、
recurring 恢复时逐 occurrence 补跑；durable 路径一律记账（`missed`/`skipped` +
原因），recurring 只跳一次到下一未来时刻，崩溃残留的 `running` 收束为 `failed`
且不重放副作用。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from bootstrap.db.repository.schedule_repo import ScheduleRepository

pytestmark = pytest.mark.postgres


async def test_one_shot_beyond_grace_is_missed_not_silently_deleted(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    exec_sql,
) -> None:
    """超宽限的 one-shot：记 `missed` 并可查，既不补执行也不删行（§10 PROPOSED DEFAULT）。"""
    tenant = await make_tenant()
    fired = clock.now - timedelta(seconds=301)
    job = await make_job(tenant, fire_at=fired, message="错过的提醒")
    service = build_service()

    await run_ticks(service)

    executions = await repo.list_executions(job_id=job["id"])
    assert len(executions) == 1
    assert executions[0]["status"] == "missed"
    assert executions[0]["skip_reason"] == "misfire_grace_exceeded"
    assert executions[0]["attempt_count"] == 0
    # 零副作用。
    assert exec_sql("SELECT count(*) FROM canonical_messages")[0][0] == 0
    assert exec_sql("SELECT count(*) FROM outbound_delivery_intents")[0][0] == 0
    # job 行保留（不是删除），无待执行 occurrence，终态冗余可见。
    refreshed = await repo.get_job(job["id"])
    assert refreshed["status"] == "active"
    assert refreshed["fire_at"] is None
    assert refreshed["last_outcome"] == "missed"
    # admin 面按状态可查。
    assert len(await repo.list_executions(status="missed")) == 1


async def test_one_shot_within_grace_still_executes(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    exec_sql,
) -> None:
    """宽限内（<300s）照常补执行——grace 只决定补执行还是记账。"""
    tenant = await make_tenant()
    job = await make_job(tenant, fire_at=clock.now - timedelta(seconds=299))
    service = build_service()

    await run_ticks(service)

    assert (await repo.list_executions(job_id=job["id"]))[0]["status"] == "succeeded"
    assert exec_sql("SELECT count(*) FROM outbound_delivery_intents")[0][0] == 1


async def test_recurring_recovery_does_not_replay_missed_occurrences(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
    exec_sql,
) -> None:
    """宕机 5 分钟、间隔 1 分钟的 recurring：≤1 条 skipped + 跳到下一未来时刻，不补 5 次。"""
    tenant = await make_tenant()
    job = await make_job(
        tenant,
        trigger="every",
        when="1m",
        interval_seconds=60,
        cron_expr=None,
        message="心跳",
        fire_at=clock.now - timedelta(minutes=6),
    )
    service = build_service()

    await run_ticks(service)

    executions = await repo.list_executions(job_id=job["id"])
    assert len(executions) == 1, "跳过边界只记一条，不逐 occurrence 补记账"
    assert executions[0]["status"] == "skipped"
    assert executions[0]["skip_reason"] == "recurring_advance"
    assert exec_sql("SELECT count(*) FROM outbound_delivery_intents")[0][0] == 0

    refreshed = await repo.get_job(job["id"])
    assert refreshed["fire_at"] > clock.now, "必须前进到未来 occurrence"
    assert refreshed["last_outcome"] == "skipped"

    # 走到下一个名义时刻：只成功一次，累计仍不含补跑。
    clock.set(refreshed["fire_at"] + timedelta(seconds=1))
    await run_ticks(service)

    executions = await repo.list_executions(job_id=job["id"])
    statuses = sorted(e["status"] for e in executions)
    assert statuses == ["skipped", "succeeded"]
    assert exec_sql("SELECT count(*) FROM outbound_delivery_intents")[0][0] == 1


async def test_recurring_within_grace_executes_and_advances(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
) -> None:
    """宽限内的 recurring：本次执行 + 以 max(now, scheduled_for) 为基准前进（防重复边界）。"""
    tenant = await make_tenant()
    due = clock.now - timedelta(seconds=30)
    job = await make_job(
        tenant,
        trigger="every",
        when="2m",
        interval_seconds=120,
        cron_expr=None,
        fire_at=due,
    )
    service = build_service()

    await run_ticks(service)

    assert (await repo.list_executions(job_id=job["id"]))[0]["status"] == "succeeded"
    refreshed = await repo.get_job(job["id"])
    # interval 走绝对推进（ADR-6：不随 DST/延迟漂移）：下一时刻 = 本次名义 + 一个间隔。
    assert refreshed["fire_at"] == due + timedelta(seconds=120)


async def test_restart_sweeps_running_to_failed_without_replay(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    exec_sql,
) -> None:
    """崩溃残留的 `running`：启动扫描收束为 `failed`，不补投、同一 occurrence 不再被认领。"""
    tenant = await make_tenant()
    occurrence = clock.now - timedelta(seconds=10)
    job = await make_job(tenant, fire_at=occurrence)
    # 手工造崩溃现场：running execution + 调度态已被清空的 one-shot job。
    execution_id = exec_sql(
        "INSERT INTO schedule_executions (job_id, tenant_id, scheduled_for, status,"
        " attempt_count, schedule_timezone, schedule_revision, started_at)"
        " VALUES (%s, %s, %s, 'running', 1, 'UTC', 1, now()) RETURNING id",
        (job["id"], tenant["tenant_id"], occurrence),
    )[0][0]
    exec_sql("UPDATE scheduled_jobs SET next_scheduled_for = NULL WHERE id = %s", (job["id"],))

    service = build_service()
    task = asyncio.create_task(service.run())
    try:
        for _ in range(100):
            swept = await repo.list_executions(job_id=job["id"])
            if swept and swept[0]["status"] != "running":
                break
            await asyncio.sleep(0.02)
    finally:
        service.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(swept) == 1
    assert swept[0]["status"] == "failed"
    assert swept[0]["error"] == "interrupted_by_restart"
    assert swept[0]["skip_reason"] is None
    assert str(swept[0]["id"]) == str(execution_id)
    # 零副作用，且 one-shot 仍无待执行 occurrence。
    assert exec_sql("SELECT count(*) FROM canonical_messages")[0][0] == 0
    assert exec_sql("SELECT count(*) FROM outbound_delivery_intents")[0][0] == 0
    assert (await repo.get_job(job["id"]))["fire_at"] is None


async def test_swept_occurrence_cannot_be_reclaimed(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    exec_sql,
) -> None:
    """`(job_id, scheduled_for)` 去重：崩溃收束后的同一 occurrence 不会再被执行一次。"""
    tenant = await make_tenant()
    occurrence = clock.now - timedelta(seconds=10)
    job = await make_job(tenant, fire_at=occurrence)
    exec_sql(
        "INSERT INTO schedule_executions (job_id, tenant_id, scheduled_for, status,"
        " attempt_count, schedule_timezone, schedule_revision, started_at)"
        " VALUES (%s, %s, %s, 'running', 1, 'UTC', 1, now()) RETURNING id",
        (job["id"], tenant["tenant_id"], occurrence),
    )
    assert len(await repo.sweep_interrupted_executions()) == 1

    # 调度态被拨回同一 occurrence（崩溃重放/并发 tick 的最坏情形）。
    exec_sql(
        "UPDATE scheduled_jobs SET next_scheduled_for = %s, status='active'"
        " WHERE id = %s",
        (occurrence, job["id"]),
    )
    claimed = await repo.claim_due_jobs(
        clock.now, advance=lambda _job, _after: None
    )
    assert [c.job_id for c in claimed] == []
    # 记账仍是一条（收束为 failed），没有第二条同名 occurrence 执行。
    executions = await repo.list_executions(job_id=job["id"])
    assert len(executions) == 1
    assert executions[0]["status"] == "failed"


async def test_misfire_grace_is_configurable(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
) -> None:
    """宽限来自 `[scheduler].misfire_grace_seconds`：收紧到 10s 后同样迟到即 `missed`。"""
    tenant = await make_tenant()
    job = await make_job(tenant, fire_at=clock.now - timedelta(seconds=20))
    service = build_service(misfire_grace_seconds=10)

    await run_ticks(service)

    assert (await repo.list_executions(job_id=job["id"]))[0]["status"] == "missed"
    assert service.repo._grace == timedelta(seconds=10)


async def test_recurring_cron_recovery_jumps_to_next_future_fire(
    make_tenant,
    make_job,
    clock,
    repo: ScheduleRepository,
    build_service,
    run_ticks,
) -> None:
    """cron 型 recurring：超宽限恢复后由 `next_cron_fire` 直接给出下一未来时刻。"""
    tenant = await make_tenant()
    job = await make_job(
        tenant,
        trigger="every",
        when="*/5 * * * *",
        interval_seconds=None,
        cron_expr="*/5 * * * *",
        fire_at=clock.now - timedelta(minutes=37),
    )
    service = build_service()

    await run_ticks(service)

    refreshed = await repo.get_job(job["id"])
    assert refreshed["fire_at"].minute % 5 == 0
    assert refreshed["fire_at"] > clock.now
    assert (await repo.list_executions(job_id=job["id"]))[0]["status"] == "skipped"
