"""Outbound delivery worker（C2）：outbox intent 的独立投递执行器。

职责边界（openspec/changes/c2-durable-control-plane/design.md ADR-5，
PILOT_ROADMAP §5.9.11）：

- 以数据库 lease 认领 outbox（`pending/failed` 到期重试 + stale `attempting`
  接管），冻结初始参数 `lease_ttl=60s`、heartbeat `20s`、最多 5 次 attempt、
  退避 `1m/5m/30m/2h/6h`，最终 `dead_letter`；均为可配置初始值，不是成功保证。
- **「模型生成完成 ≠ channel 已送达」**：worker 只消费执行完成事务创建的
  intent，不生成、不改写 canonical final message；`sent` 只能由发送回调的
  成功确认推进。
- at-least-once：失败按退避重试；租约丢失（心跳失败或 CAS 失败）时放弃本次
  attempt 的任何状态写入，由接管者收束，防止双 worker 重复推进。
- 发送通道以注入的 `send_callback` 接入；本模块不改接既有单体
  `MessageBus.dispatch_outbound`（切换归后续 change）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from bootstrap.db.repository.control_plane_repo import DeliveryRepository, LeaseLostError

logger = logging.getLogger(__name__)

__all__ = [
    "DeliveryEnvelope",
    "DeliveryWorkerConfig",
    "OutboundDeliveryWorker",
]


@dataclass(frozen=True)
class DeliveryWorkerConfig:
    """§5.9.11 冻结的 Pilot 初始运行参数（可配置，不是业务成功保证）。"""

    lease_ttl_seconds: float = 60.0
    heartbeat_interval_seconds: float = 20.0
    max_attempts: int = 5
    backoff_seconds: tuple[float, ...] = (60.0, 300.0, 1800.0, 7200.0, 21600.0)
    poll_interval_seconds: float = 1.0
    batch_size: int = 10

    def __post_init__(self) -> None:
        if self.lease_ttl_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("lease_ttl_seconds 必须大于 heartbeat_interval_seconds")
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须 >= 1")
        if len(self.backoff_seconds) < self.max_attempts:
            raise ValueError("backoff_seconds 长度必须覆盖 max_attempts")


@dataclass(frozen=True)
class DeliveryEnvelope:
    """交给发送回调的一次投递任务（来自认领到的 intent 行）。"""

    intent_id: str
    tenant_id: str
    conversation_id: str
    message_id: str
    turn_id: str | None
    idempotency_key: str
    channel: str
    target_chat_id: str
    payload: dict[str, object] = field(default_factory=dict)
    attempt_count: int = 0


DeliverySendCallback = Callable[[DeliveryEnvelope], Awaitable[str | None]]
"""发送回调：成功返回 provider receipt（可为 None）；失败应当抛异常。"""


class OutboundDeliveryWorker:
    """数据库 lease 认领 outbox 并投递；独立记录 attempt/provider receipt/时间戳。"""

    def __init__(
        self,
        repository: DeliveryRepository,
        send_callback: DeliverySendCallback,
        *,
        config: DeliveryWorkerConfig | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._repo = repository
        self._send = send_callback
        self._cfg = config or DeliveryWorkerConfig()
        self._owner = worker_id or f"delivery-worker:{uuid.uuid4().hex[:8]}"
        self._running = False

    @property
    def owner(self) -> str:
        return self._owner

    async def run(self) -> None:
        """持续轮询认领并投递，直到 `stop()`。"""
        self._running = True
        try:
            while self._running:
                processed = await self.process_once()
                if processed == 0:
                    await asyncio.sleep(self._cfg.poll_interval_seconds)
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    async def process_once(self) -> int:
        """单轮：认领一批到期 intent 并投递，返回本轮认领数量（测试/调度入口）。"""
        claimed = await self._repo.claim_batch(
            self._owner,
            batch_size=self._cfg.batch_size,
            lease_ttl_seconds=self._cfg.lease_ttl_seconds,
            max_attempts=self._cfg.max_attempts,
        )
        if not claimed:
            return 0
        results = await asyncio.gather(
            *(self._process(intent) for intent in claimed),
            return_exceptions=True,
        )
        for intent, result in zip(claimed, results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "delivery attempt 处理异常 intent=%s owner=%s: %r",
                    intent["id"],
                    self._owner,
                    result,
                )
        return len(claimed)

    async def _process(self, intent: dict[str, Any]) -> None:
        intent_id = str(intent["id"])
        tenant_id = str(intent["tenant_id"])
        payload_raw = intent.get("payload")
        payload: dict[str, Any] = dict(payload_raw) if isinstance(payload_raw, dict) else {}
        attempt_raw = intent.get("attempt_count")
        envelope = DeliveryEnvelope(
            intent_id=intent_id,
            tenant_id=tenant_id,
            conversation_id=str(intent["conversation_id"]),
            message_id=str(intent["message_id"]),
            turn_id=str(intent["turn_id"]) if intent.get("turn_id") else None,
            idempotency_key=str(intent["idempotency_key"]),
            channel=str(intent["channel"]),
            target_chat_id=str(intent["target_chat_id"]),
            payload=payload,
            attempt_count=int(attempt_raw) if isinstance(attempt_raw, int) else 0,
        )
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(tenant_id, intent_id, lease_lost),
            name=f"delivery-heartbeat:{intent_id}",
        )
        try:
            try:
                receipt = await self._send(envelope)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if lease_lost.is_set():
                    logger.warning(
                        "发送失败且租约已失，放弃本次 attempt（不写状态）intent=%s",
                        intent_id,
                    )
                    return
                try:
                    recorded = await self._repo.record_attempt_failed(
                        tenant_id,
                        uuid.UUID(intent_id),
                        self._owner,
                        str(exc),
                        max_attempts=self._cfg.max_attempts,
                        backoff_seconds=self._cfg.backoff_seconds,
                    )
                except LeaseLostError:
                    logger.warning(
                        "记录失败时租约已失，放弃本次 attempt（不写状态）intent=%s",
                        intent_id,
                    )
                    return
                logger.info(
                    "delivery attempt 失败 intent=%s status=%s next=%s error=%s",
                    intent_id,
                    recorded["status"],
                    recorded["next_attempt_at"],
                    exc,
                )
                return
            if lease_lost.is_set():
                # 失租后即使拿到成功结果也不得写 sent（ADR-5）；接管者会重投。
                logger.warning(
                    "发送成功但租约已失，放弃 sent 推进（可能重复投递）intent=%s",
                    intent_id,
                )
                return
            try:
                await self._repo.record_attempt_sent(
                    tenant_id,
                    uuid.UUID(intent_id),
                    self._owner,
                    provider_receipt=receipt,
                )
            except LeaseLostError:
                logger.warning(
                    "推进 sent 时租约已失，放弃本次 attempt（不写状态）intent=%s",
                    intent_id,
                )
                return
            logger.info("delivery sent intent=%s owner=%s", intent_id, self._owner)
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _heartbeat_loop(
        self,
        tenant_id: str,
        intent_id: str,
        lease_lost: asyncio.Event,
    ) -> None:
        """按 `heartbeat_interval_seconds` 续租；续租失败即标记租约丢失。"""
        while True:
            await asyncio.sleep(self._cfg.heartbeat_interval_seconds)
            ok = await self._repo.heartbeat(
                tenant_id,
                uuid.UUID(intent_id),
                self._owner,
                lease_ttl_seconds=self._cfg.lease_ttl_seconds,
            )
            if not ok:
                lease_lost.set()
                logger.warning("delivery 租约已失 intent=%s owner=%s", intent_id, self._owner)
                return
