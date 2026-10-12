from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bus.events import InboundMessage

@dataclass
class ChatMessage:
    role: str
    content: str


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: str = ""


@dataclass
class ToolCallGroup:
    text: str
    calls: list[ToolCall] = field(default_factory=list)


@dataclass
class HistoryMessage:
    role: str
    content: str
    tools_used: list[str] = field(default_factory=list)
    tool_chain: list[ToolCallGroup] = field(default_factory=list)


@dataclass
class RetrievalTrace:
    gate_type: str | None = None
    route_decision: str | None = None
    rewritten_query: str | None = None
    injected_count: int = 0
    raw: object | None = None


def to_tool_call_groups(raw_chain: list[dict]) -> list[ToolCallGroup]:
    groups: list[ToolCallGroup] = []
    for group in raw_chain:
        text = str(group.get("text", "") or "")
        calls: list[ToolCall] = []
        for call in (group.get("calls") or []):
            args = call.get("arguments")
            calls.append(
                ToolCall(
                    call_id=str(call.get("call_id", "") or ""),
                    name=str(call.get("name", "") or ""),
                    arguments=args if isinstance(args, dict) else {},
                    result=str(call.get("result", "") or ""),
                )
            )
        groups.append(ToolCallGroup(text=text, calls=calls))
    return groups


@dataclass
class ContextBundle:
    history: list[ChatMessage] = field(default_factory=list)
    memory_blocks: list[str] = field(default_factory=list)
    skill_mentions: list[str] = field(default_factory=list)
    retrieved_memory_block: str = ""
    retrieval_trace_raw: Any | None = None
    retrieval_metadata: dict[str, Any] = field(default_factory=dict)
    retrieval_verified: bool = False
    history_messages: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PersonaSnapshot:
    """本轮 turn 的 tenant 人设与关系状态快照（c9-persona-relationship ADR-4）。

    由 turn 入口在 tenant lane 内解析一次（PG durable 模式读 `tenant_persona_profiles`
    与画像专表的 `relationship_state` 行，c18 ADR-9），与 `MemoryProfileSnapshot` 成对
    返回、出自同一次数据库借用（ADR-1），随 ContextRequest 传入 prompt 组装；组装期不再
    查库。None = 无快照（dev/SQLite 路径或 onboarding 未完成）→ 各 block 走单体回退语义。
    """

    tenant_id: str
    source: str
    identity: str
    personality_rules: str
    relationship_state: str


@dataclass(frozen=True)
class MemoryProfileSnapshot:
    """本轮 turn 的 tenant 记忆画像快照（c18-tenant-scoped-memory ADR-1）。

    由 turn 入口在 tenant lane 内解析一次（画像专表的两条 SELECT，与 PersonaSnapshot 同
    一次借用取回），随 ContextRequest 传入 prompt 组装；组装期不再查库，也不得回退读共享
    workspace 记忆目录。

    不含 `self` 那一格——自我认知归 PersonaSnapshot.relationship_state
    （ADR-9）。不含 `journal`——它是日志，没有任何组装期消费者（ADR-8）。
    `empty()` 表示该租户这几类都还没有内容，是空语义而非回退。
    """

    tenant_id: str
    long_term: str
    pending: str
    recent_context: str
    history: str

    @classmethod
    def empty(cls, tenant_id: str) -> "MemoryProfileSnapshot":
        return cls(
            tenant_id=tenant_id,
            long_term="",
            pending="",
            recent_context="",
            history="",
        )


@dataclass
class ContextRequest:
    history: list[dict[str, Any]]
    current_message: str
    media: list[str] | None = None
    skill_names: list[str] | None = None
    channel: str | None = None
    chat_id: str | None = None
    message_timestamp: datetime | None = None
    retrieved_memory_block: str = ""
    disabled_sections: set[str] | None = None
    turn_injection_prompt: str | None = None
    persona_snapshot: "PersonaSnapshot | None" = None
    profile_snapshot: "MemoryProfileSnapshot | None" = None
    tenant_id: str = ""


@dataclass
class ContextRenderResult:
    system_prompt: str
    turn_injection_context: dict[str, str] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    debug_breakdown: list[Any] = field(default_factory=list)


@dataclass
class LLMResponse:
    reply: str | None
    tool_calls: list["LLMToolCall"] = field(default_factory=list)
    thinking: str | None = None


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReasonerResult:
    reply: str
    invocations: list[LLMToolCall] = field(default_factory=list)
    thinking: str | None = None
    streamed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    # 日志系统注入字段
    initial_messages: list[dict[str, Any]] = field(default_factory=list)
    tools_schema: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TurnRecord:
    msg: InboundMessage
    reply: str
    invocations: list[LLMToolCall] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
