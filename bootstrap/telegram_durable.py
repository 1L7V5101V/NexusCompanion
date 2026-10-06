"""Telegram durable 入站网关与投递适配（C10：telegram-binding-sync）。

Pilot Telegram 接入（`[channels.telegram] pilot_identity_binding` 开启）的
durable 路径，镜像 `webchat_durable.py` 的接受/收束/投递三层：

1. **入站**（:class:`TelegramDurableGateway`）——绑定解析已由通道完成；本网关
   做 overload 预检 → T1 `accept_inbound`（Telegram source 三元组幂等，C2 已
   内建部分唯一索引）→ `publish_inbound_wait` 入队（T1 已提交后满载在界内等待，
   语义与 WebChat 网关一致）。重复 source 三元组走既有幂等成功路径（零新写入）。
2. **投递分发**（:class:`ChannelRoutingDeliveryAdapter`）——`claim_batch` 不分
   channel（不改 C2 冻结面）；adapter 层按 `envelope.channel` 分发：`chat` →
   既有 WebchatDeliveryAdapter（行为不变），`telegram` →
   :class:`TelegramDeliveryAdapter`（Bot API 发送，telegram message_id 作
   provider receipt）。未注册 channel 记失败重试（不静默丢弃）。

Telegram 方向的文本投递成功即推进 `sent`（§5.9.11：sent 只由 provider ack
推进）；回复携带的媒体 best-effort 发送、失败仅记日志——避免重试整条 intent
造成文本重复。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.models.canonical import CanonicalMessageModel
from bootstrap.db.repository.control_plane_repo import (
    IngressRepository,
    NotFoundError,
)
from bus.events import InboundMessage
from bus.queue import MessageBus
from infra.channels.web_chat_protocol import message_accepted

logger = logging.getLogger(__name__)

__all__ = [
    "ChannelRoutingDeliveryAdapter",
    "TelegramDeliveryAdapter",
    "TelegramDurableGateway",
    "TelegramPilotIngress",
    "telegram_accept_outcome_message",
]

# 入站接受后入队等待上限（与 WebChat 网关同级；T1 已提交，超时按失败终态收束）。
_ENQUEUE_WAIT_SECONDS = 30.0


def telegram_pilot_prereq_error(*, storage_backend: str, auth_enabled: bool) -> str | None:
    """Pilot Telegram 绑定同步的前置条件检查（ADR-6 fail-fast）。

    返回 None 表示满足；否则返回用户可见的失败原因（启动时 raise）。
    """
    if storage_backend != "postgres":
        return (
            "[channels.telegram].pilot_identity_binding=true 要求"
            " [storage].backend=postgres（PG durable source of truth）；"
            "当前后端不满足，拒绝启动。"
        )
    if not auth_enabled:
        return (
            "[channels.telegram].pilot_identity_binding=true 要求"
            " [auth].enabled=true（绑定码签发与身份门禁依赖认证体系）；"
            "请先启用认证。"
        )
    return None


class TelegramDurableGateway:
    """Telegram 入站的 durable 接受网关（绑定解析在通道侧完成）。"""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        bus: MessageBus,
        *,
        enqueue_wait_seconds: float = _ENQUEUE_WAIT_SECONDS,
    ) -> None:
        self._ingress = IngressRepository(session_factory)
        self._sf = session_factory
        self._bus = bus
        self._enqueue_wait = enqueue_wait_seconds
        self._webchat_channel: Any | None = None

    def bind_webchat_channel(self, channel: Any) -> None:
        """绑定 WebChat 通道引用（T1 accepted 帧实时推送用，C10 ADR-5）。"""
        self._webchat_channel = channel

    async def accept_message(
        self,
        *,
        identity: Any,
        source_message_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        sender: str = "telegram",
    ) -> str:
        """durable 接受一条已绑定 Telegram 私聊消息。

        返回 ``"accepted"`` 或 ``"duplicate"``（均表示 T1 已提交/既存，调用方
        无需再发送确认）；``"overload"``/``"error"`` 表示未入队（无 durable
        写入或已按失败终态收束），调用方把用户可见文案交回通道。

        抛出的绑定/会话级异常已由通道侧门禁拦截；会话缺失（NotFoundError）
        在此映射为 error 语义（fail-closed，真实原因进日志）。
        """
        tenant_id = str(identity.tenant_id)
        conversation_id = str(identity.conversation_id)
        # 1. overload 预检：T1 之前拒绝，不消耗幂等键（§5.9.4/C4 同款契约）。
        if self._bus.inbound_full():
            return "overload"
        # 2. T1：dedupe(source 三元组) + canonical user message + inbox + queued
        #    turn + accepted 重放帧，单事务；重复注入回既有身份（零新写入）。
        client_ack = message_accepted(
            client_message_id=f"tg:{identity.telegram_chat_id}:{source_message_id}",
            session_key=f"chat:{tenant_id}",
        )
        try:
            result = await self._ingress.accept_inbound(
                tenant_id,
                conversation_id,
                source_channel="telegram",
                source_identity_id=str(identity.telegram_user_id),
                source_message_id=str(source_message_id),
                account_id=identity.account_id,
                content=content,
                metadata={
                    **(metadata or {}),
                    "client_message_id": client_ack["client_message_id"],
                    "username": sender,
                },
                replay_frame=client_ack,
            )
        except NotFoundError:
            logger.error(
                "telegram durable accept 被拒绝: tenant=%s conv=%s（会话缺失/租户不符）",
                tenant_id,
                conversation_id,
            )
            return "error"
        except Exception:
            logger.exception("telegram durable accept 事务失败 tenant=%s", tenant_id)
            return "error"
        if result.duplicate:
            logger.info(
                "telegram 重复 update 幂等忽略 tenant=%s source_message_id=%s",
                tenant_id,
                source_message_id,
            )
            return "duplicate"
        if result.replay_frame is not None:
            self._push_accepted_live(conversation_id, result.replay_frame)

        # 3. 入队执行（T1 已提交；metadata 携带 pg 身份键贯穿到 T2）。
        # session_key_override：turn 执行会话键与 WebChat 完全一致（ADR-3，
        # 共享 tenant 派生视图），否则 channel:chat_id 会分裂出第二个 session。
        inbound = InboundMessage(
            channel="telegram",
            sender=sender,
            chat_id=str(identity.telegram_chat_id),
            content=content,
            media=[],
            metadata={
                **(metadata or {}),
                "client_message_id": client_ack["client_message_id"],
                "username": sender,
                "session_key_override": f"chat:{tenant_id}",
                "nexus_pg_turn_id": result.turn_id or "",
                "nexus_pg_inbox_id": result.inbox_id,
                "nexus_pg_message_id": result.message_id,
                "nexus_pg_sequence": str(result.sequence),
                "nexus_pg_conversation_id": conversation_id,
                "nexus_pg_tenant_id": tenant_id,
            },
            tenant_id=tenant_id,
        )
        try:
            await asyncio.wait_for(
                self._bus.publish_inbound_wait(inbound), self._enqueue_wait
            )
        except Exception as exc:
            await self._fail_queued_turn(
                tenant_id,
                turn_id=result.turn_id,
                detail=str(exc),
            )
            return "error"
        return "accepted"

    async def _fail_queued_turn(self, tenant_id: str, *, turn_id: str | None, detail: str) -> None:
        """入队失败的 queued turn 收束为 failed（durable，不产投递意图）。"""
        if not turn_id:
            return
        from bootstrap.db.repository.control_plane_repo import TurnControlRepository
        from infra.channels.web_chat_protocol import turn_failed

        try:
            await TurnControlRepository(self._sf).transition_turn(
                tenant_id,
                turn_id,
                expected_status="queued",
                new_status="failed",
                error={"code": "enqueue_failed", "detail": detail[:500]},
                replay_frame=turn_failed(turn_id=turn_id, error="enqueue_failed"),
            )
        except Exception:
            logger.error(
                "telegram durable turn 收束失败终态异常 turn=%s", turn_id, exc_info=True
            )

    def _push_accepted_live(self, conversation_id: str, frame: dict[str, Any]) -> None:
        """T1 提交后的 accepted 帧实时推送 WebChat 在线连接（尽力而为）。

        失败仅记日志：durable 权威在重放帧表，断线补拉/REST 重建兜底。
        """
        if self._webchat_channel is None or not conversation_id:
            return
        task = asyncio.create_task(
            self._webchat_channel.deliver_frame(conversation_id, dict(frame))
        )
        task.add_done_callback(_log_push_failure)


class TelegramDeliveryAdapter:
    """delivery worker 的 Telegram 发送回调（channel="telegram" 的 intent）。

    取 final canonical message 内容经 Bot API 发送；成功以 telegram message_id
    作 provider receipt。媒体取自 intent payload（T2 delivery_payload），
    best-effort 发送、失败仅记日志——不使 intent 重试（否则文本会随重试重复）。
    """

    def __init__(self, session_factory: async_sessionmaker, channel: Any) -> None:
        self._sf = session_factory
        self._channel = channel

    async def __call__(self, envelope: Any) -> str | None:
        content = await self._final_message_content(
            str(envelope.tenant_id), str(envelope.message_id)
        )
        payload = getattr(envelope, "payload", None)
        media = [str(m) for m in (payload or {}).get("media", [])]
        receipt = await self._channel.pilot_deliver_final(
            str(envelope.target_chat_id),
            content,
            media=media,
        )
        return f"telegram:{receipt}" if receipt else "telegram:sent"

    async def _final_message_content(self, tenant_id: str, message_id: str) -> str:
        from uuid import UUID

        async with self._sf() as sess:
            row = (
                await sess.execute(
                    select(CanonicalMessageModel).where(
                        CanonicalMessageModel.id == UUID(message_id),
                        CanonicalMessageModel.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise RuntimeError(
                    f"final canonical message 缺失: message_id={message_id}"
                )
            return str(row.content or "")


class _EnvelopeChannelProto(Protocol):
    """delivery envelope 上与本分发相关的字段（结构化鸭子类型）。"""

    channel: str
    tenant_id: str


class ChannelRoutingDeliveryAdapter:
    """按 `envelope.channel` 分发的 delivery adapter（claim 不分 channel）。"""

    def __init__(self, routes: dict[str, Any]) -> None:
        self._routes = dict(routes)

    async def __call__(self, envelope: Any) -> str | None:
        channel = str(getattr(envelope, "channel", "") or "")
        handler = self._routes.get(channel)
        if handler is None:
            # 未注册 channel：记录失败让 worker 按退避重试（不静默丢弃；
            # 管理员可从 dead_letter / 日志看到配置缺口）。
            raise RuntimeError(
                f"delivery adapter 未注册 channel={channel!r} "
                f"intent={getattr(envelope, 'id', '?')}"
            )
        return await handler(envelope)


def telegram_accept_outcome_message(kind: str) -> str:
    """接受结果的用户可见文案（overload/error 直接经 Bot 发送；无 durable 写入）。"""
    if kind == "overload":
        return "服务繁忙，请稍后重发这条消息。"
    return "服务暂时不可用，请稍后重发这条消息。"


_UNBOUND_GUIDE = (
    "您好！这个 Bot 已切换到账号绑定模式。\n\n"
    "请先在 WebChat 登录后进入「Telegram 绑定」获取一次性绑定码，"
    "然后把绑定码直接发给我完成绑定。\n\n"
    "绑定码 10 分钟内有效且只能使用一次。"
)


def _log_push_failure(task: "asyncio.Task[Any]") -> None:
    """实时推送 fire-and-forget 任务的失败记录（不中断主流程）。"""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning("webchat 实时推送 telegram 帧失败: %s", error)


class TelegramPilotIngress:
    """通道侧单一门面：绑定门禁 + 兑换 + durable 接受（C10 ADR-3）。

    `TelegramChannel` 在 Pilot 模式（pilot_identity_binding=true）把私聊
    文本/命令/媒体交给本门面；返回值为需要回复给用户的文案，``None`` 表示
    已受理（回复将来自 durable delivery，不在本路径即时发送）。
    """

    def __init__(self, binding_service: Any, gateway: TelegramDurableGateway) -> None:
        self._binding = binding_service
        self._gateway = gateway

    def bind_webchat_channel(self, channel: Any) -> None:
        """绑定 WebChat 通道引用（透传给 durable 网关，accepted 帧实时推送）。"""
        self._gateway.bind_webchat_channel(channel)

    async def resolve(self, telegram_user_id: str, telegram_chat_id: str) -> Any | None:
        """active 绑定解析（fail-closed：无绑定/账号非 active → None）。"""
        return await self._binding.resolve_identity(telegram_user_id, telegram_chat_id)

    async def handle_text(
        self,
        *,
        chat_id: str,
        user_id: str,
        username: str,
        message_id: str,
        raw_text: str,
        agent_text: str,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """私聊文本入口：未绑定先试兑换，已绑定走 durable 接受。"""
        identity = await self.resolve(user_id, chat_id)
        if identity is None:
            return await self._try_redeem(
                raw_text, user_id=user_id, chat_id=chat_id
            )
        kind = await self._gateway.accept_message(
            identity=identity,
            source_message_id=message_id,
            content=agent_text,
            metadata={
                "username": username,
                **(metadata or {}),
            },
            sender=username or str(user_id),
        )
        if kind in ("accepted", "duplicate"):
            return None
        return telegram_accept_outcome_message(kind)

    async def handle_command_start(
        self, *, chat_id: str, user_id: str, code_argument: str
    ) -> str | None:
        """/start [code]：未绑定且有码参数 → 兑换；否则绑定指引。"""
        identity = await self.resolve(user_id, chat_id)
        if identity is not None:
            return "此 Telegram 账号已完成绑定。直接发消息即可开始对话。"
        if not code_argument.strip():
            return _UNBOUND_GUIDE
        return await self._try_redeem(code_argument, user_id=user_id, chat_id=chat_id)

    async def _try_redeem(self, code_text: str, *, user_id: str, chat_id: str) -> str:
        from bootstrap.db.repository.telegram_repo import (
            TelegramBindingConflictError,
            TelegramCodeRejectedError,
        )

        try:
            binding = await self._binding.redeem(
                code_text=code_text,
                telegram_user_id=user_id,
                telegram_chat_id=chat_id,
            )
        except TelegramCodeRejectedError:
            return "绑定码无效、已使用或已过期。请在 WebChat 重新获取后重试。"
        except TelegramBindingConflictError as exc:
            if "identity" in str(exc):
                return "此 Telegram 账号已绑定其他测试账号；如需换绑请联系管理员先解绑。"
            return "该测试账号已绑定其他 Telegram 身份；如需换绑请先解绑。"
        logger.info(
            "telegram 绑定码兑换成功 user=%s account=%s tenant=%s",
            user_id,
            binding["account_id"],
            binding["tenant_id"],
        )
        return "绑定成功！现在发消息即可与你的助手对话。"

    async def media_unsupported(self, *, user_id: str, chat_id: str) -> str | None:
        """媒体消息（首版不支持）；未绑定用户仍给绑定指引。"""
        if await self.resolve(user_id, chat_id) is None:
            return _UNBOUND_GUIDE
        return "首版暂不支持图片/文件消息，请发送文字。"

