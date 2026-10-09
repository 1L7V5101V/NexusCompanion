"""C11 显式用户 schedule 的 PostgreSQL 仓储（openspec/changes/c11-explicit-schedules ADR-1/2/3）。

与 C2 control plane 同构的**三段事务**：

- **认领事务** `claim_due_jobs()` —— 到期 job 行锁扫描 + execution 插入 + 调度态前进，
  一个事务内原子完成；`(job_id, scheduled_for)` 唯一约束是重复触发的兜底。
  时间语义（下一 occurrence 怎么算）由调用方以 `advance` 回调传入：仓储只拥有事务与
  记账，不复制 `agent/scheduler.py` 的 cron/interval 计算（tasks 3.3）。
- **事务外副作用** —— instant 直投内容、soft 的 `process_direct` AI 调用，绝不持 DB 事务。
- **收束事务** `complete_execution()` —— canonical assistant message（取号）+ durable
  `turn.completed` 重放帧 + pending `outbound_delivery_intents`
  （幂等键 `sched:<execution_id>`）+ execution 终态 + job `last_outcome`，同一事务。

投递只按 C2 的 outbox intent 模式消费：本模块对 `outbound_delivery_intents` schema
零改动，既有 delivery worker 按 `channel` 路由认领，C11 是新写入方。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Text, and_, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.models.canonical import (
    CanonicalConversationModel,
    CanonicalMessageModel,
    TestAccountModel,
)
from bootstrap.db.models.control_plane import OutboundDeliveryIntentModel
from bootstrap.db.models.schedule import (
    ScheduleExecutionModel,
    ScheduledJobModel,
)
from bootstrap.db.repository.control_plane_repo import record_replay_frame
from bootstrap.schedule_defaults import DEFAULT_MISFIRE_GRACE_SECONDS
from infra.channels.web_chat_protocol import turn_completed

logger = logging.getLogger(__name__)

__all__ = [
    "ClaimedExecution",
    "ScheduleBindingError",
    "ScheduleExecutionNotFoundError",
    "ScheduleRepository",
    "ScheduleTransitionError",
]

# 收束事务的 assistant message metadata 标记（§5.9.14：执行可解释）。
_SCHEDULER_SOURCE = "scheduler"


class ScheduleError(Exception):
    """schedule 仓储错误基类。"""


class ScheduleBindingError(ScheduleError):
    """服务端无法解析出可信投递归属（无 canonical conversation / binding 缺失）。

    创建必须拒绝而非落半成品行——工具层把它映射为 ``code=schedule_no_delivery_binding``。
    """


class ScheduleExecutionNotFoundError(ScheduleError, LookupError):
    """目标 execution 不存在。"""


class ScheduleTransitionError(ScheduleError):
    """状态推进前提不成立（如对 revoked job resume、收束已终态的 execution）。"""


@dataclass(frozen=True)
class ClaimedExecution:
    """认领到的一条待执行 occurrence，连同执行它所需的 job 快照。

    只返回 `running` occurrence；`missed`/`skipped` 在认领事务内直落终态、不交给
    调用方（它们没有副作用可做）。
    """

    execution_id: uuid.UUID
    job_id: uuid.UUID
    tenant_id: str
    scheduled_for: datetime
    job: dict[str, Any]


def _to_json(data: Any) -> str | None:
    return json.dumps(data, ensure_ascii=False) if data is not None else None


def _from_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_utc(value: datetime) -> datetime:
    """PG TIMESTAMPTZ 往返值一律归一到 UTC-aware（interval/cron 计算的前置）。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _job_to_dict(row: ScheduledJobModel, *, run_count: int = 0) -> dict[str, Any]:
    spec = _from_json(row.schedule_spec_json)
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "account_id": str(row.account_id),
        "conversation_id": str(row.conversation_id),
        "name": row.name,
        "trigger": row.trigger_kind,
        "tier": row.tier,
        "when": spec.get("when", ""),
        "cron_expr": spec.get("cron_expr"),
        "interval_seconds": spec.get("interval_seconds"),
        "advance_minutes": spec.get("advance_minutes"),
        "message": row.message,
        "prompt": row.prompt,
        "timezone": row.timezone,
        "revision": row.revision,
        "delivery_channel": row.delivery_channel,
        "delivery_target": row.delivery_target,
        "status": row.status,
        # 展示用「下次触发时间」；None = 无待执行（one-shot 已收束 / recurring 停摆）。
        "fire_at": row.next_scheduled_for,
        "last_outcome": row.last_outcome,
        "run_count": run_count,
        "created_at": row.created_at,
    }


