from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from agent.scheduler import (
    LatencyTracker,
    ScheduleManager,
    SchedulerService,
)
from agent.tools.message_push import MessagePushTool
from agent.tools.registry import ToolRegistry
from agent.tools.remind import RemindTool
from agent.tools.schedule import (
    CancelScheduleTool,
    ListSchedulesTool,
    ResumeScheduleTool,
    ScheduleTool,
    SuspendScheduleTool,
)
from bootstrap.toolsets.protocol import (
    ToolsetDeps,
    ToolsetProvider,
    build_registration_result,
)


class SchedulerToolsetProvider(ToolsetProvider):
    def register(self, registry: ToolRegistry, deps: ToolsetDeps):
        before = set(registry._tools.keys())
        scheduler = deps.scheduler
        if scheduler is None:
            raise RuntimeError("SchedulerToolsetProvider requires scheduler")
        registry.register(
            ScheduleTool(scheduler),
            risk="write",
            search_hint="cron timer 延时执行",
        )
        registry.register(
            ListSchedulesTool(scheduler),
            risk="read-only",
            search_hint="提醒列表 已有计划",
        )
        registry.register(
            CancelScheduleTool(scheduler),
            risk="write",
            search_hint="删除提醒 取消任务",
        )
        registry.register(
            RemindTool(scheduler),
            risk="write",
            search_hint="日程提醒 会议提醒 出门提醒 提前通知",
        )
        # C11：暂停/恢复是工具层的显式状态入口（取消是终态删除，暂停可逆）。
        registry.register(
            SuspendScheduleTool(scheduler),
            risk="write",
            search_hint="暂停提醒 暂时停用 稍后再说",
        )
        registry.register(
            ResumeScheduleTool(scheduler),
            risk="write",
            search_hint="恢复提醒 取消暂停 重新启用",
        )
        return build_registration_result(
            registry=registry,
            source_name="schedule",
            before=before,
            extras={"scheduler": scheduler},
        )


def build_scheduler(
    workspace: Path,
    push_tool: MessagePushTool,
    *,
    config: Any = None,
    agent_loop_provider: Callable[[], Any] | None = None,
    revocation_gate: "Any | None" = None,
) -> ScheduleManager:
    """按存储后端装配调度服务（c11-explicit-schedules ADR-4）。

    `storage.backend == "postgres"` → `DurableSchedulerService`（tenant/account/
    conversation owned 的 PG job + execution，投递经 C2 outbox）；否则保持 legacy
    JSON 路径不变（dev 单用户：`schedules.json` + 内存 task），`push_tool` 只在
    legacy 分支使用。
    """
    if getattr(getattr(config, "storage", None), "backend", "sqlite") == "postgres":
        from bootstrap.schedule_durable import build_durable_scheduler

        return build_durable_scheduler(
            config,
            agent_loop_provider=agent_loop_provider,
            revocation_gate=revocation_gate,
        )
    return SchedulerService(
        store_path=workspace / "schedules.json",
        push_tool=push_tool,
        agent_loop=None,
        agent_loop_provider=agent_loop_provider,
        tracker=LatencyTracker(),
        revocation_gate=revocation_gate,
    )


def register_scheduler_tools(
    tools: ToolRegistry,
    scheduler: ScheduleManager,
) -> None:
    SchedulerToolsetProvider().register(
        tools,
        ToolsetDeps(
            config=None,
            workspace=Path("."),
            scheduler=scheduler,
        ),
    )
