"""C7 task 6.2：后台任务 owner 归属与跨租户重校验（design ADR-7）。

- schedule/remind 创建任务时以调用方租户打 owner 标记（context 注入，模型不可覆盖）；
- list_schedules 只列当前租户任务（user principal），dev/owner 全量；
- cancel_schedule 跨租户任务拒绝（code=task_foreign_tenant，不静默改删）；
- scheduler 触发即执行前重校验：创建后账号被封禁 → 到达执行时机拒绝副作用；
- task_output/task_stop 跨租户任务拒绝（user principal），own tenant 正常；
- spawn/shell/task_stop 对普通租户保持关闭面（process-exec，task 4.2 闸门）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import agent.tools.shell as shell_mod
from agent.admission.revocation import RevocationGate, TenantStatus
from agent.scheduler import LatencyTracker, SchedulerService, ScheduledJob
from agent.tools.base import Tool, ToolEffect, ToolResult
from agent.tools.context import ToolExecutionContext
from agent.tools.registry import ToolRegistry
from agent.tools.remind import RemindTool
from agent.tools.schedule import CancelScheduleTool, ListSchedulesTool, ScheduleTool
from agent.tools.shell import ShellTaskOutputTool, ShellTaskStopTool
from tests.conftest import drain_tasks, make_job

_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_NOW_FN = lambda: _NOW  # noqa: E731


def make_svc(tmp_path, mock_push, mock_loop, *, gate=None):
    return SchedulerService(
        store_path=tmp_path / "jobs.json",
        push_tool=mock_push,
        agent_loop=mock_loop,
        tracker=LatencyTracker(default=25.0),
        _now_fn=_NOW_FN,
        revocation_gate=gate,
    )


def _user_ctx(tenant: str) -> dict[str, str]:
    """模拟 ToolExecutionContext.tool_kwargs() 注入的可信键（不含 channel/chat_id，
    调用方按工具参数显式传递）。"""
    return {
        "tenant_id": tenant,
        "principal_type": "user",
    }


# ── schedule/remind 打 owner ─────────────────────────────────────


async def test_schedule_stamps_owner_from_context(tmp_path, mock_push, mock_loop):
    svc = make_svc(tmp_path, mock_push, mock_loop)
    tool = ScheduleTool(svc)
    result = await tool.execute(
        tier="instant",
        trigger="at",
        when="14:30",
        channel="chat",
        chat_id="tenant:a",
        message="hi",
        **_user_ctx("tenant:a"),
    )
    assert "已注册" in result
    jobs = svc.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].owner_tenant_id == "tenant:a"


async def test_remind_stamps_owner_on_three_jobs(tmp_path, mock_push, mock_loop):
    svc = make_svc(tmp_path, mock_push, mock_loop)
    tool = RemindTool(svc)
    await tool.execute(
        when="2025-06-02T12:00",
        description="组会",
        channel="chat",
        chat_id="tenant:a",
        **_user_ctx("tenant:a"),
    )
    jobs = svc.list_jobs()
    assert len(jobs) == 3
    assert {j.owner_tenant_id for j in jobs} == {"tenant:a"}


# ── list_schedules 租户隔离 ──────────────────────────────────────


async def _seed_two_tenants(svc: SchedulerService) -> None:
    for tenant in ("tenant:a", "tenant:b"):
        for i in range(2):
            svc.add_job(
                make_job(
                    name=f"{tenant}-job{i}",
                    fire_at=_NOW + timedelta(minutes=5),
                )
            )
            svc._jobs[list(svc._jobs.keys())[-1]].owner_tenant_id = tenant


async def test_list_schedules_scoped_to_own_tenant(tmp_path, mock_push, mock_loop):
    svc = make_svc(tmp_path, mock_push, mock_loop)
    await _seed_two_tenants(svc)
    text = await ListSchedulesTool(svc).execute(**_user_ctx("tenant:a"))
    assert "tenant:a-job0" in text
    assert "tenant:a-job1" in text
    assert "tenant:b" not in text


async def test_list_schedules_dev_principal_sees_all(tmp_path, mock_push, mock_loop):
    svc = make_svc(tmp_path, mock_push, mock_loop)
    await _seed_two_tenants(svc)
    text = await ListSchedulesTool(svc).execute(
        tenant_id="tenant:a", principal_type="dev"
    )
    assert "tenant:a-job0" in text
    assert "tenant:b-job0" in text


# ── cancel_schedule 跨租户拒绝 ───────────────────────────────────


async def test_cancel_schedule_rejects_foreign_tenant_by_id(
    tmp_path, mock_push, mock_loop
):
    svc = make_svc(tmp_path, mock_push, mock_loop)
    svc.add_job(
        make_job(name="secret-job", fire_at=_NOW + timedelta(minutes=5))
    )
    job = svc.list_jobs()[0]
    job.owner_tenant_id = "tenant:b"

    result = await CancelScheduleTool(svc).execute(
        id=job.id[:10], **_user_ctx("tenant:a")
    )
    assert "task_foreign_tenant" in result
    assert svc.list_jobs(tenant_id="tenant:b"), "对方租户任务不得被删"


async def test_cancel_schedule_rejects_foreign_name(tmp_path, mock_push, mock_loop):
    svc = make_svc(tmp_path, mock_push, mock_loop)
    svc.add_job(make_job(name="standup", fire_at=_NOW + timedelta(minutes=5)))
    svc.list_jobs()[0].owner_tenant_id = "tenant:b"

    result = await CancelScheduleTool(svc).execute(
        name="standup", **_user_ctx("tenant:a")
    )
    assert "task_foreign_tenant" in result
    assert svc.list_jobs(tenant_id="tenant:b"), "对方租户任务不得被删"


async def test_cancel_schedule_own_tenant_works(tmp_path, mock_push, mock_loop):
    svc = make_svc(tmp_path, mock_push, mock_loop)
    svc.add_job(make_job(name="mine", fire_at=_NOW + timedelta(minutes=5)))
    svc.list_jobs()[0].owner_tenant_id = "tenant:a"

    result = await CancelScheduleTool(svc).execute(
        name="mine", **_user_ctx("tenant:a")
    )
    assert "已取消" in result
    assert not svc.list_jobs(tenant_id="tenant:a")


# ── scheduler 触发前重校验 ───────────────────────────────────────


async def _provider(status: TenantStatus):
    async def _check(tenant_id: str) -> TenantStatus:
        return status

    return _check


async def test_scheduler_fire_rejected_when_owner_revoked(
    tmp_path, mock_push, mock_loop
):
    gate = RevocationGate(await _provider(TenantStatus.REVOKED), source="test")
    svc = make_svc(tmp_path, mock_push, mock_loop, gate=gate)
    job = make_job(tier="instant", fire_at=_NOW - timedelta(seconds=1))
    job.owner_tenant_id = "tenant:a"
    svc._jobs[job.id] = job

    await svc._tick()
    await drain_tasks()

    mock_push.execute.assert_not_called()  # 0 副作用


async def test_scheduler_fire_allowed_when_owner_active(
    tmp_path, mock_push, mock_loop
):
    gate = RevocationGate(await _provider(TenantStatus.ACTIVE), source="test")
    svc = make_svc(tmp_path, mock_push, mock_loop, gate=gate)
    job = make_job(tier="instant", fire_at=_NOW - timedelta(seconds=1))
    job.owner_tenant_id = "tenant:a"
    svc._jobs[job.id] = job

    await svc._tick()
    await drain_tasks()

    mock_push.execute.assert_called_once()


# ── task_output / task_stop 跨租户拒绝 ───────────────────────────


def _fake_proc(returncode: int = 0) -> Any:
    return type("_Proc", (), {"returncode": returncode})()


def _seed_bg_task(task_id: str, owner: str | None, log_path: str) -> None:
    shell_mod._BG_REGISTRY[task_id] = shell_mod._BackgroundTask(
        proc=_fake_proc(),
        log_path=log_path,
        pump_task=asyncio.ensure_future(asyncio.sleep(0)),
        started_at=shell_mod.time.monotonic(),
        wall_started_at_ms=int(shell_mod.time.time() * 1000),
        owner_tenant_id=owner,
    )


async def test_task_output_foreign_tenant_rejected(tmp_path):
    Path(tmp_path / "bg.log").write_text("secret", encoding="utf-8")
    _seed_bg_task("t_foreign", "tenant:b", str(tmp_path / "bg.log"))
    try:
        result = await ShellTaskOutputTool().execute(
            task_id="t_foreign", **_user_ctx("tenant:a")
        )
        assert "task_foreign_tenant" in result
    finally:
        shell_mod._BG_REGISTRY.pop("t_foreign", None)


async def test_task_output_own_tenant_allowed(tmp_path):
    Path(tmp_path / "bg.log").write_text("ok", encoding="utf-8")
    _seed_bg_task("t_own", "tenant:a", str(tmp_path / "bg.log"))
    try:
        result = await ShellTaskOutputTool().execute(
            task_id="t_own", **_user_ctx("tenant:a")
        )
        assert "task_foreign_tenant" not in result
        assert "ok" in result
    finally:
        shell_mod._BG_REGISTRY.pop("t_own", None)


async def test_task_stop_foreign_tenant_rejected(tmp_path, monkeypatch):
    _seed_bg_task("s_foreign", "tenant:b", str(tmp_path / "bg.log"))
    killed: list[str] = []

    async def _fake_kill(task_id: str) -> None:
        killed.append(task_id)

    monkeypatch.setattr(shell_mod, "_bg_kill", _fake_kill)
    try:
        result = await ShellTaskStopTool().execute(
            task_id="s_foreign", **_user_ctx("tenant:a")
        )
        assert "task_foreign_tenant" in result
        assert killed == [], "对方租户任务不得被 kill"
        assert "s_foreign" in shell_mod._BG_REGISTRY
    finally:
        shell_mod._BG_REGISTRY.pop("s_foreign", None)


# ── spawn/shell/task_stop 关闭面 ────────────────────────────────


class _ProcExecStub(Tool):
    """基类只提供 execute；name 由 _proc_stub 按工具名注入。"""

    name = "_placeholder_proc"
    description = "process-exec stub"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return "ran"


def _proc_stub(name: str) -> Tool:
    return type(f"_Proc_{name}", (_ProcExecStub,), {"name": name})()


def _ctx(tenant: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="req-1",
        account_id=f"acct-{tenant}",
        tenant_id=tenant,
        session_id=f"chat:{tenant}",
        turn_id="turn-1",
        channel="chat",
        chat_id=tenant,
        principal_type="user",
    )


@pytest.mark.asyncio
async def test_spawn_shell_task_stop_closed_face_for_user() -> None:
    """spawn/shell/task_stop 对普通租户关闭面（effect 闸门，task 4.2/ADR-4）。"""
    registry = ToolRegistry()
    for name in ("spawn", "shell", "task_stop", "spawn_manage"):
        registry.register(
            _proc_stub(name),
            effect=ToolEffect.PROCESS_EXEC,
        )
    for name in ("spawn", "shell", "task_stop", "spawn_manage"):
        result = await registry.execute(name, {}, context=_ctx("tenant:a"))
        assert "tool_denied_effect" in str(result), name


@pytest.mark.asyncio
async def test_task_output_effect_not_denied_then_owner_checked() -> None:
    """task_output 是 read-only（可达），但跨租户仍被 owner 校验拒绝。"""
    registry = ToolRegistry()
    registry.register(_proc_stub("task_output"), effect=ToolEffect.READ_ONLY)
    result = await registry.execute(
        "task_output",
        {"task_id": "ghost"},
        context=_ctx("tenant:a"),
    )
    assert "tool_denied_effect" not in str(result)