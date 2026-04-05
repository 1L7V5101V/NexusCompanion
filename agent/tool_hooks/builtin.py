from __future__ import annotations

from typing import TYPE_CHECKING

from agent.procedure_hint import (
    _match_procedure_items,
    build_intercept_hint,
    build_procedure_hint,
)
from agent.tool_hooks.base import ToolHook
from agent.tool_hooks.types import HookContext, HookOutcome

if TYPE_CHECKING:
    from core.memory.port import MemoryPort


class ProcedureGuardHook(ToolHook):
    name = "procedure_guard"
    event = "pre_tool_use"

    def __init__(self, memory: "MemoryPort | None") -> None:
        self._memory = memory

    def matches(self, ctx: HookContext) -> bool:
        return self._memory is not None

    async def run(self, ctx: HookContext) -> HookOutcome:
        items = _match_procedure_items(
            memory=self._memory,
            tool_name=ctx.request.tool_name,
            tool_arguments=ctx.current_arguments,
        )
        ctx.request.hook_state["procedure_items"] = items
        injected_ids = ctx.request.hook_state.setdefault("injected_proc_ids", set())
        intercept_items = [
            item
            for item in items
            if bool(item.get("intercept", False))
            and str(item.get("id", "")) not in injected_ids
        ]
        if not intercept_items:
            return HookOutcome()
        injected_ids.update(str(item.get("id", "")) for item in intercept_items)
        return HookOutcome(
            decision="deny",
            reason=build_intercept_hint(intercept_items, ctx.request.tool_name),
        )


class ProcedureResultHintHook(ToolHook):
    name = "procedure_result_hint"
    event = "post_tool_use"

    def __init__(self, memory: "MemoryPort | None") -> None:
        self._memory = memory

    def matches(self, ctx: HookContext) -> bool:
        return self._memory is not None

    async def run(self, ctx: HookContext) -> HookOutcome:
        cached_items = ctx.request.hook_state.get("procedure_items")
        source_items = cached_items if isinstance(cached_items, list) else _match_procedure_items(
            memory=self._memory,
            tool_name=ctx.request.tool_name,
            tool_arguments=ctx.current_arguments,
        )
        items = [
            item
            for item in source_items
            if not bool(item.get("intercept", False))
        ]
        injected_ids = ctx.request.hook_state.setdefault("injected_proc_ids", set())
        hint, new_ids = build_procedure_hint(items, injected_ids)
        if not hint:
            return HookOutcome()
        injected_ids.update(new_ids)
        extra = hint.split("\n", 1)[1] if "\n" in hint else hint
        return HookOutcome(extra_message=extra)
