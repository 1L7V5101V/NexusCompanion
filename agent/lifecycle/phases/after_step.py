from __future__ import annotations

from bus.event_bus import EventBus
from agent.lifecycle.phase import TapPhase
from agent.lifecycle.types import AfterStepCtx


class AfterStepPhase(TapPhase[AfterStepCtx, AfterStepCtx, AfterStepCtx]):

    def __init__(self, bus: EventBus) -> None:
        super().__init__(bus)

    async def _setup(self, input: AfterStepCtx) -> AfterStepCtx:
        return input

    async def _finalize(self, ctx: AfterStepCtx, input: AfterStepCtx) -> AfterStepCtx:
        return ctx
