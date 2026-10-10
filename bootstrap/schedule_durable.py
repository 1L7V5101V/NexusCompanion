"""C11 durable 显式 schedule 调度服务（openspec/changes/c11-explicit-schedules ADR-3/4）。

与 legacy `SchedulerService`（JSON store + 内存 task）实现同一个 `ScheduleManager`
契约，由 `storage.backend` 选择装配：

- **启动恢复扫描**先行：崩溃残留的 `running` execution 一律 `failed`
  （`interrupted_by_restart`），不重放副作用；调度态在认领时已前进，所以同一
  occurrence 不会被重复执行。
- **1s tick** 只做认领 + 派发，AI 调用与投递收束在独立任务里，不阻塞 tick。
  单轮异常就地吞掉并退避——本服务的 `run()` 位于 `AppRuntime` 的 primary task 集合
  （`asyncio.gather` 一损俱损），外抛会连带拖垮出站与被动循环。
- **触发时 fail-closed 链**（ADR-2）：账号状态（认领事务内）→ `RevocationGate` →
  Telegram binding 仍 active。任一不过即 `skipped` 并记录原因，零投递副作用。
- **投递不自己实现**：收束事务写 canonical message + pending `outbound_delivery_intents`
  （幂等键 `sched:<execution_id>`），既有 delivery worker 按 channel 路由认领。

时间计算全部复用 `agent/scheduler.py`（`compute_actual_trigger`/`next_cron_fire`/
`LatencyTracker`），本模块不复制第二套 cron/interval 实现。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from agent.admission.revocation import RevocationGate, RevocationRejected
from agent.scheduler import (
    JobRef,
    LatencyTracker,
    ScheduleDeliveryUnresolvedError,
    ScheduledJob,
    compute_actual_trigger,
    next_cron_fire,
)
from bootstrap.db.repository.schedule_repo import (
    ClaimedExecution,
    ScheduleBindingError,
    ScheduleRepository,
    ScheduleTransitionError,
)
from bootstrap.db.repository.telegram_repo import TelegramBindingRepository
from bootstrap.schedule_defaults import (
    DEFAULT_MISFIRE_GRACE_SECONDS,
    DEFAULT_TICK_INTERVAL_SECONDS,
)

logger = logging.getLogger(__name__)

__all__ = ["DurableSchedulerService", "build_durable_scheduler"]

# soft 路径生成内容前的工具面：与 legacy 一致（提醒内容不得自己再推送/读写记忆）。
_SOFT_DISABLED_TOOLS = [
    "message_push",
    "recall_memory",
    "memorize",
    "forget_memory",
]


class DurableSchedulerService:
    """PG 后端的显式用户 schedule 服务（实现 `ScheduleManager` 契约）。"""

    def __init__(
        self,
        *,
        session_factory: Any,
        engine: AsyncEngine | None = None,
        misfire_grace_seconds: int = DEFAULT_MISFIRE_GRACE_SECONDS,
        agent_loop_provider: Callable[[], Any] | None = None,
        revocation_gate: RevocationGate | None = None,
        telegram_channel_name: str | None = None,
        tracker: LatencyTracker | None = None,
        tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS,
        _now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self.repo = ScheduleRepository(
            session_factory, misfire_grace_seconds=misfire_grace_seconds
        )
        self.tracker = tracker or LatencyTracker()
        self._agent_loop_provider = agent_loop_provider
        self._revocation_gate = revocation_gate
        self._telegram_channel_name = telegram_channel_name
        self._bindings = (
            TelegramBindingRepository(session_factory) if telegram_channel_name else None
        )
        self._tick_interval = tick_interval_seconds
        self._now = _now_fn or (lambda: datetime.now(UTC))
        # 进程内在途 job id：同一 job 不并发执行（legacy 的 _in_flight 同语义）。
        self._in_flight: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._running = False

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        """恢复扫描 → 每秒 tick（接口与 legacy `SchedulerService.run()` 对齐）。"""
        swept = await self.repo.sweep_interrupted_executions()
        self._running = True
        logger.info(
            "DurableSchedulerService started（恢复扫描收束 %s 条中断执行）", len(swept)
        )
        while self._running:
            await asyncio.sleep(self._tick_interval)
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[schedule] tick 失败，下个周期重试")

    def stop(self) -> None:
        self._running = False

    async def aclose(self) -> None:
        """停 tick + 释放本服务的独立连接池（由装配方在 CoreRuntime.stop 里调用）。"""
        self._running = False
        if self._engine is not None:
            await self._engine.dispose()

    # ── tick ──────────────────────────────────────────────────────────────

    async def _tick_once(self) -> None:
        now = self._now()
        claimed = await self.repo.claim_due_jobs(
            now,
            advance=self._advance,
            soft_lead_seconds=self._soft_lead_seconds(),
            exclude_job_ids=self._in_flight,
        )
        for item in claimed:
            self._in_flight.add(str(item.job_id))
            task = asyncio.create_task(
                self._run_claimed(item), name=f"schedule:{item.execution_id}"
            )
            self._tasks.add(task)
            task.add_done_callback(self._discard_task)

    def _discard_task(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        # 任务内已记账；异常在此消费掉，避免 asyncio 的 "exception never retrieved"。
        if not task.cancelled() and task.exception() is not None:
            logger.exception(
                "[schedule] 执行任务内部异常", exc_info=task.exception()
            )

    def _soft_lead_seconds(self) -> float:
        """SOFT 预触发提前量：向 `compute_actual_trigger` 要值，不重述 `fire_at - lead`。"""
        probe = self._now()
        lead = (
            probe - compute_actual_trigger(probe, "soft", self.tracker)
        ).total_seconds()
        return max(0.0, lead)

    async def _run_claimed(self, item: ClaimedExecution) -> None:
        try:
            await self._execute(item)
        except asyncio.CancelledError:
            # 进程退出：execution 留在 running，下次启动的恢复扫描收束（不补投）。
            logger.info(
                "[schedule] 执行被中断（保留 running 待恢复扫描）: execution=%s",
                item.execution_id,
            )
            raise
        except Exception as exc:
            logger.exception("[schedule] 执行失败: job=%s", item.job_id)
            await self._safe_fail(item, error=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            self._in_flight.discard(str(item.job_id))

    async def _execute(self, item: ClaimedExecution) -> None:
        job = item.job
        tenant_id = str(job["tenant_id"])

        if not await self._gate_open(tenant_id, action="scheduler_execute"):
            await self.repo.skip_execution(
                item.execution_id, skip_reason="revocation_gate"
            )
            return
        if not await self._binding_active(job):
            await self.repo.skip_execution(
                item.execution_id, skip_reason="binding_inactive"
            )
            return

        if job["tier"] == "instant":
            content = str(job.get("message") or "")
            if not content.strip():
                await self.repo.fail_execution(
                    item.execution_id, error="empty_message"
                )
                return
        else:
            content = await self._generate_soft(job)
            # soft 的 AI 调用可能长达数十秒，落库前再验一次（C7 触发前重校验锚点）。
            if not await self._gate_open(tenant_id, action="scheduler_soft_execute"):
                await self.repo.skip_execution(
                    item.execution_id, skip_reason="revocation_gate"
                )
                return
            if not content.strip():
                await self.repo.fail_execution(
                    item.execution_id, error="empty_ai_response"
                )
                return

        await self.repo.complete_execution(item.execution_id, content=content)
        logger.info(
            "[schedule] 触发完成 job=%s tier=%s execution=%s",
            job.get("name") or str(job["id"])[:8],
            job["tier"],
            item.execution_id,
        )

    async def _generate_soft(self, job: dict[str, Any]) -> str:
        loop = self._agent_loop_provider() if self._agent_loop_provider else None
        if loop is None:
            raise RuntimeError("durable schedule soft job requires agent_loop")
        t0 = time.monotonic()
        content = await loop.process_direct(
            content=job.get("prompt"),
            channel=job["delivery_channel"],
            chat_id=job["delivery_target"],
            session_key=f"scheduler:{job['id']}",
            busy_session_key=f"{job['delivery_channel']}:{job['delivery_target']}",
            omit_user_turn=True,
            skip_post_memory=True,
            skip_memory_retrieval=True,
            disabled_tools=list(_SOFT_DISABLED_TOOLS),
        )
        elapsed = time.monotonic() - t0
        self.tracker.record(elapsed)
        logger.info(
            "[schedule] soft AI 完成 job=%s 耗时=%.1fs P90=%.1fs",
            job.get("name") or str(job["id"])[:8],
            elapsed,
            self.tracker.lead,
        )
        return str(content or "")

    async def _gate_open(self, tenant_id: str, *, action: str) -> bool:
        if self._revocation_gate is None:
            return True
        try:
            await self._revocation_gate.check(tenant_id, action=action)
        except RevocationRejected:
            logger.info("[schedule] revocation 拒绝执行: tenant=%s action=%s", tenant_id, action)
            return False
        return True

    async def _binding_active(self, job: dict[str, Any]) -> bool:
        """Telegram 渠道的冻结 binding 必须仍然 active（解绑后不再推送，ADR-2 ③）。"""
        if self._bindings is None or job["delivery_channel"] != self._telegram_channel_name:
            return True
        binding = await self._bindings.get_active_binding_by_account(job["account_id"])
        return binding is not None

    async def _safe_fail(self, item: ClaimedExecution, *, error: str) -> None:
        try:
            row = await self.repo.fail_execution(item.execution_id, error=error)
            logger.info(
                "[schedule] 失败已记账: execution=%s status=%s",
                item.execution_id,
                row["status"],
            )
        except Exception:
            # 已是终态（或收束本身失败）→ 留给恢复扫描/下轮，不再上抛。
            logger.warning(
                "[schedule] 失败终态写入未完成: execution=%s", item.execution_id
            )

    def _advance(self, job: dict[str, Any], after: datetime) -> datetime | None:
        """recurring 前进：cron 走 `next_cron_fire`，interval 走绝对推进（ADR-6）。"""
        cron_expr = job.get("cron_expr")
        if cron_expr:
            try:
                return next_cron_fire(str(cron_expr), str(job["timezone"]), after)
            except ValueError:
                logger.warning(
                    "[schedule] cron 无法解析未来 occurrence，job 停摆: job=%s expr=%r",
                    job["id"],
                    cron_expr,
                )
                return None
        base = job.get("fire_at")
        interval = timedelta(seconds=int(job.get("interval_seconds") or 3600))
        if base is None:
            return after + interval
        if base.tzinfo is None:
            base = base.replace(tzinfo=UTC)
        nxt = base + interval
        while nxt <= after:
            nxt += interval
        return nxt

    # ── ScheduleManager 实现（工具面）────────────────────────────────────

    async def create_job(self, job: ScheduledJob) -> ScheduledJob:
        """落 PG job 行：owner 三元组由 tenant 解析，binding 取服务端注入值并冻结。

        无 tenant 归属（非认证路径直调）或 tenant 无 canonical conversation →
        `ScheduleBindingError`，不落半成品行（ADR-2 fail-closed）。
        """
        if not job.owner_tenant_id:
            raise ScheduleDeliveryUnresolvedError(
                "durable schedule 需要服务端注入的 tenant 归属（principal=user 时由 "
                "ToolExecutionContext 提供）"
            )
        try:
            row = await self.repo.create_job(
                tenant_id=job.owner_tenant_id,
                trigger=job.trigger,
                tier=job.tier,
                when=job.when,
                fire_at=job.fire_at,
                timezone=job.timezone,
                delivery_channel=job.channel,
                delivery_target=job.chat_id,
                message=job.message,
                prompt=job.prompt,
                name=job.name,
                interval_seconds=job.interval_seconds,
                cron_expr=job.cron_expr,
                advance_minutes=job.advance_minutes,
            )
        except ScheduleBindingError as exc:
            # 仓储错误不外泄到 agent 层：工具面按 agent 侧异常映射拒绝码。
            raise ScheduleDeliveryUnresolvedError(str(exc)) from exc
        job.id = str(row["id"])
        job.owner_tenant_id = str(row["tenant_id"])
        return job

    async def fetch_jobs(self, *, tenant_id: str | None = None) -> list[ScheduledJob]:
        rows = await self.repo.list_jobs(tenant_id=tenant_id)
        return [
            self._to_dataclass(row)
            for row in rows
            if row["status"] == "active" and row["fire_at"] is not None
        ]

    async def find_job_refs(
        self, *, id_prefix: str = "", name: str = ""
    ) -> list[JobRef]:
        refs = await self.repo.list_job_refs(id_prefix=id_prefix, name=name)
        return [
            JobRef(id=r["id"], name=r["name"], owner_tenant_id=r["tenant_id"])
            for r in refs
        ]

    async def cancel_jobs(
        self, ids: Sequence[str], *, tenant_id: str | None = None
    ) -> list[str]:
        return await self.repo.cancel_jobs(list(ids), tenant_id=tenant_id)

    async def cancel_by_name(
        self, name: str, *, tenant_id: str | None = None
    ) -> list[str]:
        refs = await self.repo.list_job_refs(name=name)
        if tenant_id is not None:
            refs = [r for r in refs if r["tenant_id"] == tenant_id]
        return await self.repo.cancel_jobs([r["id"] for r in refs])

    async def suspend_job(self, job_id: str, *, tenant_id: str | None = None) -> bool:
        return await self._transition(job_id, "suspended", tenant_id=tenant_id)

    async def resume_job(self, job_id: str, *, tenant_id: str | None = None) -> bool:
        return await self._transition(job_id, "active", tenant_id=tenant_id)

    async def _transition(
        self, job_id: str, new_status: str, *, tenant_id: str | None
    ) -> bool:
        try:
            row = await self.repo.set_job_status(
                job_id, new_status, tenant_id=tenant_id
            )
        except ScheduleTransitionError:
            logger.info(
                "[schedule] 状态处置被拒: job=%s -> %s（revoked 为终态）", job_id, new_status
            )
            return False
        return row is not None

    @staticmethod
    def _to_dataclass(row: dict[str, Any]) -> ScheduledJob:
        """PG job 行 → 工具面共用的 `ScheduledJob` 视图（legacy 显示字段全对齐）。"""
        return ScheduledJob(
            trigger=row["trigger"],
            tier=row["tier"],
            fire_at=row["fire_at"],
            channel=row["delivery_channel"],
            chat_id=row["delivery_target"],
            interval_seconds=row["interval_seconds"],
            cron_expr=row["cron_expr"],
            message=row["message"],
            prompt=row["prompt"],
            name=row["name"],
            timezone=row["timezone"],
            when=row["when"],
            advance_minutes=row["advance_minutes"],
            owner_tenant_id=row["tenant_id"],
            run_count=int(row["run_count"]),
            enabled=row["status"] == "active",
            id=row["id"],
        )


def build_durable_scheduler(
    config: Any,
    *,
    agent_loop_provider: Callable[[], Any] | None = None,
    revocation_gate: RevocationGate | None = None,
) -> DurableSchedulerService:
    """按 `[storage].postgres_url` 构造独立 engine/pool 的 durable 调度服务（ADR-4）。

    与 `build_work_queue_runtime`、`build_webchat_durable_runtime` 同模式：本服务自持
    engine，`CoreRuntime.stop()` 经 `aclose()` 释放——不与 work queue 或 webchat 网关
    共享池，避免一侧的租约/退避排队拖累另一侧的到期认领。
    """
    from bootstrap.db.config import DatabaseConfig
    from bootstrap.db.engine import create_engine, create_session_factory
    from bootstrap.work_queue import async_pg_url

    engine = create_engine(
        DatabaseConfig(
            url=async_pg_url(config.storage.postgres_url),
            pool_size=config.storage.pool_size,
        )
    )
    telegram = getattr(getattr(config, "channels", None), "telegram", None)
    # C10 绑定同步未开启时没有 telegram_identity_bindings 可用，跳过 binding 重验
    # （旧单体 allowlist 路径的推送目标不由 binding 授权）。
    telegram_channel_name = (
        str(telegram.channel_name)
        if telegram is not None and getattr(telegram, "pilot_identity_binding", False)
        else None
    )
    return DurableSchedulerService(
        session_factory=create_session_factory(engine),
        engine=engine,
        misfire_grace_seconds=getattr(
            getattr(config, "scheduler", None),
            "misfire_grace_seconds",
            DEFAULT_MISFIRE_GRACE_SECONDS,
        ),
        agent_loop_provider=agent_loop_provider,
        revocation_gate=revocation_gate,
        telegram_channel_name=telegram_channel_name,
        tracker=LatencyTracker(),
    )
