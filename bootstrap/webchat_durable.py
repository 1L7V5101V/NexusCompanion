"""WebChat durable 接受网关（pg-durable-sot-cutover ADR-1/ADR-6）。

把 WS `send` 的接受序列落到 C2 durable control plane（多租户 PG 模式）：

1. **overload 预检**（`MessageBus.inbound_full()`）——§5.9.4/C4 契约：overload
   拒绝 SHALL 发生在 durable acceptance 之前，不消耗幂等键，客户端可原样重发；
2. **T1 入站接受事务**（`IngressRepository.accept_inbound`）——dedupe（账号 +
   client_message_id，部分唯一索引）+ canonical user message + inbox + queued
   turn + accepted 重放帧，单事务提交后才允许向客户端发 `message.accepted`；
3. **入队执行**（`publish_inbound_wait`）——T1 已提交，队列满时在界内等待槽位
   而非拒绝；等待超时属 durable acceptance 之后的执行失败，按 turn.failed 终态
   收束（不撤销已接受消息，见 design ADR-1）。

dev 回退（`agent.dev_mode` 且无认证）：identity 的 conversation_id 不是合法
UUID → `eligible()` 返回 False，通道走 legacy in-proc 路径（ADR-6，dev-only）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agent.config_models import Config
from bootstrap.db.config import DatabaseConfig
from bootstrap.db.engine import create_engine, create_session_factory
from bootstrap.db.repository.control_plane_repo import (
    DeliveryRepository,
    IngressRepository,
    NotFoundError,
    TurnControlRepository,
    WebchatReplayRepository,
)
from bootstrap.delivery_worker import OutboundDeliveryWorker
from bootstrap.work_queue import async_pg_url
from bus.events import InboundMessage
from bus.queue import MessageBus
from infra.channels.web_chat_channel import DurableSendOutcome
from infra.channels.web_chat_protocol import message_accepted, turn_failed

logger = logging.getLogger(__name__)

__all__ = [
    "WebchatDeliveryAdapter",
    "WebchatDeliveryLoop",
    "WebchatDurableGateway",
    "WebchatDurableRuntime",
    "WebchatDurableTurnFinisher",
    "build_webchat_durable_runtime",
]

# metadata 保留键：pg durable 身份贯穿（task 3.x 完成事务消费；`nexus_` 前缀
# 与既有 `nexus_error` 约定一致，不进 prompt、不参与授权）。
PG_TURN_ID_KEY = "nexus_pg_turn_id"
PG_TENANT_ID_KEY = "nexus_pg_tenant_id"
PG_INBOX_ID_KEY = "nexus_pg_inbox_id"
PG_MESSAGE_ID_KEY = "nexus_pg_message_id"
PG_SEQUENCE_KEY = "nexus_pg_sequence"
PG_CONVERSATION_ID_KEY = "nexus_pg_conversation_id"


def _is_uuid(value: str) -> bool:
    try:
        _ = uuid.UUID(value)
    except (TypeError, ValueError):
        return False
    return True


class WebchatDurableGateway:
    """实现通道侧 `DurableSendGateway` 协议（infra 定义，bootstrap 实现）。"""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        bus: MessageBus,
        *,
        channel_name: str = "chat",
        enqueue_wait_seconds: float = 30.0,
    ) -> None:
        self._ingress = IngressRepository(session_factory)
        self._turns = TurnControlRepository(session_factory)
        self._replay = WebchatReplayRepository(session_factory)
        self._bus = bus
        self._channel_name = channel_name
        self._enqueue_wait = enqueue_wait_seconds

    @staticmethod
    def eligible(identity: Any) -> bool:
        """durable 路径资格：服务端身份三元组为真实 C1 UUID（dev 回退排除）。"""
        return bool(
            _is_uuid(str(getattr(identity, "conversation_id", "") or ""))
            and _is_uuid(str(getattr(identity, "account_id", "") or ""))
            and bool(str(getattr(identity, "tenant_id", "") or "").strip())
        )

    async def hello_seq(self, identity: Any) -> int:
        """durable 重放水位（hello ``latest_seq``；task 5.1）。"""
        return await self._replay.current_seq(
            str(identity.tenant_id), str(identity.conversation_id)
        )

    async def replay_after(
        self, identity: Any, after_seq: int
    ) -> list[dict[str, Any]] | None:
        """durable 补拉（task 5.1）：``seq > after_seq`` 的帧按序返回。

        游标超出持久窗口（早于最旧保留帧或晚于当前水位）→ 返回 None，
        调用方按协议回 ``replay_required`` 由客户端经 REST 重建。
        """
        tenant_id = str(identity.tenant_id)
        conversation_id = str(identity.conversation_id)
        latest = await self._replay.current_seq(tenant_id, conversation_id)
        if after_seq > latest:
            return None
        if after_seq >= latest:
            return []
        oldest = await self._replay.oldest_seq(tenant_id, conversation_id)
        if oldest is None:
            # 水位 > 游标但窗口内无帧（retention 已清理）→ 无法保证连续补拉。
            return None
        if after_seq < oldest - 1:
            return None
        return await self._replay.frames_after(tenant_id, conversation_id, after_seq)

    async def accept_send(
        self,
        *,
        identity: Any,
        client_message_id: str,
        content: str,
        media: list[str],
        sender: str,
    ) -> DurableSendOutcome:
        tenant_id = str(identity.tenant_id)
        conversation_id = str(identity.conversation_id)
        ack_template = message_accepted(
            client_message_id=client_message_id,
            session_key=str(identity.session_key),
        )
        # 1. overload 预检：durable acceptance 之前拒绝，不消耗幂等键。
        if self._bus.inbound_full():
            return DurableSendOutcome(
                kind="overload",
                error_message="服务繁忙，请稍后重发这条消息。",
            )
        # 2. T1：单事务提交 dedupe + canonical user message + inbox + queued
        #    turn + accepted 重放帧；任一步失败整体回滚（客户端可原样重发）。
        try:
            result = await self._ingress.accept_inbound(
                tenant_id,
                conversation_id,
                account_id=identity.account_id,
                client_message_id=client_message_id,
                content=content,
                metadata={
                    "client_message_id": client_message_id,
                    "username": sender,
                    "nexus_media": [str(m) for m in media],
                },
                replay_frame=ack_template,
            )
        except NotFoundError as exc:
            # canonical conversation 缺失/租户不符：fail-closed，真实原因进日志。
            logger.error(
                "webchat durable accept 被拒绝: tenant=%s conv=%s: %s",
                tenant_id,
                conversation_id,
                exc,
            )
            return DurableSendOutcome(
                kind="error",
                error_message="服务暂时不可用，请稍后重发这条消息。",
            )
        except Exception as exc:  # 存储/网络瞬断等：不 ack、不缓存幂等。
            logger.error(
                "webchat durable accept 事务失败: tenant=%s conv=%s: %r",
                tenant_id,
                conversation_id,
                exc,
            )
            return DurableSendOutcome(
                kind="error",
                error_message="服务暂时不可用，请稍后重发这条消息。",
            )
        if result.duplicate:
            # 幂等成功路径：逐字重放原 accepted 帧（同 wire seq，ADR-3）。
            frame = result.replay_frame or (
                {**ack_template, "seq": result.replay_seq}
                if result.replay_seq is not None
                else None
            )
            if frame is None:
                return DurableSendOutcome(
                    kind="error",
                    error_message="服务暂时不可用，请稍后重发这条消息。",
                )
            return DurableSendOutcome(kind="duplicate", frame=frame)

        # 3. 入队执行（T1 已提交；满载在界内等待槽位，超时按失败终态收束）。
        inbound = InboundMessage(
            channel=self._channel_name,
            sender=sender,
            chat_id=str(identity.chat_id),
            content=content,
            media=[str(m) for m in media],
            metadata={
                "client_message_id": client_message_id,
                "username": sender,
                PG_TURN_ID_KEY: result.turn_id or "",
                PG_INBOX_ID_KEY: result.inbox_id,
                PG_MESSAGE_ID_KEY: result.message_id,
                PG_SEQUENCE_KEY: str(result.sequence),
                PG_CONVERSATION_ID_KEY: conversation_id,
            },
            tenant_id=tenant_id,
        )
        try:
            await asyncio.wait_for(
                self._bus.publish_inbound_wait(inbound), self._enqueue_wait
            )
        except asyncio.TimeoutError:
            await self._fail_queued_turn(
                identity,
                turn_id=result.turn_id,
                reason="overload_enqueue_timeout",
                detail="durable acceptance 后入队等待超时，turn 收束为失败",
            )
            return DurableSendOutcome(
                kind="overload",
                error_message="服务繁忙，请稍后重发这条消息。",
            )
        except Exception as exc:
            await self._fail_queued_turn(
                identity,
                turn_id=result.turn_id,
                reason="enqueue_failed",
                detail=str(exc),
            )
            return DurableSendOutcome(
                kind="error",
                error_message="服务暂时不可用，请稍后重发这条消息。",
            )
        frame = result.replay_frame or (
            {**ack_template, "seq": result.replay_seq}
            if result.replay_seq is not None
            else None
        )
        return DurableSendOutcome(kind="accepted", frame=frame)

    async def _fail_queued_turn(
        self,
        identity: Any,
        *,
        turn_id: str | None,
        reason: str,
        detail: str,
    ) -> None:
        """把仍处 queued 的 turn 收束为 failed（durable，不重新生成/不产 intent）。

        失败原因对日志/对账可见；wire 侧以 overload 错误帧提示客户端重发。
        """
        if not turn_id:
            return
        tenant_id = str(identity.tenant_id)
        try:
            await self._turns.transition_turn(
                tenant_id,
                turn_id,
                expected_status="queued",
                new_status="failed",
                error={"code": reason, "detail": detail},
                replay_frame=turn_failed(turn_id=turn_id, error=reason),
            )
            logger.warning(
                "webchat durable turn 收束失败终态 turn=%s reason=%s", turn_id, reason
            )
        except Exception as exc:  # 收束失败不影响错误帧返回；对账兜底可见。
            logger.error(
                "webchat durable turn 收束失败终态异常 turn=%s: %r", turn_id, exc
            )


class WebchatDurableTurnFinisher:
    """outbound 流终态收束器（pg-durable-sot-cutover task 3.1/3.2/3.3）。

    订阅 bus outbound（**先于通道订阅**，见 app 装配）：对携带 pg durable 元
    数据的 WebChat 出站做控制面终态写入，并把 durable 重放 seq 盖回出站
    metadata（``nexus_replay_seq``），通道随后以该 seq 发终态帧（wire 帧与
    durable 帧逐字一致）。

    镜像管线持久化事实（design ADR-2/task 3.1）：
    - 正常回复（AfterReasoning 已持久化 session）→ T2 ``complete_turn_with_
      delivery``：final canonical message + turn completed + pending intent
      + ``turn.completed`` 重放帧，同事务；
    - ``nexus_error``（abort/provider_error/loop 兜底：session 与 canonical 均
      无 final）→ ``transition_turn`` 失败终态 + ``turn.failed`` 重放帧，
      **不产投递意图**；
    - 终态后 ``mark_inbox_processed``（accepted→processed 收束，task 1.3）。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        *,
        channel_name: str = "chat",
        telemetry: Any = None,
    ) -> None:
        self._turns = TurnControlRepository(session_factory)
        self._ingress = IngressRepository(session_factory)
        self._channel_name = channel_name
        self._telemetry = telemetry
        self._wake: Callable[[], None] | None = None

    def bind_wake(self, wake: Callable[[], None]) -> None:
        """绑定 T2 提交后的 delivery 唤醒回调（task 4.1；未绑定为 no-op）。"""
        self._wake = wake

    def subscribe(self, bus: MessageBus) -> None:
        bus.subscribe_outbound(self._channel_name, self._on_outbound)

    async def _on_outbound(self, msg: Any) -> None:
        logger.info(
            "webchat durable finisher 收到出站: channel=%s meta_keys=%s",
            getattr(msg, "channel", "?"),
            sorted((msg.metadata or {}).keys())
            if isinstance(getattr(msg, "metadata", None), dict)
            else "<非 dict>",
        )
        try:
            await self._finish(msg)
        except Exception:
            logger.exception(
                "webchat durable 终态收束异常 turn=%s",
                (msg.metadata or {}).get("nexus_pg_turn_id")
                if isinstance(msg.metadata, dict)
                else "?",
            )

    async def _finish(self, msg: Any) -> None:
        metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        turn_id = str(metadata.get("nexus_pg_turn_id") or "")
        if not turn_id or msg.channel != self._channel_name:
            logger.info(
                "webchat durable 终态收束跳过: channel=%s expected=%s pg_turn_id=%s meta_keys=%s",
                msg.channel,
                self._channel_name,
                turn_id or "<空>",
                sorted(metadata),
            )
            return
        # 租户以 durable 链自身的键为准（control 路径的 outbound metadata 不含
        # tenant_id 下划线键——TurnNotFoundError 教训）。
        tenant_id = str(
            metadata.get("nexus_pg_tenant_id") or metadata.get("tenant_id") or ""
        ).strip()
        inbox_id = str(metadata.get("nexus_pg_inbox_id") or "")
        is_error = bool(metadata.get("nexus_error"))
        fail_reason = str(metadata.get("nexus_fail_reason") or "turn_failed")
        if is_error:
            # 失败终态：无 final message、无投递意图；原因与 wire 文案可查。
            row = await self._turns.transition_turn(
                tenant_id,
                turn_id,
                expected_status="queued",
                new_status="failed",
                error={"code": fail_reason, "detail": str(msg.content)[:500]},
                replay_frame=turn_failed(
                    turn_id=turn_id, error=str(msg.content)[:500]
                ),
            )
            replay_seq = row.get("replay_seq")
        else:
            result = await self._turns.complete_turn_with_delivery(
                tenant_id,
                str(metadata.get("nexus_pg_conversation_id") or ""),
                turn_id,
                expected_status="queued",
                response_content=str(msg.content),
                delivery_channel=self._channel_name,
                delivery_target=tenant_id,
                delivery_payload={
                    "turn_id": turn_id,
                    "media": [str(m) for m in (msg.media or [])],
                },
                message_metadata={
                    "thinking": msg.thinking,
                    "client_message_id": metadata.get("client_message_id"),
                },
                replay_frame={
                    "type": "turn.completed",
                    "seq": None,
                    "turn_id": turn_id,
                    "content": str(msg.content),
                    "thinking": msg.thinking,
                    "media": [str(m) for m in (msg.media or [])],
                },
            )
            replay_seq = result.replay_seq
        if inbox_id:
            await self._ingress.mark_inbox_processed(tenant_id, inbox_id)
        if replay_seq is not None:
            metadata["nexus_replay_seq"] = int(replay_seq)
        if not is_error and self._wake is not None:
            # T2 已产出 pending intent：唤醒 delivery worker 立即投递（task 4.1）。
            self._wake()
        # E10 turn 记录点（task 6.1；失败不阻断主流程）。
        if self._telemetry is not None:
            try:
                self._telemetry.turn_finished(
                    turn_id=turn_id,
                    tenant_id=tenant_id,
                    status="failed" if is_error else "completed",
                    channel=self._channel_name,
                    error=str(msg.content)[:200] if is_error else None,
                    error_type=(
                        str(metadata.get("nexus_fail_reason"))
                        if is_error
                        else None
                    ),
                )
            except Exception:
                logger.exception("turn lifecycle 记录点异常（不阻断主流程）")


