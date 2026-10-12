from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from agent.prompting.assembler import PromptSectionRender
from bus.events import InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from agent.core.response_parser import ResponseMetadata
    from agent.core.runtime_support import SessionLike, TurnRunResult


# 1. 工厂函数：给 dataclass field(default_factory=...) 提供显式类型签名，消除 pyright Unknown 推断。
def _empty_str_list() -> list[str]:
    return []


def _empty_metadata() -> dict[str, Any]:
    return {}


def _empty_prompt_sections() -> list[PromptSectionRender]:
    return []


@dataclass
class TurnState:
    msg: InboundMessage
    session_key: str
    dispatch_outbound: bool
    session: SessionLike | None = None
    extra_metadata: dict[str, Any] = field(default_factory=_empty_metadata)
    # C14：本 work 的 active memory engine（work-start 解析一次后冻结；空串 =
    # dev/未接线路径，消费方回退进程 primary）。
    memory_engine: str = ""

    @property
    def tenant_id(self) -> str:
        """本 turn 的 tenant，来自可信入站消息的 tenant_id（由适配器派生）。"""
        return self.msg.tenant_id


@dataclass
class BeforeTurnCtx:
    # before-* ctx 走 GATE 链，插件可直接改写字段影响后续阶段。
    # read-only by convention
    session_key: str
    channel: str
    chat_id: str
    content: str
    timestamp: datetime
    retrieved_memory_block: str
    retrieval_trace_raw: object | None
    history_messages: tuple[Any, ...]
    # writable
    skill_names: list[str] = field(default_factory=_empty_str_list)
    abort: bool = False
    abort_reply: str = ""
    extra_hints: list[str] = field(default_factory=_empty_str_list)
    extra_metadata: dict[str, Any] = field(default_factory=_empty_metadata)


@dataclass(frozen=True)
class BeforeReasoningInput:
    state: TurnState
    before_turn: BeforeTurnCtx
    # c18 task 3.7 / ADR-12：本 turn 的画像快照在 before_reasoning 之前就已解析好，
    # 预热渲染吃同一份——以无租户形状预热会把租户作用域的段缓存与调试视图污染成空画像。
    profile_snapshot: Any


@dataclass
class BeforeReasoningCtx:
    # before-* ctx 走 GATE 链，插件可直接改写字段影响后续阶段。
    # read-only by convention
    session_key: str
    channel: str
    chat_id: str
    content: str
    timestamp: datetime
    # writable
    skill_names: list[str]
    retrieved_memory_block: str
    # c18 task 3.7：预热渲染要按本轮真实归属渲染，所以归属与快照随 ctx 一起进来。
    tenant_id: str
    profile_snapshot: Any
    extra_hints: list[str] = field(default_factory=_empty_str_list)
    abort: bool = False
    abort_reply: str = ""


@dataclass(frozen=True)
class PromptRenderInput:
    session_key: str
    channel: str
    chat_id: str
    content: str
    media: list[str] | None
    timestamp: datetime
    history: list[dict[str, Any]]
    skill_names: list[str] | None
    retrieved_memory_block: str
    disabled_sections: set[str]
    turn_injection_prompt: str
    # c18 task 3.7：归属与画像快照随输入进来，不在渲染阶段自己猜。
    tenant_id: str
    profile_snapshot: Any
    extra_hints: list[str] | None = None
    persona_snapshot: Any | None = None


@dataclass
class PromptRenderCtx:
    # render/before-step ctx 走 GATE 链，插件可直接改写字段影响后续阶段。
    # read-only by convention
    session_key: str
    channel: str
    chat_id: str
    content: str
    media: list[str] | None
    timestamp: datetime
    history: list[dict[str, Any]]
    skill_names: list[str] | None
    retrieved_memory_block: str
    disabled_sections: set[str]
    turn_injection_prompt: str
    # c18 task 3.7：归属与画像快照必填，缺失即空语义由调用方交出 empty()
    tenant_id: str
    profile_snapshot: Any
    extra_hints: list[str] = field(default_factory=_empty_str_list)
    # writable
    system_sections_top: list[PromptSectionRender] = field(
        default_factory=_empty_prompt_sections
    )
    system_sections_bottom: list[PromptSectionRender] = field(
        default_factory=_empty_prompt_sections
    )

    persona_snapshot: "Any | None" = None

