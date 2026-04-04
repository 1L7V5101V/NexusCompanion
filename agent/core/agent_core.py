from __future__ import annotations

from agent.core.context_store import ContextStore
from agent.core.prompt_block import PromptBlock
from agent.core.reasoner import Reasoner
from agent.core.types import (
    ContextBundle,
    InboundMessage,
    OutboundMessage,
    TurnRecord,
)
from agent.tools.base import Tool


class AgentCore:
    """
    ┌──────────────────────────────────────┐
    │ AgentCore                            │
    ├──────────────────────────────────────┤
    │ 1. prepare context                   │
    │ 2. build system prompt               │
    │ 3. run reasoner                      │
    │ 4. commit turn                       │
    │ 5. build outbound                    │
    └──────────────────────────────────────┘
    """

    def __init__(
        self,
        *,
        context_store: ContextStore,
        reasoner: Reasoner,
        tools: list[Tool],
        prompt_blocks: list[PromptBlock] | None = None,
        identity_prompt: str = "",
    ) -> None:
        self._context_store = context_store
        self._reasoner = reasoner
        self._tools = tools
        self._prompt_blocks = sorted(
            prompt_blocks or [],
            key=lambda block: block.priority,
        )
        self._identity_prompt = identity_prompt.strip()

    async def process(self, msg: InboundMessage) -> OutboundMessage:
        # 1. 准备本轮上下文
        context = await self._context_store.prepare(msg)

        # 2. 组装 system prompt
        system_prompt = await self._build_system_prompt(msg, context)

        # 3. 运行推理循环
        result = await self._reasoner.run(
            msg=msg,
            system_prompt=system_prompt,
            context=context,
            tools=self._tools,
        )

        # 4. 提交本轮结果
        await self._context_store.commit(
            TurnRecord(
                msg=msg,
                reply=result.reply,
                invocations=result.invocations,
                metadata=dict(result.metadata),
            )
        )

        # 5. 构造出站消息
        return OutboundMessage(
            channel=msg.channel,
            session_key=msg.session_key,
            content=result.reply,
            metadata={"thinking": result.thinking} if result.thinking else {},
        )

    async def _build_system_prompt(
        self,
        msg: InboundMessage,
        context: ContextBundle,
    ) -> str:
        # 1. 先放 identity prompt
        parts: list[str] = [self._identity_prompt] if self._identity_prompt else []

        # 2. 插入 prepare() 取回的 memory blocks
        for block in context.memory_blocks:
            text = str(block or "").strip()
            if text:
                parts.append(text)

        # 3. 按顺序渲染 prompt blocks
        for block in self._prompt_blocks:
            rendered = await block.render(msg, context)
            if rendered:
                parts.append(rendered)

        # 4. 拼出最终 system prompt
        return "\n\n".join(parts)