class WebchatDeliveryAdapter:
    """delivery worker 的 WebChat 发送回调（task 4.1；ADR-4）。

    从 durable 重放帧表取 final 帧（含 seq，逐字 = 在线广播帧），投递到该
    canonical 会话的全部在线连接；前端按 turn_id 幂等渲染（store 覆盖语义），
    at-least-once 重复投递安全。**零在线连接 → 抛错**（spec：attempt 记录
    失败并按退避重试至 dead_letter；离线客户端重连后经补拉/REST 重建补齐）。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        channel: Any,
    ) -> None:
        self._replay = WebchatReplayRepository(session_factory)
        self._channel = channel

    async def __call__(self, envelope: Any) -> str | None:
        frame = await self._replay.frame_for_message(
            envelope.tenant_id,
            envelope.conversation_id,
            envelope.message_id,
            "turn.completed",
        )
        if frame is None:
            # 帧已被 retention 清理（C12 §8.4）：无法在线补投，按失败重试；
            # 客户端始终可经 REST 从 canonical 重建。
            raise RuntimeError(
                f"replay frame 缺失（retention?）: message_id={envelope.message_id}"
            )
        delivered = await self._channel.deliver_frame(
            str(envelope.conversation_id), frame
        )
        if delivered == 0:
            raise RuntimeError(
                "webchat 会话无在线连接: "
                f"conversation_id={envelope.conversation_id}"
            )
        return f"webchat:{delivered}"


class WebchatDeliveryLoop:
    """T2 提交事件唤醒 + 轮询兜底的 delivery worker 循环（task 4.1）。"""

    def __init__(
        self,
        worker: Any,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._worker = worker
        self._poll = poll_interval_seconds
        self._wake = asyncio.Event()
        self._running = False

    def wake(self) -> None:
        """T2 提交后立即唤醒（避免 final 帧等一个轮询周期）。"""
        self._wake.set()

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            self._wake.clear()
            if not self._running:
                break
            try:
                await self._worker.process_once()
            except Exception:
                logger.exception("webchat delivery 轮询异常（下一周期重试）")


async def reconcile_webchat_on_startup(
    runtime: Any,
    *,
    session_manager: Any,
) -> dict[str, Any]:
    # ``runtime`` 只消费 ``session_factory``（测试可用真实 factory 的轻量替身）。
    """启动对账（pg-durable-sot-cutover task 5.3；spec「启动对账与恢复」）。

    - 非终态 turn（queued/in_progress）→ 收束为 ``failed``（原因
      ``restart_reconciled``），不重新生成 final、不创建投递意图；
      同事务写 ``turn.failed`` 重放帧（重连补拉可见失败终态）；
    - 关联 inbox 收束（accepted→processed）；
    - 受影响会话的派生视图（session view）按 canonical 流全量重建
      （canonical 为唯一权威，双向分歧一并修复，§5.9.12 可重建派生物）；
    - 对账结果结构化返回并记日志（管理员可见，不静默丢弃）。
    """
    from bootstrap.db.repository.canonical_repo import (
        CanonicalMessageRepository,
    )

    turns_repo = TurnControlRepository(runtime.session_factory)
    ingress_repo = IngressRepository(runtime.session_factory)
    messages_repo = CanonicalMessageRepository(runtime.session_factory)

    pending = await turns_repo.list_non_terminal_turns()
    affected: dict[str, str] = {}  # conversation_id -> tenant_id
    reconciled = 0
    for turn in pending:
        tenant_id = str(turn["tenant_id"])
        turn_id = str(turn["id"])
        conversation_id = str(turn["conversation_id"])
        try:
            await turns_repo.transition_turn(
                tenant_id,
                turn_id,
                expected_status=str(turn["status"]),
                new_status="failed",
                error={
                    "code": "restart_reconciled",
                    "detail": "进程重启中断的执行收束为失败（不重新生成）",
                },
                replay_frame=turn_failed(
                    turn_id=turn_id, error="restart_reconciled"
                ),
            )
            reconciled += 1
            affected[conversation_id] = tenant_id
            inbox_id = turn.get("inbox_record_id")
            if inbox_id:
                await ingress_repo.mark_inbox_processed(tenant_id, inbox_id)
        except Exception:
            logger.exception(
                "启动对账收束 turn 失败（下轮重启重试）turn=%s", turn_id
            )

    rebuilt: list[str] = []
    for conversation_id, tenant_id in affected.items():
        session_key = f"chat:{tenant_id}"
        try:
            messages = await messages_repo.fetch_messages(
                tenant_id, conversation_id
            )
            storage = session_manager._view(tenant_id)
            _ = storage.delete_session(session_key, cascade=True)
            session = session_manager.get_or_create(tenant_id, session_key)
            replayed = [
                {
                    "role": str(m["role"]),
                    "content": str(m["content"] or ""),
                    "timestamp": str(m.get("created_at") or ""),
                }
                for m in messages
                if str(m["role"]) in ("user", "assistant")
            ]
            if replayed:
                await session_manager.append_messages(session, replayed)
            rebuilt.append(session_key)
        except Exception:
            logger.exception(
                "启动对账重建派生视图失败 conversation=%s", conversation_id
            )

    summary = {
        "pending_turns": len(pending),
        "reconciled_turns": reconciled,
        "rebuilt_sessions": rebuilt,
    }
    if reconciled or rebuilt:
        logger.warning("webchat 启动对账完成: %s", summary)
    return summary


@dataclass
class WebchatDurableRuntime:
    """durable 网关 + 独立 async engine（停机 cleanup 释放连接池）。"""

    engine: AsyncEngine
    session_factory: async_sessionmaker
    gateway: WebchatDurableGateway
    finisher: WebchatDurableTurnFinisher
    telemetry: Any = None
    delivery_loop: "WebchatDeliveryLoop | None" = None
    delivery_task: "asyncio.Task[None] | None" = None

    async def aclose(self) -> None:
        await self.engine.dispose()

    def start_delivery(self, channel: Any) -> None:
        """装配 delivery worker 循环并启动任务（需通道实例，二阶段调用）。

        通道与网关互为构造前置（通道要注入 gateway、适配器要读通道连接表），
        因此 delivery 装配独立于 runtime 构造，由 app 在通道建成后调用。
        """
        if self.delivery_task is not None:
            return
        telemetry = self.telemetry
        worker = OutboundDeliveryWorker(
            DeliveryRepository(self.session_factory),
            WebchatDeliveryAdapter(self.session_factory, channel),
            on_delivery_finished=(
                (lambda payload: telemetry.delivery_finished(
                    message_id=str(payload["intent"]["message_id"]),
                    turn_id=str(payload["intent"].get("turn_id")) or None,
                    tenant_id=str(payload["intent"]["tenant_id"]),
                    channel=str(payload["intent"]["channel"]),
                    result=str(payload["result"]),
                    attempt=int(payload["intent"].get("attempt_count") or 0),
                    error=payload.get("error"),
                ))
                if telemetry is not None
                else None
            ),
        )
        loop = WebchatDeliveryLoop(worker)
        self.finisher.bind_wake(loop.wake)
        self.delivery_loop = loop
        self.delivery_task = asyncio.create_task(
            loop.run(), name="webchat_delivery"
        )
        self.delivery_task.add_done_callback(self._delivery_done)
        logger.info("webchat delivery worker 已装配（wake+poll 混合循环）")

    def stop_delivery(self) -> None:
        if self.delivery_loop is not None:
            self.delivery_loop.stop()

    @staticmethod
    def _delivery_done(task: "asyncio.Task[None]") -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "webchat delivery 循环意外退出",
                exc_info=(type(error), error, error.__traceback__),
            )


def build_webchat_durable_runtime(
    config: Config,
    *,
    bus: MessageBus,
    channel_name: str,
) -> WebchatDurableRuntime | None:
    """按存储后端构造 durable 网关运行期；非 PostgreSQL 后端返回 None（ADR-6）。

    engine 与 work queue 运行期独立（各自池、各自生命周期）；URL/池参数与
    `build_work_queue_runtime` 同源（`[storage].postgres_url`）。
    """
    if config.storage.backend != "postgres":
        return None
    db_cfg = DatabaseConfig(
        url=async_pg_url(config.storage.postgres_url),
        pool_size=config.storage.pool_size,
    )
    engine = create_engine(db_cfg)
    session_factory = create_session_factory(engine)
    gateway = WebchatDurableGateway(
        session_factory,
        bus,
        channel_name=channel_name,
    )
    from bootstrap.webchat_telemetry import build_default_lifecycle_telemetry

    try:
        telemetry = build_default_lifecycle_telemetry()
    except Exception:
        logger.exception("webchat lifecycle 指标注册失败（降级为仅日志）")
        telemetry = None
    finisher = WebchatDurableTurnFinisher(
        session_factory, channel_name=channel_name, telemetry=telemetry
    )
    logger.info(
        "webchat durable 接受网关已装配（channel=%s, PG durable source of truth）",
        channel_name,
    )
    return WebchatDurableRuntime(
        engine=engine,
        session_factory=session_factory,
        gateway=gateway,
        finisher=finisher,
        telemetry=telemetry,
    )
