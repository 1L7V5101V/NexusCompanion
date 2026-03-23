from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HistoryMessage:
    role: str
    content: str
    tools_used: list[str] = field(default_factory=list)
    tool_chain: list["ToolCallGroup"] = field(default_factory=list)


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict
    result: str


@dataclass
class ToolCallGroup:
    text: str
    calls: list[ToolCall] = field(default_factory=list)


@dataclass
class RetrievalTrace:
    gate_type: str | None = None
    route_decision: str | None = None
    rewritten_query: str | None = None
    injected_count: int = 0
    raw: object | None = None
