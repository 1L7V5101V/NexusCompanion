"""
定时任务工具：ScheduleTool / ListSchedulesTool / CancelScheduleTool
+ SuspendScheduleTool / ResumeScheduleTool（C11 状态联动）

AI 通过这些工具注册、查询、暂停/恢复、取消定时任务。后端由 `ScheduleManager` 契约
隔离（legacy JSON 与 PG durable 两个实现），工具面不感知存储。
"""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent.scheduler import (
    SCHEDULE_NO_DELIVERY_BINDING_REJECT,
    JobRef,
    ScheduleDeliveryUnresolvedError,
    ScheduleManager,
    ScheduledJob,
    compute_fire_at,
    is_cron_expr,
    parse_duration,
)
from agent.tools.base import Tool


class ScheduleTool(Tool):
    name = "schedule"
    description = (
        "注册定时任务。支持三种触发模式：\n"
        "  at    — 指定绝对时间，如 '14:30' 或 '2025-06-01T09:00'\n"
        "  after — 相对延迟，如 '30s' '5m' '2h'（需传 request_time 补偿延迟）\n"
        "  every — 循环，如 '1h' '30m' '0 9 * * *'（每天9点）\n\n"
        "两种执行模式：\n"
        "  instant — 到时直接推送固定消息，适合喝水提醒等固定文本\n"
        "  soft    — 到时调用 AI 生成实时内容，适合天气/新闻等\n\n"
        "可选参数 advance_minutes：提前N分钟触发，适用于组会/日程提前提醒场景。\n"
        "如需多个提前量（如提前30分钟 + 提前15分钟），多次调用本工具即可。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "tier": {
                "type": "string",
                "enum": ["instant", "soft"],
                "description": "instant=直接推消息；soft=触发时调用AI生成内容",
            },
            "trigger": {
                "type": "string",
                "enum": ["at", "after", "every"],
                "description": "触发模式",
            },
            "when": {
                "type": "string",
                "description": (
                    "触发时间描述，与 trigger 对应：\n"
                    "  at    → '14:30' 或 '2025-06-01T09:00'\n"
                    "  after → '30s' '5m' '2h'\n"
                    "  every → '1h' '30m' '0 9 * * *'"
                ),
            },
            "message": {
                "type": "string",
                "description": "tier=instant 时的消息内容（必填）",
            },
            "prompt": {
                "type": "string",
                "description": "tier=soft 时触发 AI 的提示词（必填）",
            },
            "channel": {
                "type": "string",
                "description": "目标渠道，如 telegram、qq",
            },
            "chat_id": {
                "type": "string",
                "description": "目标会话 ID",
            },
            "timezone": {
                "type": "string",
                "description": "时区，如 Asia/Shanghai，默认使用系统配置",
            },
            "name": {
                "type": "string",
                "description": "任务名，方便后续用 cancel_schedule 取消",
            },
            "advance_minutes": {
                "type": "integer",
                "description": "提前N分钟触发，如 30 表示在 when 指定时间前30分钟触发。适合日程提前提醒。",
            },
            "request_time": {
                "type": "string",
                "description": (
                    "trigger=after 时必填：来自 system prompt 的消息接收时间（ISO 格式）。"
                    "用于从用户发消息时刻计算延迟，而非从 tool 调用时刻计算。"
                ),
            },
        },
        "required": ["tier", "trigger", "when", "channel", "chat_id"],
    }

    def __init__(self, service: ScheduleManager, default_tz: str = "Asia/Shanghai") -> None:
        self._service = service
        self._default_tz = default_tz

    async def execute(self, **kwargs: Any) -> str:
        tier = kwargs.get("tier", "")
        trigger = kwargs.get("trigger", "")
        when = kwargs.get("when", "")
        message = kwargs.get("message")
        prompt = kwargs.get("prompt")
        channel = kwargs.get("channel", "")
        chat_id = str(kwargs.get("chat_id", ""))
        tz = kwargs.get("timezone") or self._default_tz
        name = kwargs.get("name")
        request_time = kwargs.get("request_time")
        advance_minutes = kwargs.get("advance_minutes")

        # ── validation ──
        if tier not in ("instant", "soft"):
            return f"错误：tier 须为 instant 或 soft，收到 {tier!r}"
        if trigger not in ("at", "after", "every"):
            return f"错误：trigger 须为 at/after/every，收到 {trigger!r}"
        if tier == "instant" and not message:
            return "错误：tier=instant 时 message 为必填项"
        if tier == "soft" and not prompt:
            return "错误：tier=soft 时 prompt 为必填项"
        # user principal 的 channel/chat_id 由 registry 从 ToolExecutionContext 注入
        # （模型提交值已被剥离）；两者仍为空即无服务端授权目标——直接拒绝。
        if not channel or not chat_id:
            return SCHEDULE_NO_DELIVERY_BINDING_REJECT

        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            return f"错误：无效的时区 {tz!r}"

        # ── compute fire_at ──
        try:
            fire_at = compute_fire_at(trigger, when, tz, request_time)
        except ValueError as e:
            return f"错误：{e}"

        # ── apply advance_minutes offset ──
        advance_applied: int | None = None
        if advance_minutes is not None:
            try:
                offset = int(advance_minutes)
            except (TypeError, ValueError):
                return f"错误：advance_minutes 须为整数，收到 {advance_minutes!r}"
            if offset <= 0:
                return f"错误：advance_minutes 须为正整数，收到 {offset}"
            fire_at = fire_at - timedelta(minutes=offset)
            advance_applied = offset

        # ── parse every spec ──
        interval_seconds = None
        cron_expr = None
        if trigger == "every":
            try:
                if is_cron_expr(when):
                    cron_expr = when.strip()
                else:
                    iv = parse_duration(when)
                    interval_seconds = int(iv.total_seconds())
            except ValueError as e:
                return f"错误：{e}"

        # ── build & register ──
        job = ScheduledJob(
            trigger=trigger,
            tier=tier,
            fire_at=fire_at,
            channel=channel,
            chat_id=chat_id,
            interval_seconds=interval_seconds,
            cron_expr=cron_expr,
            message=message,
            prompt=prompt,
            name=name,
            timezone=tz,
            when=when,
            advance_minutes=advance_applied,
            # C7 task 6.2（ADR-7）：所有者 = 调用方租户（context 注入，模型不可覆盖）。
            owner_tenant_id=kwargs.get("tenant_id") or None,
        )
        try:
            # durable 后端在此冻结 delivery binding 并落 PG；legacy 后端落 JSON。
            job = await self._service.create_job(job)
        except ScheduleDeliveryUnresolvedError:
            return SCHEDULE_NO_DELIVERY_BINDING_REJECT

        # 优先用 fire_at 自带的时区（来自 request_time 的 offset），
        # 让用户看到本地时间而不是 UTC
        try:
            if fire_at.tzinfo is not None and str(fire_at.tzinfo) not in ("UTC", "utc"):
                display_dt = fire_at
            elif request_time:
                parsed_rt = datetime.fromisoformat(request_time)
                display_dt = (
                    fire_at.astimezone(parsed_rt.tzinfo)
                    if parsed_rt.tzinfo
                    else fire_at.astimezone()
                )
            else:
                display_dt = fire_at.astimezone()
            time_str = display_dt.strftime("%Y-%m-%d %H:%M:%S %z")
        except Exception:
            time_str = fire_at.isoformat()

        label = f"「{name}」" if name else job.id[:8]
        return f"已注册定时任务 {label}，首次触发时间：{time_str}"