def _execution_to_dict(row: ScheduleExecutionModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "job_id": str(row.job_id),
        "tenant_id": row.tenant_id,
        "scheduled_for": row.scheduled_for,
        "status": row.status,
        "attempt_count": row.attempt_count,
        "skip_reason": row.skip_reason,
        "error": row.error,
        "schedule_timezone": row.schedule_timezone,
        "schedule_revision": row.schedule_revision,
        "delivery_intent_id": str(row.delivery_intent_id) if row.delivery_intent_id else None,
        "delivery_message_id": str(row.delivery_message_id) if row.delivery_message_id else None,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "created_at": row.created_at,
    }


class ScheduleRepository:
    """显式用户 schedule 的 durable 读写面（PG 专用；非 PG 后端不构造本类）。"""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        *,
        misfire_grace_seconds: int = DEFAULT_MISFIRE_GRACE_SECONDS,
    ) -> None:
        self._sf = session_factory
        self._grace = timedelta(seconds=misfire_grace_seconds)

    # ── 创建与查询（tasks 2.1）────────────────────────────────────────────

    async def create_job(
        self,
        *,
        tenant_id: str,
        trigger: str,
        tier: str,
        when: str,
        fire_at: datetime,
        timezone: str,
        delivery_channel: str,
        delivery_target: str,
        message: str | None = None,
        prompt: str | None = None,
        name: str | None = None,
        interval_seconds: int | None = None,
        cron_expr: str | None = None,
        advance_minutes: int | None = None,
    ) -> dict[str, Any]:
        """落一行 job：owner 三元组解析（fail-closed）+ 服务端 binding 冻结。

        `delivery_channel`/`delivery_target` 由调用方从服务端上下文组装后传入并**冻结**
        在行上（§5.9.14：模型/客户端不可提交授权目标）；`account_id`/`conversation_id`
        由 tenant 的 canonical conversation 派生，Pilot 每 tenant 恰一个会话。
        """
        if not delivery_channel or not delivery_target:
            raise ScheduleBindingError(
                f"缺少服务端解析的 delivery binding（tenant_id={tenant_id!r}）"
            )
        async with self._sf() as sess, sess.begin():
            conversation = (
                await sess.execute(
                    select(CanonicalConversationModel).where(
                        CanonicalConversationModel.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if conversation is None:
                # 不区分「tenant 不存在」与「无会话」：都不落 job，不泄露归属。
                raise ScheduleBindingError(
                    f"tenant 无 canonical conversation，拒绝创建（fail-closed）: "
                    f"tenant_id={tenant_id!r}"
                )
            row = ScheduledJobModel(
                tenant_id=tenant_id,
                account_id=conversation.account_id,
                conversation_id=conversation.id,
                name=name,
                trigger_kind=trigger,
                tier=tier,
                schedule_spec_json=_to_json(
                    {
                        "when": when,
                        "cron_expr": cron_expr,
                        "interval_seconds": interval_seconds,
                        "advance_minutes": advance_minutes,
                    }
                ),
                message=message,
                prompt=prompt,
                timezone=timezone,
                revision=1,
                delivery_channel=delivery_channel,
                delivery_target=delivery_target,
                status="active",
                next_scheduled_for=_as_utc(fire_at),
            )
            sess.add(row)
            await sess.flush()
            return _job_to_dict(row)

    async def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """按租户/状态列 job；`tenant_id=None` 为全量（dev/owner/admin 视图）。"""
        stmt = select(ScheduledJobModel)
        if tenant_id is not None:
            stmt = stmt.where(ScheduledJobModel.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(ScheduledJobModel.status == status)
        stmt = stmt.order_by(ScheduledJobModel.next_scheduled_for.asc().nulls_last())
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._sf() as sess:
            rows = list((await sess.execute(stmt)).scalars().all())
            counts: dict[uuid.UUID, int] = {}
            if rows:
                succeeded = (
                    await sess.execute(
                        select(
                            ScheduleExecutionModel.job_id,
                            func.count(ScheduleExecutionModel.id),
                        )
                        .where(
                            ScheduleExecutionModel.job_id.in_([r.id for r in rows]),
                            ScheduleExecutionModel.status == "succeeded",
                        )
                        .group_by(ScheduleExecutionModel.job_id)
                    )
                ).all()
                counts = {job_id: int(total) for job_id, total in succeeded}
            return [_job_to_dict(r, run_count=counts.get(r.id, 0)) for r in rows]

    async def get_job(
        self, job_id: uuid.UUID | str, *, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        row = await self._get_job_row(_coerce_uuid(job_id), tenant_id=tenant_id)
        return _job_to_dict(row) if row is not None else None

    async def list_job_refs(
        self, *, id_prefix: str = "", name: str = ""
    ) -> list[dict[str, Any]]:
        """候选任务的最小归属面（id/name/tenant_id），供 cancel 的跨租户拒绝判定。

        工具层需要「前缀/名称命中了谁的 job」才能给出 ``code=task_foreign_tenant``，
        但不应拿到全量规格——因此只投影归属三列。
        """
        stmt = select(
            ScheduledJobModel.id, ScheduledJobModel.name, ScheduledJobModel.tenant_id
        )
        if id_prefix:
            # LIKE 元字符转义：前缀按字面匹配（legacy 用的是 str.startswith 同语义）。
            escaped = (
                id_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            stmt = stmt.where(cast(ScheduledJobModel.id, Text).like(f"{escaped}%"))
        if name:
            stmt = stmt.where(ScheduledJobModel.name == name)
        async with self._sf() as sess:
            return [
                {"id": str(job_id), "name": job_name, "tenant_id": tenant}
                for job_id, job_name, tenant in (await sess.execute(stmt)).all()
            ]

    async def set_job_status(
        self,
        job_id: uuid.UUID | str,
        new_status: str,
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        """suspend/resume/revoke 处置；每次状态推进 `revision +1`（计划版本语义）。

        `revoked` 是终态：不可 resume（禁用 ≠ 暂停）。suspended 期间 `next_scheduled_for`
        原样保留，恢复后由 tick 按 misfire 规则判定补执行/记账。
        """
        if new_status not in ("active", "suspended", "revoked"):
            raise ScheduleTransitionError(f"非法 job 状态: {new_status!r}")
        coerced = _coerce_uuid(job_id)
        if coerced is None:
            return None
        async with self._sf() as sess, sess.begin():
            row = (
                await sess.execute(
                    select(ScheduledJobModel)
                    .where(ScheduledJobModel.id == coerced)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if tenant_id is not None and row.tenant_id != tenant_id:
                # 跨租户：不泄露存在性，与 get_job/cancel 同语义。
                return None
            if row.status == new_status:
                return _job_to_dict(row)
            if row.status == "revoked":
                raise ScheduleTransitionError(
                    f"revoked job 不可恢复: job_id={row.id}"
                )
            row.status = new_status
            row.revision += 1
            row.updated_at = datetime.now(UTC)
            await sess.flush()
            return _job_to_dict(row)

    async def revoke_jobs_by_account(self, account_id: uuid.UUID | str) -> int:
        """账号封禁联动：该账号全部未完成 job 置 `revoked`（返回处置条数）。"""
        coerced = _coerce_uuid(account_id)
        if coerced is None:
            return 0
        async with self._sf() as sess, sess.begin():
            rows = (
                await sess.execute(
                    select(ScheduledJobModel)
                    .where(
                        ScheduledJobModel.account_id == coerced,
                        ScheduledJobModel.status != "revoked",
                    )
                    .with_for_update()
                )
            ).scalars().all()
            now = datetime.now(UTC)
            for row in rows:
                row.status = "revoked"
                row.revision += 1
                row.updated_at = now
            return len(rows)

    async def cancel_jobs(
        self,
        job_ids: list[str],
        *,
        tenant_id: str | None = None,
    ) -> list[str]:
        """取消（禁用并清空待执行态）：job 行保留，execution 留痕不删（只追加表）。

        与 legacy 的「删除」差异在工具返回值上不可见（同样报「已取消 N 个」）；
        保留行是为了 execution 的外键留痕可解释（ADR-1）。
        """
        cancelled: list[str] = []
        async with self._sf() as sess, sess.begin():
            for raw in job_ids:
                coerced = _coerce_uuid(raw)
                if coerced is None:
                    continue
                row = (
                    await sess.execute(
                        select(ScheduledJobModel)
                        .where(ScheduledJobModel.id == coerced)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    continue
                if tenant_id is not None and row.tenant_id != tenant_id:
                    continue
                if row.status == "revoked":
                    continue
                row.status = "revoked"
                row.next_scheduled_for = None
                row.revision += 1
                row.updated_at = datetime.now(UTC)
                cancelled.append(str(row.id))
        return cancelled

    # ── 认领与收束（tasks 2.2 / design ADR-3）─────────────────────────────

    async def claim_due_jobs(
        self,
        now: datetime,
        *,
        advance: Callable[[dict[str, Any], datetime], datetime | None],
        soft_lead_seconds: float = 0.0,
        exclude_job_ids: Collection[str] = (),
        limit: int = 50,
    ) -> list[ClaimedExecution]:
        """到期认领：一个事务内插入 execution + 前进调度态，返回待执行 occurrence。

        `advance(job_dict, baseline)` 由调用方提供时间语义（cron/interval），
        仓储只负责把它写回 `next_scheduled_for`。分支（ADR-3）：

        - 账号 `suspended`（含 provisioning/failed 等非 active）→ **不插 execution、
          不前进**：job 停留在到期态，下个 tick 重查，避免逐 tick skip 刷屏；
        - 账号 `revoked` → job 置 revoked，不产生 execution；
        - one-shot 超 grace → `missed` + `next_scheduled_for=NULL`，无副作用；
        - recurring 超 grace → `skipped(recurring_advance)` + 前进到 `advance(now)`，
          **不枚举中间 occurrence**（§10 DECIDED：不回放全部 missed）；
        - grace 内 → `running` 认领；recurring 当场前进到 `advance(max(now, scheduled_for))`，
          one-shot 清空 `next_scheduled_for`（该 occurrence 已被占用，重复认领由
          `(job_id, scheduled_for)` 唯一约束兜底）。

        `soft_lead_seconds` 复刻 legacy 的 SOFT 预触发：soft tier 的名义 `scheduled_for`
        可以还在未来（提前 P90 起算 AI），到期判据按 tier 分别取阈值。
        `exclude_job_ids` 排除进程内仍在途的 job（同一 job 不并发执行）。
        """
        now = _as_utc(now)
        soft_threshold = now + timedelta(seconds=max(0.0, soft_lead_seconds))
        claimed: list[ClaimedExecution] = []
        async with self._sf() as sess, sess.begin():
            stmt = (
                select(ScheduledJobModel, TestAccountModel.status)
                .join(
                    TestAccountModel,
                    TestAccountModel.id == ScheduledJobModel.account_id,
                )
                .where(
                    ScheduledJobModel.status == "active",
                    ScheduledJobModel.next_scheduled_for.is_not(None),
                    or_(
                        and_(
                            ScheduledJobModel.tier == "soft",
                            ScheduledJobModel.next_scheduled_for <= soft_threshold,
                        ),
                        and_(
                            ScheduledJobModel.tier != "soft",
                            ScheduledJobModel.next_scheduled_for <= now,
                        ),
                    ),
                )
            )
            excluded = [cid for cid in (_coerce_uuid(raw) for raw in exclude_job_ids) if cid]
            if excluded:
                stmt = stmt.where(~ScheduledJobModel.id.in_(excluded))
            stmt = (
                stmt.order_by(ScheduledJobModel.next_scheduled_for)
                .limit(limit)
                .with_for_update(of=ScheduledJobModel, skip_locked=True)
            )
            rows = (await sess.execute(stmt)).all()
            for job, account_status in rows:
                scheduled_for = _as_utc(job.next_scheduled_for)
                overdue = now - scheduled_for > self._grace
                recurring = job.trigger_kind == "every"

                if account_status == "revoked":
                    job.status = "revoked"
                    job.revision += 1
                    job.updated_at = now
                    continue
                if account_status != "active":
                    # 挂起语义：保持到期态等待恢复，不写 execution（ADR-3）。
                    continue

                if overdue and recurring:
                    # 跳过边界必须落到**严格未来**：cron 的 get_next_fire_time 对
                    # 整点当刻是闭区间（after=12:00:00 → 12:00:00），用 now 作基准会
                    # 原地不前进、下个 tick 立即重认领同一时刻。
                    nxt = advance(_job_to_dict(job), now + timedelta(microseconds=1))
                    await self._insert_terminal(
                        sess,
                        job,
                        scheduled_for=scheduled_for,
                        status="skipped",
                        skip_reason="recurring_advance",
                        now=now,
                    )
                    job.next_scheduled_for = nxt
                    job.last_outcome = "skipped"
                    continue
                if overdue:
                    await self._insert_terminal(
                        sess,
                        job,
                        scheduled_for=scheduled_for,
                        status="missed",
                        skip_reason="misfire_grace_exceeded",
                        now=now,
                    )
                    job.next_scheduled_for = None
                    job.last_outcome = "missed"
                    continue

                execution_id = uuid.uuid4()
                inserted = (
                    await sess.execute(
                        pg_insert(ScheduleExecutionModel)
                        .values(
                            id=execution_id,
                            job_id=job.id,
                            tenant_id=job.tenant_id,
                            scheduled_for=scheduled_for,
                            status="running",
                            attempt_count=1,
                            schedule_timezone=job.timezone,
                            schedule_revision=job.revision,
                            started_at=now,
                        )
                        # 同一 occurrence 已被认领（并发 tick / 崩溃重放）→ 不报错、
                        # 不派发第二次副作用；调度态照常前进（ADR-3 幂等兜底）。
                        .on_conflict_do_nothing(
                            index_elements=["job_id", "scheduled_for"]
                        )
                        .returning(ScheduleExecutionModel.id)
                    )
                ).scalar_one_or_none()
                # 快照必须在改调度态**之前**取：交给执行方的 `job` 描述的是这次
                # occurrence 当时的计划（含 fire_at=名义触发时刻），不是认领后的残值。
                job_snapshot = _job_to_dict(job)
                if recurring:
                    baseline = max(now, scheduled_for) + timedelta(microseconds=1)
                    job.next_scheduled_for = advance(job_snapshot, baseline)
                else:
                    job.next_scheduled_for = None
                job.updated_at = now
                if inserted is None:
                    logger.info(
                        "[schedule] occurrence 已被认领，跳过副作用: job=%s scheduled_for=%s",
                        job.id,
                        scheduled_for.isoformat(),
                    )
                    continue
                claimed.append(
                    ClaimedExecution(
                        execution_id=execution_id,
                        job_id=job.id,
                        tenant_id=job.tenant_id,
                        scheduled_for=scheduled_for,
                        job=job_snapshot,
                    )
                )
        return claimed

    async def _insert_terminal(
        self,
        sess: Any,
        job: ScheduledJobModel,
        *,
        scheduled_for: datetime,
        status: str,
        skip_reason: str,
        now: datetime,
    ) -> None:
        """missed/skipped 直落终态（不经历 running），每条都带原因、不静默删除。"""
        sess.add(
            ScheduleExecutionModel(
                id=uuid.uuid4(),
                job_id=job.id,
                tenant_id=job.tenant_id,
                scheduled_for=scheduled_for,
                status=status,
                attempt_count=0,
                skip_reason=skip_reason,
                schedule_timezone=job.timezone,
                schedule_revision=job.revision,
                finished_at=now,
            )
        )
        job.updated_at = now

    async def complete_execution(
        self,
        execution_id: uuid.UUID | str,
        *,
        content: str,
    ) -> dict[str, Any]:
        """收束事务：assistant message（取号）+ 重放帧 + pending intent + 终态，单事务原子。

        与 C2 `complete_turn_with_delivery` 同形；差别是归属由 job 行给定、无 turn
        （`turn_id=NULL`，幂等键 `sched:<execution_id>` 一对一）。sequence/message/
        frame/intent/execution/job 六者原子，任一步失败整体回滚（execution 留在
        `running`，由恢复扫描收束为 `failed`）。
        """
        coerced = _require_uuid(execution_id, "execution_id")
        async with self._sf() as sess, sess.begin():
            execution = (
                await sess.execute(
                    select(ScheduleExecutionModel)
                    .where(ScheduleExecutionModel.id == coerced)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if execution is None:
                raise ScheduleExecutionNotFoundError(f"execution 不存在: {coerced}")
            if execution.status != "running":
                raise ScheduleTransitionError(
                    f"execution 非 running，不可收束: id={coerced} status={execution.status!r}"
                )
            job = (
                await sess.execute(
                    select(ScheduledJobModel)
                    .where(ScheduledJobModel.id == execution.job_id)
                    .with_for_update()
                )
            ).scalar_one()

            allocated = (
                await sess.execute(
                    update(CanonicalConversationModel)
                    .where(
                        CanonicalConversationModel.id == job.conversation_id,
                        CanonicalConversationModel.tenant_id == job.tenant_id,
                    )
                    .values(
                        next_sequence=CanonicalConversationModel.next_sequence + 1
                    )
                    .returning(CanonicalConversationModel.next_sequence - 1)
                )
            ).scalar_one_or_none()
            if allocated is None:
                raise ScheduleBindingError(
                    f"canonical conversation 不存在: job_id={job.id} "
                    f"tenant_id={job.tenant_id!r}"
                )

            message_id = uuid.uuid4()
            sess.add(
                CanonicalMessageModel(
                    id=message_id,
                    tenant_id=job.tenant_id,
                    conversation_id=job.conversation_id,
                    sequence=allocated,
                    role="assistant",
                    content=content,
                    metadata_json=_to_json(
                        {
                            "source": _SCHEDULER_SOURCE,
                            "job_id": str(job.id),
                            "execution_id": str(execution.id),
                            "scheduled_for": execution.scheduled_for.isoformat(),
                            "trigger": job.trigger_kind,
                            "tier": job.tier,
                        }
                    ),
                )
            )
            await sess.flush()

            # WebChat 投递读重放帧（WebchatDeliveryAdapter），缺帧会退避到死信；
            # 因此与 intent 同事务写 turn.completed 帧，seq 复用 C2 的唯一取号实现。
            # 无 turn 的 schedule 输出用 sched:<execution_id> 作前端幂等键。
            await record_replay_frame(
                sess,
                job.tenant_id,
                job.conversation_id,
                frame_type="turn.completed",
                frame=turn_completed(
                    turn_id=f"sched:{execution.id}",
                    content=content,
                ),
                message_id=message_id,
            )

            intent_id = uuid.uuid4()
            sess.add(
                OutboundDeliveryIntentModel(
                    id=intent_id,
                    tenant_id=job.tenant_id,
                    conversation_id=job.conversation_id,
                    message_id=message_id,
                    turn_id=None,
                    idempotency_key=f"sched:{execution.id}",
                    channel=job.delivery_channel,
                    target_chat_id=job.delivery_target,
                    payload_json=None,
                    status="pending",
                )
            )
            await sess.flush()

            now = datetime.now(UTC)
            execution.status = "succeeded"
            execution.delivery_intent_id = intent_id
            execution.delivery_message_id = message_id
            execution.finished_at = now
            job.last_outcome = "succeeded"
            return _execution_to_dict(execution)

    async def fail_execution(
        self, execution_id: uuid.UUID | str, *, error: str
    ) -> dict[str, Any]:
        """执行失败终态（不产生 message/intent；recurring 的推进已在认领时完成）。"""
        return await self._terminalize(
            execution_id, status="failed", error=error, skip_reason=None
        )

    async def skip_execution(
        self, execution_id: uuid.UUID | str, *, skip_reason: str
    ) -> dict[str, Any]:
        """触发时 fail-closed 重验不过（RevocationGate / binding 失效）：已认领的
        running occurrence 收束为 `skipped` 并记录原因，零投递副作用。"""
        return await self._terminalize(
            execution_id, status="skipped", error=None, skip_reason=skip_reason
        )

    async def _terminalize(
        self,
        execution_id: uuid.UUID | str,
        *,
        status: str,
        error: str | None,
        skip_reason: str | None,
    ) -> dict[str, Any]:
        coerced = _require_uuid(execution_id, "execution_id")
        async with self._sf() as sess, sess.begin():
            execution = (
                await sess.execute(
                    select(ScheduleExecutionModel)
                    .where(ScheduleExecutionModel.id == coerced)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if execution is None:
                raise ScheduleExecutionNotFoundError(f"execution 不存在: {coerced}")
            if execution.status != "running":
                raise ScheduleTransitionError(
                    f"execution 非 running: id={coerced} status={execution.status!r}"
                )
            job = (
                await sess.execute(
                    select(ScheduledJobModel)
                    .where(ScheduledJobModel.id == execution.job_id)
                    .with_for_update()
                )
            ).scalar_one()
            now = datetime.now(UTC)
            execution.status = status
            execution.error = error
            execution.skip_reason = skip_reason
            execution.finished_at = now
            job.last_outcome = status
            return _execution_to_dict(execution)

    # ── 恢复与查询（tasks 2.3）────────────────────────────────────────────

    async def sweep_interrupted_executions(self) -> list[dict[str, Any]]:
        """启动恢复扫描：崩溃残留的 `running` → `failed(interrupted_by_restart)`。

        不重放副作用（ADR-4）：recurring 的下一 occurrence 已在认领时写入，one-shot
        的 `next_scheduled_for` 已清空，因此本方法只收束 execution 终态；同一
        occurrence 之后不会被重复认领（唯一约束 + 调度态已前进）。
        """
        async with self._sf() as sess, sess.begin():
            rows = (
                await sess.execute(
                    select(ScheduleExecutionModel)
                    .where(ScheduleExecutionModel.status == "running")
                    .with_for_update()
                )
            ).scalars().all()
            now = datetime.now(UTC)
            swept: list[dict[str, Any]] = []
            for execution in rows:
                execution.status = "failed"
                execution.error = "interrupted_by_restart"
                execution.finished_at = now
                swept.append(_execution_to_dict(execution))
            if swept:
                logger.info("[schedule] 恢复扫描收束 %s 条中断 execution", len(swept))
            return swept

    async def list_executions(
        self,
        *,
        status: str | None = None,
        tenant_id: str | None = None,
        job_id: uuid.UUID | str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """执行留痕查询（admin misfire 面与恢复共用；只追加表，无删除入口）。"""
        stmt = select(ScheduleExecutionModel)
        if status is not None:
            stmt = stmt.where(ScheduleExecutionModel.status == status)
        if tenant_id is not None:
            stmt = stmt.where(ScheduleExecutionModel.tenant_id == tenant_id)
        if job_id is not None:
            coerced = _coerce_uuid(job_id)
            if coerced is None:
                return []
            stmt = stmt.where(ScheduleExecutionModel.job_id == coerced)
        stmt = stmt.order_by(ScheduleExecutionModel.scheduled_for.desc()).limit(limit)
        async with self._sf() as sess:
            return [
                _execution_to_dict(r) for r in (await sess.execute(stmt)).scalars().all()
            ]

    async def _get_job_row(
        self, job_id: uuid.UUID | None, *, tenant_id: str | None
    ) -> ScheduledJobModel | None:
        if job_id is None:
            return None
        stmt = select(ScheduledJobModel).where(ScheduledJobModel.id == job_id)
        if tenant_id is not None:
            stmt = stmt.where(ScheduledJobModel.tenant_id == tenant_id)
        async with self._sf() as sess:
            return (await sess.execute(stmt)).scalar_one_or_none()


def _coerce_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _require_uuid(value: uuid.UUID | str, what: str) -> uuid.UUID:
    coerced = _coerce_uuid(value)
    if coerced is None:
        raise ValueError(f"{what} 需要有效 UUID: {value!r}")
    return coerced
