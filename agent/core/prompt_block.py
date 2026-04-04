from __future__ import annotations

from abc import ABC, abstractmethod

from agent.core.types import ContextBundle, InboundMessage


class PromptBlock(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """片段名称"""

    @property
    @abstractmethod
    def priority(self) -> int:
        """片段优先级"""

    @abstractmethod
    async def render(
        self,
        msg: InboundMessage,
        context: ContextBundle,
    ) -> str | None:
        """渲染本轮 prompt 片段"""
