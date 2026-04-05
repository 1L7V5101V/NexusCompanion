from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from agent.core.types import ToolCallGroup


@dataclass
class PostTurnEvent:
    session_key: str
    channel: str
    chat_id: str
    user_message: str
    assistant_response: str
    tools_used: list[str]
    tool_chain: list[ToolCallGroup]
    session: object
    timestamp: datetime | None = None
    extra: dict = field(default_factory=dict)


@runtime_checkable
class PostTurnPipeline(Protocol):
    def schedule(self, event: PostTurnEvent) -> None: ...
