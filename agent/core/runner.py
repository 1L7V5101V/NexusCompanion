from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from agent.core.agent_core import AgentCore
from agent.core.types import InboundMessage as CoreInboundMessage
from agent.core.types import OutboundMessage as CoreOutboundMessage
from bus.events import InboundMessage, OutboundMessage
from bus.internal_events import is_spawn_completion_message, parse_spawn_completion
from bus.processing import ProcessingState
from bus.queue import MessageBus

logger = logging.getLogger("agent.core.runner")


class PassiveRunner(ABC):
    """
    ┌──────────────────────────────────────┐
    │ PassiveRunner                        │
    ├──────────────────────────────────────┤
    │ 1. 消费 bus 入站消息                 │
    │ 2. 调 AgentCore.process()           │
    │ 3. 发布 bus 出站消息                 │
    │ 4. 提供 process_direct()            │
    └──────────────────────────────────────┘
    """

    @property
    @abstractmethod
    def processing_state(self) -> ProcessingState | None: ...

    @abstractmethod
    async def run(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    async def process_direct(
        self,
        *,
        content: str,
        session_key: str,
        channel: str,
        chat_id: str,
    ) -> str: ...


class CoreRunner(PassiveRunner):
    _MESSAGE_TIMEOUT_S: float = 600.0

    def __init__(
        self,
        *,
        bus: MessageBus,
        agent_core: AgentCore,
        processing_state: ProcessingState | None = None,
    ) -> None:
        self._bus = bus
        self._agent_core = agent_core
        self._processing_state = processing_state
        self._running = False

    @property
    def processing_state(self) -> ProcessingState | None:
        return self._processing_state

    async def run(self) -> None:
        # 1. 启动被动主循环
        self._running = True
        logger.info("CoreRunner 启动")

        while self._running:
            try:
                msg = await asyncio.wait_for(self._bus.consume_inbound(), timeout=1.0)
                try:
                    await self._process(msg)
                except Exception as exc:
                    logger.error("处理消息出错: %s", exc, exc_info=True)
                    await self._bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=f"出错：{exc}",
                        )
                    )
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._running = False
        logger.info("CoreRunner 停止")

    async def process_direct(
        self,
        *,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        # 1. 构造直连消息
        msg = InboundMessage(
            channel=channel,
            sender="user",
            chat_id=chat_id,
            content=content,
        )

        # 2. 直接走主处理链，但不回写 outbound bus
        outbound = await self._process(
            msg,
            session_key=session_key,
            dispatch_outbound=False,
        )
        return outbound.content if outbound else ""

    async def _process(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        dispatch_outbound: bool = True,
    ) -> OutboundMessage:
        # 1. 记录会话级 processing 状态
        started = time.time()
        key = session_key or msg.session_key
        if self._processing_state is not None:
            self._processing_state.enter(key)

        # 2. 带超时执行内层处理
        try:
            return await asyncio.wait_for(
                self._process_inner(
                    msg,
                    key,
                    dispatch_outbound=dispatch_outbound,
                ),
                timeout=self._MESSAGE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.error(
                "消息处理超时 (%ss) channel=%s chat_id=%s",
                self._MESSAGE_TIMEOUT_S,
                msg.channel,
                msg.chat_id,
            )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="（处理超时，请重试）",
            )
        finally:
            if self._processing_state is not None:
                self._processing_state.exit(key)
            _ = started

    async def _process_inner(
        self,
        msg: InboundMessage,
        key: str,
        *,
        dispatch_outbound: bool,
    ) -> OutboundMessage:
        # 1. spawn completion 先转成新的内部 prompt 消息
        if is_spawn_completion_message(msg):
            msg = self._build_spawn_completion_message(msg)

        # 2. 普通消息统一走 AgentCore
        core_msg = CoreInboundMessage(
            channel=msg.channel,
            session_key=key,
            sender=msg.sender,
            content=msg.content,
            media=list(msg.media),
            timestamp=msg.timestamp,
            metadata=dict(msg.metadata or {}),
        )
        core_outbound = await self._agent_core.process(core_msg)

        # 3. 转回 bus 出站消息
        outbound = self._to_bus_outbound(msg, core_outbound)
        if dispatch_outbound:
            await self._bus.publish_outbound(outbound)
        return outbound

    def _build_spawn_completion_message(self, msg: InboundMessage) -> InboundMessage:
        # 1. 解析后台任务事件
        event = parse_spawn_completion(msg)
        label = event.label or "后台任务"
        task = event.task.strip()
        status = (event.status or "incomplete").strip()
        result = event.result.strip()
        exit_reason = event.exit_reason.strip()
        retry_count = event.retry_count

        # 2. 生成这轮要交给 AgentCore 的内部指导文本
        exit_labels = {
            "completed": "正常完成",
            "max_iterations": "迭代预算耗尽（任务可能不完整）",
            "tool_loop": "工具调用循环截断（任务可能不完整）",
            "error": "执行出错",
            "forced_summary": "强制汇总（任务可能不完整）",
        }
        exit_label = exit_labels.get(exit_reason, exit_reason or "未知")
        if retry_count >= 1:
            guidance = (
                "⚠️ 已重试一次，不再重试。\n"
                "请直接将已获得的结果汇报给用户，说明已完成的部分和未完成的部分。"
            )
        else:
            guidance = (
                "**处理指引（按顺序判断，选其一执行）**\n"
                "1. 结果完整回答了原始任务 → 直接向用户汇报，不提及内部机制\n"
                "2. 退出原因是【迭代预算耗尽】或【工具调用循环截断】，且核心信息明显不足 → "
                "调用 spawn 重试；task 中说明上次卡在哪、这次从哪继续；"
                "run_in_background=true；同时简短告知用户正在补充\n"
                "3. 结果为空或明显出错 → 直接告知用户失败，询问是否需要重试\n"
                "重试只允许一次。"
            )
        current_message = (
            f"[后台任务回传]\n"
            f"任务标签: {label}\n"
            f"原始任务: {task or '（未提供）'}\n"
            f"退出原因: {exit_label}\n"
            f"执行结果:\n{result or '（无结果）'}\n\n"
            f"{guidance}\n\n"
            "禁止在回复中提及 subagent、spawn、job_id、内部事件等内部概念。\n"
            "必要时可读取结果里提到的文件来补充说明。"
        )
        marker = f"[后台任务完成] {label} ({status})"
        if exit_reason:
            marker += f" [{exit_reason}]"

        # 3. 返回供 AgentCore 继续处理的伪用户消息
        return InboundMessage(
            channel=msg.channel,
            sender=msg.sender,
            chat_id=msg.chat_id,
            content=current_message,
            timestamp=msg.timestamp,
            media=[],
            metadata={
                **(msg.metadata or {}),
                "skip_post_memory": True,
                "_skip_retrieval": True,
                "_persist_user_content": marker,
            },
        )

    def _to_bus_outbound(
        self,
        msg: InboundMessage,
        core_outbound: CoreOutboundMessage,
    ) -> OutboundMessage:
        # 1. 抽取 thinking 元数据
        metadata = dict(core_outbound.metadata or {})
        thinking_raw = metadata.pop("thinking", None)
        thinking = str(thinking_raw).strip() if thinking_raw else None

        # 2. 构造 bus 出站消息
        return OutboundMessage(
            channel=core_outbound.channel,
            chat_id=msg.chat_id,
            content=core_outbound.content,
            thinking=thinking or None,
            media=list(core_outbound.media),
            metadata=metadata,
        )
