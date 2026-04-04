from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from agent.core.types import LLMResponse
from agent.core.types import ToolCall as CoreToolCall

if TYPE_CHECKING:
    from agent.provider import LLMProvider as LegacyProvider
    from agent.tools.base import Tool


class LLMProvider(ABC):
    @abstractmethod
    async def step(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[Tool],
    ) -> LLMResponse:
        """执行单轮模型调用"""


class ProviderLLMAdapter(LLMProvider):
    """
    ┌──────────────────────────────────────┐
    │ ProviderLLMAdapter                   │
    ├──────────────────────────────────────┤
    │ 1. 新消息转旧 provider.chat 输入      │
    │ 2. 调旧 provider                     │
    │ 3. 转回 core 的 LLMResponse          │
    └──────────────────────────────────────┘
    """

    def __init__(
        self,
        provider: "LegacyProvider",
        *,
        model: str,
        max_tokens: int,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_tokens = max_tokens

    async def step(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list["Tool"],
    ) -> LLMResponse:
        # 1. 组装旧 provider 所需的消息与 schema
        legacy_messages = [dict(item) for item in messages]
        if system_prompt.strip():
            legacy_messages = [
                {"role": "system", "content": system_prompt},
                *legacy_messages,
            ]
        legacy_tools = [tool.to_schema() for tool in tools if hasattr(tool, "to_schema")]

        # 2. 调用旧 provider
        response = await self._provider.chat(
            messages=legacy_messages,
            tools=legacy_tools,
            model=self._model,
            max_tokens=self._max_tokens,
        )

        # 3. 转回 core 响应对象
        return LLMResponse(
            reply=response.content,
            tool_calls=[
                CoreToolCall(
                    id=item.id,
                    name=item.name,
                    arguments=item.arguments,
                )
                for item in response.tool_calls
            ],
            thinking=response.thinking,
        )
