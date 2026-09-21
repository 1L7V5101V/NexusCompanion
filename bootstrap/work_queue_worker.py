"""Work queue worker（C15）：`background_work_items` 的 durable 消费者。

职责边界（openspec/changes/2026-09-20-c15-work-queue-consumer/design.md ADR-1..ADR-7）：

- 以数据库 lease 认领 work item（`queued` 到期 + stale `in_progress` 接管），冻结初始
  参数 `lease_ttl=60s`、heartbeat `20s`、最多 5 次业务失败、退避 `1m/5m/30m/2h/6h`；
- **同租户串行、跨租户并发**：认领到的每一项交给 C3 的 `TenantLaneRouter`
  （`work_kind` 决定 lane：`interactive` → `run_interactive`，其余 → `run_maintenance`）；
  维护类在交互类忙时抛 `MaintenanceDeferred`，转 `release_for_retry`（计时但不计失败）；
- **handler 两段式**（ADR-6 实现约束）：`execute()` 是长活（LLM/网络），**不得写库**、
  **不在任何事务内**；`persist(session, ...)` 是短事务写入，由 worker 在**与终态同一
  事务**内调用。这样既保住「副作用与终态同提交」的 effectively-once 提交侧，又不把
  秒~分钟级的长活塞进数据库事务（否则会占住行锁与连接池，并让 heartbeat 被行锁堵死）；
- 失租（心跳返回 False 或终态 CAS 失败）时放弃本次尝试的任何状态写入，由接管者收束；
- `stop()` 只阻止认领新 work 并 `close()` lane，**等待在途 handler 收束、不中途取消**
  （ADR-7）；未收束项交给下次启动的 `sweep_stale_leases`。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from agent.admission.lanes import (
    LaneClosedError,
    MaintenanceDeferred,
    TenantLaneRouter,
)
from bootstrap.db.repository.control_plane_repo import (
    LeaseLostError,
    WorkItemNotFoundError,
    WorkItemRepository,
)

logger = logging.getLogger(__name__)

__all__ = [
    "WorkHandler",
    "WorkItemEnvelope",
    "WorkQueueWorker",
    "WorkQueueWorkerConfig",
]


@dataclass(frozen=True)
class WorkQueueWorkerConfig:
    """冻结的 Pilot 初始运行参数（可配置，不是业务成功保证）。"""

    lease_ttl_seconds: float = 60.0
    heartbeat_interval_seconds: float = 20.0
    max_attempts: int = 5
    backoff_seconds: tuple[float, ...] = (60.0, 300.0, 1800.0, 7200.0, 21600.0)
    poll_interval_seconds: float = 1.0
    batch_size: int = 10
    maintenance_acquire_timeout_seconds: float = 5.0
    release_delay_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.lease_ttl_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("lease_ttl_seconds 必须大于 heartbeat_interval_seconds")
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须 >= 1")
        if len(self.backoff_seconds) < self.max_attempts:
            raise ValueError("backoff_seconds 长度必须覆盖 max_attempts")
        if self.batch_size < 1:
            raise ValueError("batch_size 必须 >= 1")
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须为正")


@dataclass(frozen=True)
class WorkItemEnvelope:
    """交给 handler 的一次工作项（来自认领到的行）。"""

    work_item_id: str
    tenant_id: str
    work_kind: str
    flow: str | None
    idempotency_key: str | None
    conversation_id: str | None
    payload: dict[str, object] = field(default_factory=dict)
    attempt_count: int = 0


class WorkHandler(Protocol):
    """work item handler 契约（ADR-6）。

    必须**两段式**，因为长活（LLM / 网络，秒~分钟级）不能放进数据库事务：

    - ``execute``：长活，**不得写库**，返回任意结果对象；
    - ``persist``：短事务写入；由 worker 在**与终态同一事务**内调用，
      因此「DB 副作用」与「work item 终态」要么都在、要么都不在。

    两段合起来必须**幂等或可重放**（ADR-3 边界）：崩溃后同一 work 会被重新执行。
    """

    async def execute(self, envelope: WorkItemEnvelope) -> object: ...

    async def persist(
        self,
        session: AsyncSession,
        envelope: WorkItemEnvelope,
        result: object,
    ) -> None: ...


WorkHandlerMap = Mapping[str, "WorkHandler"]
"""`flow → handler` 映射；`flow` 决定 handler，`work_kind` 只决定 lane（ADR-2/ADR-5）。"""


class WorkQueueWorker:
    """数据库 lease 认领 work item 并在 tenant lane 内执行。"""

    def __init__(
        self,
        repository: WorkItemRepository,
        handlers: WorkHandlerMap,
        *,
        config: WorkQueueWorkerConfig | None = None,
        worker_id: str | None = None,
        router: TenantLaneRouter | None = None,
    ) -> None:
        self._repo = repository
        self._handlers = handlers
        self._cfg = config or WorkQueueWorkerConfig()
        self._owner = worker_id or f"work-queue:{uuid.uuid4().hex[:8]}"
        self._router = router or TenantLaneRouter()
        self._running = False

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def router(self) -> TenantLaneRouter:
        return self._router

    async def run(self) -> None:
        """持续轮询认领并执行，直到 `stop()`。

        `process_once()` 内部 `gather` 了本轮全部在途 handler，因此循环退出时本轮
        已收束；**调用方应调 `stop()` 而非 cancel**，否则在途 handler 会被取消（ADR-7）。
        """
        self._running = True
        try:
            while self._running:
                processed = await self.process_once()
                if processed == 0:
                    await asyncio.sleep(self._cfg.poll_interval_seconds)
        finally:
            self._running = False

    def stop(self) -> None:
        """停止认领新 work（并关闭 lane）；在途 handler 继续执行至收束。"""
        self._running = False
        self._router.close()

    async def process_once(self) -> int:
        """单轮：认领一批并执行，返回本轮认领数量（测试/调度入口）。"""
        if self._router.closed:
            return 0
        claimed = await self._repo.claim_batch(
            self._owner,
            batch_size=self._cfg.batch_size,
            lease_ttl_seconds=self._cfg.lease_ttl_seconds,
        )
        if not claimed:
            return 0
        results = await asyncio.gather(
            *(self._process(item) for item in claimed),
            return_exceptions=True,
        )
        for item, result in zip(claimed, results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "work item 处理异常 work_item_id=%s owner=%s: %r",
                    item["id"],
                    self._owner,
                    result,
                )
        return len(claimed)

    # ── 单项处理 ──

    async def _process(self, item: dict[str, Any]) -> None:
        envelope = _build_envelope(item)
        handler = self._handlers.get(envelope.flow or "")

        if handler is None:
            # 未注册 flow：不得执行。按业务失败处理，使其最终进入死信可见（而非静默丢弃，
            # 也不是无限释放把审计流写爆）；修复后可由 redrive 重投。
            await self._fail(
                envelope, f"未注册 flow: {envelope.flow!r}（handler 缺失）"
            )
            return

        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(envelope.tenant_id, envelope.work_item_id, lease_lost),
            name=f"work-heartbeat:{envelope.work_item_id}",
        )
        started_at = datetime.now(UTC)
        try:
            # 必须惰性创建协程：lane 可能延后（`MaintenanceDeferred`）或拒绝
            # （`LaneClosedError`）而不调用它，提前建协程会留下未 await 的警告。
            async def run() -> None:
                await self._run_handler(envelope, handler, started_at, lease_lost)

            try:
                if envelope.work_kind == "interactive":
                    await self._router.run_interactive(
                        envelope.tenant_id, envelope.work_item_id, run
                    )
                else:
                    await self._router.run_maintenance(
                        envelope.tenant_id,
                        envelope.work_item_id,
                        run,
                        acquire_timeout=self._cfg.maintenance_acquire_timeout_seconds,
                    )
            except MaintenanceDeferred:
                # 维护类被延后：不是失败——不消耗尝试预算，留 released 痕迹后重排（ADR-5）
                await self._release(envelope, "maintenance 延后（interactive 占优）")
            except LaneClosedError:
                # 正在关闭：不写业务失败，交给下次启动的清扫（ADR-7）
                logger.info(
                    "lane 已关闭，放弃本次执行（留给启动清扫）work_item_id=%s",
                    envelope.work_item_id,
                )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _run_handler(
        self,
        envelope: WorkItemEnvelope,
        handler: WorkHandler,
        started_at: datetime,
        lease_lost: asyncio.Event,
    ) -> None:
        """两段式：长活（无事务）→ 短事务（persist + 终态，ADR-6）。"""
        try:
            result = await handler.execute(envelope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if lease_lost.is_set():
                logger.warning(
                    "handler 失败且租约已失，放弃本次 attempt（不写状态）work_item_id=%s",
                    envelope.work_item_id,
                )
                return
            await self._fail(envelope, f"handler execute 失败: {type(exc).__name__}: {exc}")
            return

        if lease_lost.is_set():
            logger.warning(
                "handler 成功但租约已失，放弃终态推进（可能重复执行）work_item_id=%s",
                envelope.work_item_id,
            )
            return

        async def mutate(session: AsyncSession) -> None:
            await handler.persist(session, envelope, result)

        try:
            await self._repo.record_work_succeeded(
                envelope.tenant_id,
                envelope.work_item_id,
                self._owner,
                mutate=mutate,
                started_at=started_at,
            )
        except LeaseLostError:
            logger.warning(
                "推进终态时租约已失，放弃本次 attempt（不写状态）work_item_id=%s",
                envelope.work_item_id,
            )
        except WorkItemNotFoundError:
            logger.warning(
                "推进终态时 work item 已不存在 work_item_id=%s", envelope.work_item_id
            )
        except Exception as exc:
            # persist 失败：终态与副作用已整体回滚，按业务失败计入预算
            if lease_lost.is_set():
                logger.warning(
                    "persist 失败且租约已失，放弃本次 attempt（不写状态）work_item_id=%s",
                    envelope.work_item_id,
                )
                return
            await self._fail(
                envelope, f"handler persist 失败: {type(exc).__name__}: {exc}"
            )
        else:
            logger.info(
                "work item succeeded work_item_id=%s owner=%s",
                envelope.work_item_id,
                self._owner,
            )

    async def _fail(self, envelope: WorkItemEnvelope, error: str) -> None:
        try:
            await self._repo.record_work_failed(
                envelope.tenant_id,
                envelope.work_item_id,
                self._owner,
                error,
                max_attempts=self._cfg.max_attempts,
                backoff_seconds=self._cfg.backoff_seconds,
            )
        except (LeaseLostError, WorkItemNotFoundError) as exc:
            logger.warning(
                "记录失败时租约已失或行不存在，放弃本次 attempt（不写状态）"
                "work_item_id=%s: %r",
                envelope.work_item_id,
                exc,
            )
            return
        logger.info(
            "work item failed work_item_id=%s error=%s",
            envelope.work_item_id,
            error,
        )

    async def _release(self, envelope: WorkItemEnvelope, note: str) -> None:
        try:
            await self._repo.release_for_retry(
                envelope.tenant_id,
                envelope.work_item_id,
                self._owner,
                delay_seconds=self._cfg.release_delay_seconds,
                note=note,
            )
        except (LeaseLostError, WorkItemNotFoundError) as exc:
            logger.warning(
                "释放延后时租约已失或行不存在，放弃本次 attempt（不写状态）"
                "work_item_id=%s: %r",
                envelope.work_item_id,
                exc,
            )
            return
        logger.info(
            "work item released（延后）work_item_id=%s note=%s",
            envelope.work_item_id,
            note,
        )

    async def _heartbeat_loop(
        self,
        tenant_id: str,
        work_item_id: str,
        lease_lost: asyncio.Event,
    ) -> None:
        """按 `heartbeat_interval_seconds` 续租；续租失败即标记租约丢失。"""
        while True:
            await asyncio.sleep(self._cfg.heartbeat_interval_seconds)
            ok = await self._repo.heartbeat(
                tenant_id,
                work_item_id,
                self._owner,
                lease_ttl_seconds=self._cfg.lease_ttl_seconds,
            )
            if not ok:
                lease_lost.set()
                logger.warning(
                    "work item 租约已失 work_item_id=%s owner=%s",
                    work_item_id,
                    self._owner,
                )
                return


def _build_envelope(item: dict[str, Any]) -> WorkItemEnvelope:
    payload_raw = item.get("payload")
    payload: dict[str, object] = dict(payload_raw) if isinstance(payload_raw, dict) else {}
    attempt_raw = item.get("attempt_count")
    conversation_id = item.get("conversation_id")
    flow = item.get("flow")
    return WorkItemEnvelope(
        work_item_id=str(item["id"]),
        tenant_id=str(item["tenant_id"]),
        work_kind=str(item["work_kind"]),
        flow=str(flow) if flow else None,
        idempotency_key=(
            str(item["idempotency_key"]) if item.get("idempotency_key") else None
        ),
        conversation_id=str(conversation_id) if conversation_id else None,
        payload=payload,
        attempt_count=int(attempt_raw) if isinstance(attempt_raw, int) else 0,
    )
