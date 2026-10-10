"""6.2 创建与 owner 负向：模型提交的推送目标不等于服务端冻结 binding（§5.9.14）。

三段：① user principal 经 registry 调用时，落库的 delivery binding 是
`ToolExecutionContext` 注入值而非模型参数；② 解析不出 canonical conversation
即拒绝创建（fail-closed，不落半成品行）；③ list/cancel 的租户隔离。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.tools.context import ToolExecutionContext
from agent.tools.registry import ToolRegistry
from agent.tools.remind import RemindTool
from agent.tools.schedule import (
    CancelScheduleTool,
    ListSchedulesTool,
    ScheduleTool,
)
from bootstrap.db.repository.schedule_repo import ScheduleRepository
from bootstrap.schedule_durable import DurableSchedulerService

pytestmark = pytest.mark.postgres


def _user_ctx(tenant: dict[str, Any], *, channel: str, chat_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id=f"req-{tenant['tenant_id']}",
        account_id=str(tenant["account_id"]),
        tenant_id=str(tenant["tenant_id"]),
        session_id=f"{channel}:{chat_id}",
        turn_id=f"turn-{tenant['tenant_id']}",
        channel=channel,
        chat_id=chat_id,
        principal_type="user",
    )


def _registry(service: DurableSchedulerService) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ScheduleTool(service), risk="write")
    registry.register(RemindTool(service), risk="write")
    registry.register(ListSchedulesTool(service), risk="read-only")
    registry.register(CancelScheduleTool(service), risk="write")
    return registry


async def test_model_submitted_target_is_not_the_frozen_binding(
    c11_factory, clock, make_tenant, repo: ScheduleRepository
) -> None:
    """user principal 提交的 channel/chat_id 被 registry 剥离；冻结值 = 服务端注入值。"""
    tenant = await make_tenant()
    service = DurableSchedulerService(session_factory=c11_factory, _now_fn=clock)
    registry = _registry(service)
    ctx = _user_ctx(tenant, channel="chat", chat_id=tenant["tenant_id"])

    result = str(
        await registry.execute(
            "schedule",
            {
                "tier": "instant",
                "trigger": "at",
                "when": "18:00",
                "message": "取快递",
                # 模型试图把提醒推到别人的会话（C7 起 user principal 无此授权面）。
                "channel": "telegram",
                "chat_id": "attacker-chat-id",
            },
            context=ctx,
        )
    )
    assert "已注册定时任务" in result

    jobs = await repo.list_jobs(tenant_id=tenant["tenant_id"])
    assert len(jobs) == 1
    job = jobs[0]
    assert job["delivery_channel"] == "chat"
    assert job["delivery_target"] == str(tenant["tenant_id"])
    assert job["delivery_channel"] != "telegram"
    assert job["delivery_target"] != "attacker-chat-id"
    # 归属三元组同时落库（§5.9.14：不是只有 channel/chat）。
    assert job["account_id"] == str(tenant["account_id"])
    assert job["conversation_id"] == str(tenant["conversation_id"])


async def test_remind_freezes_binding_to_current_conversation(
    c11_factory, clock, make_tenant, repo: ScheduleRepository
) -> None:
    """remind 的三条提前量共用同一个冻结 binding（模型提交值同样被剥离）。"""
    tenant = await make_tenant()
    service = DurableSchedulerService(session_factory=c11_factory, _now_fn=clock)
    registry = _registry(service)
    ctx = _user_ctx(tenant, channel="chat", chat_id=tenant["tenant_id"])

    result = str(
        await registry.execute(
            "remind",
            {
                "when": "15:00",
                "description": "组会",
                "channel": "telegram",
                "chat_id": "someone-else",
            },
            context=ctx,
        )
    )
    assert "已为「组会」创建 3 条提醒" in result

    jobs = await repo.list_jobs(tenant_id=tenant["tenant_id"])
    assert len(jobs) == 3
    assert {j["delivery_target"] for j in jobs} == {str(tenant["tenant_id"])}
    # 事件时间原串 + 每条提前量进 schedule_spec_json（规格可解释）。
    assert {j["advance_minutes"] for j in jobs} == {30, 15, 1}
    assert {j["when"] for j in jobs} == {"15:00"}


async def test_create_without_canonical_conversation_is_rejected(
    c11_factory, clock, repo: ScheduleRepository
) -> None:
    """tenant 解析不出 canonical conversation → 拒绝创建，不落归属不明的 job 行。"""
    service = DurableSchedulerService(session_factory=c11_factory, _now_fn=clock)
    registry = _registry(service)
    ghost = {
        "tenant_id": "ghost_tenant",
        "account_id": "00000000-0000-0000-0000-00000000dead",
    }
    ctx = _user_ctx(ghost, channel="chat", chat_id="ghost_tenant")

    result = str(
        await registry.execute(
            "schedule",
            {
                "tier": "instant",
                "trigger": "at",
                "when": "18:00",
                "message": "不会存在",
            },
            context=ctx,
        )
    )
    assert "code=schedule_no_delivery_binding" in result
    assert await repo.list_jobs(tenant_id="ghost_tenant") == []


async def test_missing_server_target_rejects_before_touching_backend(
    c11_factory, clock, repo: ScheduleRepository
) -> None:
    """无上下文直调且没有 channel/chat_id：结构化拒绝，不落任何 job 行。"""
    service = DurableSchedulerService(session_factory=c11_factory, _now_fn=clock)
    tool = ScheduleTool(service)

    result = await tool.execute(
        tier="instant", trigger="at", when="18:00", message="x"
    )
    assert "code=schedule_no_delivery_binding" in result
    assert await repo.list_jobs() == []


async def test_list_and_cancel_are_tenant_scoped(
    c11_factory, clock, make_tenant, repo: ScheduleRepository
) -> None:
    """A 看不见 B 的任务；按 B 的 id 取消 → code=task_foreign_tenant 且 B 的行不动。"""
    tenant_a = await make_tenant("a")
    tenant_b = await make_tenant("b")
    service = DurableSchedulerService(session_factory=c11_factory, _now_fn=clock)
    registry = _registry(service)

    await registry.execute(
        "schedule",
        {"tier": "instant", "trigger": "at", "when": "18:00", "message": "A 的任务"},
        context=_user_ctx(tenant_a, channel="chat", chat_id=tenant_a["tenant_id"]),
    )
    await registry.execute(
        "schedule",
        {"tier": "instant", "trigger": "at", "when": "19:00", "message": "B 的任务"},
        context=_user_ctx(tenant_b, channel="chat", chat_id=tenant_b["tenant_id"]),
    )

    listing = str(
        await registry.execute(
            "list_schedules", {}, context=_user_ctx(tenant_a, channel="chat", chat_id=tenant_a["tenant_id"])
        )
    )
    assert "A 的任务" in listing
    assert "B 的任务" not in listing

    b_job = (await repo.list_jobs(tenant_id=tenant_b["tenant_id"]))[0]
    refused = str(
        await registry.execute(
            "cancel_schedule",
            {"id": b_job["id"]},
            context=_user_ctx(tenant_a, channel="chat", chat_id=tenant_a["tenant_id"]),
        )
    )
    assert "code=task_foreign_tenant" in refused
    assert (await repo.get_job(b_job["id"]))["status"] == "active"

    cancelled = str(
        await registry.execute(
            "cancel_schedule",
            {"name": "A 的任务"},
            context=_user_ctx(tenant_a, channel="chat", chat_id=tenant_a["tenant_id"]),
        )
    )
    # 创建时未设 name，按名称取消不命中（不谎报「已取消 0 个」）。
    assert "未找到名称为" in cancelled
    a_job = (await repo.list_jobs(tenant_id=tenant_a["tenant_id"]))[0]
    done = str(
        await registry.execute(
            "cancel_schedule",
            {"id": a_job["id"][:8]},
            context=_user_ctx(tenant_a, channel="chat", chat_id=tenant_a["tenant_id"]),
        )
    )
    assert "已取消 1 个任务" in done
    assert (await repo.get_job(a_job["id"]))["status"] == "revoked"