@dataclass(frozen=True)
class PromptRenderResult:
    messages: list[dict[str, Any]]


@dataclass(frozen=True)
class BeforeStepInput:
    session_key: str
    channel: str
    chat_id: str
    iteration: int
    messages: list[dict[str, Any]]
    visible_names: set[str] | None


@dataclass
class BeforeStepCtx:
    # before-* ctx 走 GATE 链，插件可直接改写字段影响后续阶段。
    # read-only by convention
    session_key: str
    channel: str
    chat_id: str
    iteration: int
    input_tokens_estimate: int
    visible_tool_names: frozenset[str] | None
    # writable
    extra_hints: list[str] = field(default_factory=_empty_str_list)
    early_stop: bool = False
    early_stop_reply: str = ""


@dataclass(frozen=True)
class AfterStepCtx:
    # after-* fanout ctx 是观察快照；需要补充 metadata 时由 PhaseModule replace 新实例。
    session_key: str
    channel: str
    chat_id: str
    iteration: int
    context_tokens_estimate: int
    tools_called: tuple[str, ...]
    partial_reply: str
    tools_used_so_far: tuple[str, ...]
    tool_chain_partial: tuple[dict[str, Any], ...]
    partial_thinking: str | None
    has_more: bool
    early_stop: bool = False
    early_stop_reason: str = ""
    extra_metadata: dict[str, Any] = field(default_factory=_empty_metadata)


@dataclass(frozen=True)
class AfterReasoningInput:
    state: TurnState
    turn_result: TurnRunResult


@dataclass
class AfterReasoningCtx:
    # after_reasoning 仍是 GATE 链，插件可改写 reply/media/outbound_metadata。
    # read-only by convention
    session_key: str
    channel: str
    chat_id: str
    tools_used: tuple[str, ...]
    thinking: str | None
    response_metadata: ResponseMetadata
    streamed: bool
    tool_chain: tuple[dict[str, Any], ...]
    context_retry: dict[str, object]
    # writable
    reply: str
    media: list[str] = field(default_factory=_empty_str_list)
    meme_tag: str | None = None
    outbound_metadata: dict[str, Any] = field(default_factory=_empty_metadata)
    # 日志系统注入字段
    initial_messages: list[dict[str, Any]] = field(default_factory=list)
    tools_schema: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class AfterReasoningResult:
    ctx: AfterReasoningCtx
    outbound: OutboundMessage


@dataclass
class TurnSnapshot:
    state: TurnState
    outbound: OutboundMessage
    ctx: AfterReasoningCtx


@dataclass(frozen=True)
class AfterTurnCtx:
    # after-* fanout ctx 是观察快照；需要补充 metadata 时由 PhaseModule replace 新实例。
    session_key: str
    channel: str
    chat_id: str
    reply: str
    tools_used: tuple[str, ...]
    thinking: str | None
    # pre-dispatch intent flag: dispatch has NOT happened yet when Tap handlers run
    will_dispatch: bool
    extra_metadata: dict[str, Any] = field(default_factory=_empty_metadata)


@dataclass(frozen=True)
class BeforeToolCallCtx:
    session_key: str
    channel: str
    chat_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AfterToolResultCtx:
    session_key: str
    channel: str
    chat_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: str
    status: str


@dataclass
class PreToolCtx:
    """pre-tool hook 上下文 — mutable，handler 返回 dict 表示新 arguments"""
    session_key: str
    channel: str
    chat_id: str
    tool_name: str
    arguments: dict[str, Any]
    call_id: str = ""
    source: str = ""
    request_text: str = ""
    tool_batch: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    tool_batch_index: int = 0