class ListSchedulesTool(Tool):
    name = "list_schedules"
    description = "列出所有待执行的定时任务（仅当前租户）"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, service: ScheduleManager) -> None:
        self._service = service

    async def execute(self, **kwargs: Any) -> str:
        # C7 task 6.2（ADR-7）：普通账号只能查看自己租户的任务；
        # dev/owner（及无上下文的直调）保持全量可见。
        scope = _user_scope(kwargs)
        jobs = await self._service.fetch_jobs(tenant_id=scope)
        if not jobs:
            return "当前没有待执行的定时任务"

        lines = [f"定时任务列表（共 {len(jobs)} 个）："]
        for job in jobs:
            try:
                fire_at_local = job.fire_at.astimezone(ZoneInfo(job.timezone))
                time_str = fire_at_local.strftime("%Y-%m-%d %H:%M:%S %Z")
            except Exception:
                time_str = job.fire_at.isoformat()

            label = f"「{job.name}」" if job.name else job.id[:8]
            if job.tier == "instant":
                action = (job.message or "")[:40]
            else:
                action = f"[AI] {(job.prompt or '')[:40]}"

            lines.append(
                f"• {label}  [{job.tier}/{job.trigger}]  "
                f"下次: {time_str}  "
                f"内容: {action}  "
                f"已运行: {job.run_count}次"
            )
        return "\n".join(lines)


