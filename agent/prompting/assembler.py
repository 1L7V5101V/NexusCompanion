from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    from agent.context import ContextBuilder


@dataclass(frozen=True)
class PromptSectionRender:
    name: str
    content: str
    is_static: bool
    cache_hit: bool = False


@dataclass(frozen=True)
class PromptSectionMeta:
    name: str
    chars: int
    est_tokens: int
    is_static: bool
    cache_hit: bool


@dataclass
class AssembledTurnInput:
    system_sections: list[PromptSectionRender] = field(default_factory=list)
    system_prompt: str = ""
    turn_injection_context: dict[str, str] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    debug_breakdown: list[PromptSectionMeta] = field(default_factory=list)


class SectionCache:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str, str], str] = {}

    def get(self, scope: str, section_name: str, signature: str) -> str | None:
        return self._data.get((scope, section_name, signature))

    def set(self, scope: str, section_name: str, signature: str, content: str) -> None:
        self._data[(scope, section_name, signature)] = content


def build_turn_injection_message(content: str) -> dict[str, str]:
    return {"role": "system", "content": content}


class PromptAssembler:
    def __init__(self, context_builder: "ContextBuilder") -> None:
        self._context_builder = context_builder

    def assemble(
        self,
        *,
        history: list[dict[str, Any]],
        current_message: str,
        media: list[str] | None = None,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        message_timestamp: "datetime | None" = None,
        retrieved_memory_block: str = "",
        disabled_sections: set[str] | None = None,
        turn_injection_context: dict[str, str] | None = None,
    ) -> AssembledTurnInput:
        # assembler 负责把“主 prompt + turn injection + message envelope”
        # 收束成一份统一输入，避免调用方各自手拼消息顺序。
        built = self._context_builder._build_system_prompt_result(
            skill_names=skill_names,
            channel=channel,
            chat_id=chat_id,
            message_timestamp=message_timestamp,
            retrieved_memory_block=retrieved_memory_block,
            disabled_sections=disabled_sections,
        )
        injection_context = turn_injection_context or {}
        messages = self._context_builder._envelope_builder.build(
            history=history,
            current_message=current_message,
            system_prompt=built.system_prompt,
            turn_injection_context=injection_context,
            channel=channel,
            message_timestamp=message_timestamp,
            media=media,
        )
        return AssembledTurnInput(
            system_sections=built.system_sections,
            system_prompt=built.system_prompt,
            turn_injection_context=injection_context,
            messages=messages,
            debug_breakdown=built.debug_breakdown,
        )
