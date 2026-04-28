from __future__ import annotations

import agent.core.passive_support as support
from agent.lifecycle.phase import GatePhase
from agent.lifecycle.types import BeforeStepCtx, BeforeStepInput
from bus.event_bus import EventBus


class BeforeStepPhase(GatePhase[BeforeStepInput, BeforeStepCtx, BeforeStepCtx]):

    def __init__(self, bus: EventBus) -> None:
        super().__init__(bus)

    async def _setup(self, input: BeforeStepInput) -> BeforeStepCtx:
        return BeforeStepCtx(
            session_key=input.session_key,
            channel=input.channel,
            chat_id=input.chat_id,
            iteration=input.iteration,
            input_tokens_estimate=support.estimate_messages_tokens(input.messages),
            visible_tool_names=(
                frozenset(input.visible_names)
                if input.visible_names is not None
                else None
            ),
        )

    async def _finalize(
        self,
        ctx: BeforeStepCtx,
        input: BeforeStepInput,
    ) -> BeforeStepCtx:
        if ctx.extra_hints:
            input.messages.append(
                support.build_context_hint_message(
                    "plugin_hints",
                    "\n".join(ctx.extra_hints),
                )
            )
        return ctx
