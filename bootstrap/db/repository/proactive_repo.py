from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.models.proactive import (
    ContextOnlyTimestampModel,
    DeliveryModel,
    SessionStateModel,
    TickLogModel,
    TickStepLogModel,
)


class AsyncProactiveRepository:
    """Async CRUD for proactive state tables, scoped by tenant_id."""

    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    # ── deliveries ───────────────────────────────────────────

    async def record_delivery(
        self,
        tenant_id: str,
        session_key: str,
        delivery_key: str,
        sent_at: datetime,
    ) -> None:
        async with self._sf() as sess:
            row = DeliveryModel(
                tenant_id=tenant_id,
                session_key=session_key,
                delivery_key=delivery_key,
                sent_at=sent_at,
            )
            sess.add(row)
            try:
                await sess.commit()
            except Exception:
                await sess.rollback()

    async def is_delivery_duplicate(
        self,
        tenant_id: str,
        session_key: str,
        delivery_key: str,
    ) -> bool:
        async with self._sf() as sess:
            result = await sess.execute(
                select(func.count(DeliveryModel.delivery_key)).where(
                    DeliveryModel.tenant_id == tenant_id,
                    DeliveryModel.session_key == session_key,
                    DeliveryModel.delivery_key == delivery_key,
                )
            )
            return (result.scalar() or 0) > 0

    async def count_deliveries_in_window(
        self,
        tenant_id: str,
        session_key: str,
        window_start: datetime,
    ) -> int:
        async with self._sf() as sess:
            result = await sess.execute(
                select(func.count(DeliveryModel.delivery_key)).where(
                    DeliveryModel.tenant_id == tenant_id,
                    DeliveryModel.session_key == session_key,
                    DeliveryModel.sent_at >= window_start,
                )
            )
            return result.scalar() or 0

    async def list_deliveries(
        self,
        tenant_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
        session_key: str | None = None,
    ) -> tuple[list[dict], int]:
        async with self._sf() as sess:
            query = select(DeliveryModel).where(DeliveryModel.tenant_id == tenant_id)
            count_query = select(func.count(DeliveryModel.delivery_key)).where(
                DeliveryModel.tenant_id == tenant_id
            )

            if session_key:
                query = query.where(DeliveryModel.session_key == session_key)
                count_query = count_query.where(DeliveryModel.session_key == session_key)

            total = (await sess.execute(count_query)).scalar() or 0
            rows = (
                await sess.execute(
                    query.order_by(DeliveryModel.sent_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).scalars().all()
            return [
                {
                    "session_key": r.session_key,
                    "delivery_key": r.delivery_key,
                    "sent_at": r.sent_at.isoformat() if r.sent_at else "",
                }
                for r in rows
            ], total

    # ── session_state ────────────────────────────────────────

    async def get_session_state(
        self,
        tenant_id: str,
        session_key: str,
        state_key: str,
    ) -> str | None:
        async with self._sf() as sess:
            row = await sess.get(
                SessionStateModel, (session_key, state_key),
            )
            if row is None or row.tenant_id != tenant_id:
                return None
            return row.value

    async def set_session_state(
        self,
        tenant_id: str,
        session_key: str,
        state_key: str,
        value: str,
    ) -> None:
        async with self._sf() as sess:
            existing = await sess.get(
                SessionStateModel, (session_key, state_key),
            )
            if existing is not None and existing.tenant_id == tenant_id:
                existing.value = value
            else:
                sess.add(
                    SessionStateModel(
                        tenant_id=tenant_id,
                        session_key=session_key,
                        key=state_key,
                        value=value,
                    )
                )
            await sess.commit()

    # ── context_only_timestamps ──────────────────────────────

    async def mark_context_only_send(
        self,
        tenant_id: str,
        session_key: str,
        ts: datetime,
    ) -> None:
        async with self._sf() as sess:
            sess.add(
                ContextOnlyTimestampModel(
                    tenant_id=tenant_id,
                    session_key=session_key,
                    ts=ts,
                )
            )
            await sess.commit()

    # ── tick_log ─────────────────────────────────────────────

    async def record_tick_log_start(
        self,
        tenant_id: str,
        tick_id: str,
        session_key: str,
        started_at: datetime,
    ) -> None:
        async with self._sf() as sess:
            result = await sess.execute(
                select(TickLogModel).where(TickLogModel.tick_id == tick_id)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                return
            sess.add(
                TickLogModel(
                    tenant_id=tenant_id,
                    tick_id=tick_id,
                    session_key=session_key,
                    started_at=started_at,
                )
            )
            await sess.commit()

    async def record_tick_log_finish(
        self,
        tenant_id: str,
        tick_id: str,
        *,
        finished_at: datetime,
        gate_exit: str | None = None,
        terminal_action: str | None = None,
        skip_reason: str | None = None,
        steps_taken: int | None = None,
        alert_count: int | None = None,
        content_count: int | None = None,
        context_count: int | None = None,
        interesting_ids: str | None = None,
        discarded_ids: str | None = None,
        cited_ids: str | None = None,
        drift_entered: bool | None = None,
        final_message: str | None = None,
        proactive_effects_json: str | None = None,
    ) -> None:
        async with self._sf() as sess:
            existing = (
                await sess.execute(
                    select(TickLogModel).where(TickLogModel.tick_id == tick_id)
                )
            ).scalar_one_or_none()
            if existing is None or existing.tenant_id != tenant_id:
                return
            existing.finished_at = finished_at
            existing.gate_exit = gate_exit
            existing.terminal_action = terminal_action
            existing.skip_reason = skip_reason
            existing.steps_taken = steps_taken
            existing.alert_count = alert_count
            existing.content_count = content_count
            existing.context_count = context_count
            existing.interesting_ids = interesting_ids
            existing.discarded_ids = discarded_ids
            existing.cited_ids = cited_ids
            if drift_entered is not None:
                existing.drift_entered = 1 if drift_entered else 0
            existing.final_message = final_message
            existing.proactive_effects_json = proactive_effects_json
            await sess.commit()

    async def list_tick_logs(
        self,
        tenant_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
        session_key: str | None = None,
    ) -> tuple[list[dict], int]:
        async with self._sf() as sess:
            query = select(TickLogModel).where(TickLogModel.tenant_id == tenant_id)
            count_query = select(func.count(TickLogModel.id)).where(
                TickLogModel.tenant_id == tenant_id
            )

            if session_key:
                query = query.where(TickLogModel.session_key == session_key)
                count_query = count_query.where(TickLogModel.session_key == session_key)

            total = (await sess.execute(count_query)).scalar() or 0
            rows = (
                await sess.execute(
                    query.order_by(TickLogModel.started_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).scalars().all()

            return [_tick_log_to_dict(r) for r in rows], total

    async def get_tick_log(self, tenant_id: str, tick_log_id: int) -> dict | None:
        async with self._sf() as sess:
            row = await sess.get(TickLogModel, (tick_log_id,))
            if row is None or row.tenant_id != tenant_id:
                return None
            return _tick_log_to_dict(row)

    # ── tick_step_log ────────────────────────────────────────

    async def list_tick_steps(
        self,
        tenant_id: str,
        tick_id: str,
    ) -> list[dict]:
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(TickStepLogModel)
                    .where(
                        TickStepLogModel.tenant_id == tenant_id,
                        TickStepLogModel.tick_id == tick_id,
                    )
                    .order_by(TickStepLogModel.step_index.asc())
                )
            ).scalars().all()
            return [_tick_step_to_dict(r) for r in rows]


# ── helpers ─────────────────────────────────────────────────

def _tick_log_to_dict(row: TickLogModel) -> dict:
    return {
        "id": row.id,
        "tick_id": row.tick_id,
        "session_key": row.session_key,
        "started_at": row.started_at.isoformat() if row.started_at else "",
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "gate_exit": row.gate_exit,
        "terminal_action": row.terminal_action,
        "skip_reason": row.skip_reason,
        "steps_taken": row.steps_taken,
        "alert_count": row.alert_count,
        "content_count": row.content_count,
        "context_count": row.context_count,
        "interesting_ids": row.interesting_ids,
        "discarded_ids": row.discarded_ids,
        "cited_ids": row.cited_ids,
        "drift_entered": bool(row.drift_entered) if row.drift_entered is not None else None,
        "final_message": row.final_message,
        "proactive_effects_json": row.proactive_effects_json,
    }


def _tick_step_to_dict(row: TickStepLogModel) -> dict:
    return {
        "id": row.id,
        "tick_id": row.tick_id,
        "step_index": row.step_index,
        "phase": row.phase,
        "tool_name": row.tool_name,
        "tool_call_id": row.tool_call_id,
        "tool_args_json": row.tool_args_json,
        "tool_result_text": row.tool_result_text,
        "terminal_action_after": row.terminal_action_after,
        "skip_reason_after": row.skip_reason_after,
        "interesting_ids_after": row.interesting_ids_after,
        "discarded_ids_after": row.discarded_ids_after,
        "cited_ids_after": row.cited_ids_after,
        "final_message_after": row.final_message_after,
    }
