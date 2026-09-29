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
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agent.config_models import Config
from bootstrap.db.config import DatabaseConfig
from bootstrap.db.engine import create_engine, create_session_factory
from bootstrap.db.repository.control_plane_repo import (
    IngressRepository,
    NotFoundError,
    TurnControlRepository,
)
from bootstrap.work_queue import async_pg_url
from bus.events import InboundMessage
from bus.queue import MessageBus
from infra.channels.web_chat_channel import DurableSendOutcome
from infra.channels.web_chat_protocol import message_accepted, turn_failed

logger = logging.getLogger(__name__)

__all__ = [
    "WebchatDurableGateway",
    "WebchatDurableRuntime",
    "build_webchat_durable_runtime",
]

# metadata 保留键：pg durable 身份贯穿（task 3.x 完成事务消费；`nexus_` 前缀
# 与既有 `nexus_error` 约定一致，不进 prompt、不参与授权）。
PG_TURN_ID_KEY = "nexus_pg_turn_id"
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


@dataclass
class WebchatDurableRuntime:
    """durable 网关 + 独立 async engine（停机 cleanup 释放连接池）。"""

    engine: AsyncEngine
    session_factory: async_sessionmaker
    gateway: WebchatDurableGateway

    async def aclose(self) -> None:
        await self.engine.dispose()


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
    logger.info(
        "webchat durable 接受网关已装配（channel=%s, PG durable source of truth）",
        channel_name,
    )
    return WebchatDurableRuntime(
        engine=engine, session_factory=session_factory, gateway=gateway
    )