def _user_scope(kwargs: dict[str, Any]) -> str | None:
    """principal=user 时按调用方租户隔离；dev/owner/无上下文直调保持全量（ADR-7）。"""
    return (
        kwargs.get("tenant_id") or None
        if kwargs.get("principal_type") == "user"
        else None
    )


async def _resolve_targets(
    service: ScheduleManager,
    *,
    job_id: str,
    name: str,
    scope: str | None,
    verb: str,
) -> tuple[list[JobRef], str | None]:
    """按 id 前缀或名称命中候选任务，并做跨租户拒绝判定。

    返回 ``(候选, 直接回给模型的拒绝串)``：命中为空或存在外租户任务时后者非 None。
    判定只看归属（`JobRef` 的 id/名称/租户），不外泄任务内容。
    """
    if job_id:
        refs = await service.find_job_refs(id_prefix=job_id)
        empty = f"未找到 ID 为 {job_id!r} 的任务"
    else:
        refs = await service.find_job_refs(name=name)
        empty = f"未找到名称为 {name!r} 的任务"
    if not refs:
        return [], empty
    if scope is not None and any(ref.owner_tenant_id != scope for ref in refs):
        return [], (
            f"错误：存在不属于当前租户的任务，已拒绝{verb}"
            "（code=task_foreign_tenant）"
        )
    return refs, None


class _IdOrNameTool(Tool):
    """cancel/suspend/resume 共用参数面：任务 id（或其前缀）与名称二选一。"""

    parameters = {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "任务 ID 或其前缀（至少8位）",
            },
            "name": {
                "type": "string",
                "description": "任务名称",
            },
        },
    }

    def __init__(self, service: ScheduleManager) -> None:
        self._service = service


class CancelScheduleTool(_IdOrNameTool):
    name = "cancel_schedule"
    description = "取消定时任务。可按任务 ID 或名称取消"

    async def execute(self, **kwargs: Any) -> str:
        job_id = kwargs.get("id", "")
        name = kwargs.get("name", "")
        scope = _user_scope(kwargs)

        if not job_id and not name:
            return "错误：id 或 name 至少提供一个"

        refs, refusal = await _resolve_targets(
            self._service, job_id=job_id, name=name, scope=scope, verb="取消"
        )
        if refusal:
            return refusal
        if job_id:
            cancelled = await self._service.cancel_jobs(
                [ref.id for ref in refs], tenant_id=scope
            )
            return f"已取消 {len(cancelled)} 个任务"
        cancelled = await self._service.cancel_by_name(name, tenant_id=scope)
        return f"已取消 {len(cancelled)} 个名为 {name!r} 的任务"


class SuspendScheduleTool(_IdOrNameTool):
    name = "suspend_schedule"
    description = (
        "暂停定时任务：保留计划与归属，暂停期间不产生新执行；"
        "之后可用 resume_schedule 恢复"
    )

    async def execute(self, **kwargs: Any) -> str:
        return await _dispose_jobs(self._service, verb="暂停", **kwargs)


class ResumeScheduleTool(_IdOrNameTool):
    name = "resume_schedule"
    description = (
        "恢复已暂停的定时任务：按 misfire 规则从到期态继续（宽限内补执行，"
        "超宽限记 missed/skipped，不静默丢弃）"
    )

    async def execute(self, **kwargs: Any) -> str:
        return await _dispose_jobs(self._service, verb="恢复", **kwargs)


async def _dispose_jobs(service: ScheduleManager, *, verb: str, **kwargs: Any) -> str:
    """suspend/resume 的公共处置：命中 → 归属校验 → 逐个迁移状态。"""
    job_id = kwargs.get("id", "")
    name = kwargs.get("name", "")
    scope = _user_scope(kwargs)
    if not job_id and not name:
        return "错误：id 或 name 至少提供一个"

    refs, refusal = await _resolve_targets(
        service, job_id=job_id, name=name, scope=scope, verb=verb
    )
    if refusal:
        return refusal
    dispose = service.suspend_job if verb == "暂停" else service.resume_job
    disposed: list[str] = []
    for ref in refs:
        if await dispose(ref.id, tenant_id=scope):
            disposed.append(ref.id)
    if not disposed:
        # 取消/封禁是终态：处置失败不谎报成功。
        return f"未{verb}任何任务（已取消或已封禁的任务不可{verb}）"
    done = "已暂停" if verb == "暂停" else "已恢复"
    return f"{done} {len(disposed)} 个任务"
