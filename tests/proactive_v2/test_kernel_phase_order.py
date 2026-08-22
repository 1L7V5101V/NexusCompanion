from __future__ import annotations

import pytest

from agent.core.proactive_kernel import ProactiveKernel
from proactive_v2.frame import ProactiveFrame, ProactiveTickResult
from proactive_v2.phases import ProactivePhaseRunner


class _Module:
    def __init__(
        self,
        slot: str,
        phase: str,
        calls: list[str],
        requires: tuple[str, ...] = (),
    ) -> None:
        self.slot = slot
        self.phase = phase
        self.calls = calls
        self.requires = requires

    async def run(self, frame: ProactiveFrame) -> ProactiveFrame:
        self.calls.append(self.slot)
        return frame


class _Pipeline:
    slot = "proactive.tick.pipeline"
    phase = "proactive.deliver"

    def __init__(self) -> None:
        self.slots: dict[str, object] | None = None
        self.run_count = 0

    async def run(self, frame: ProactiveFrame) -> ProactiveFrame:
        self.run_count += 1
        self.slots = frame.slots
        frame.output = ProactiveTickResult(base_score=0.42)
        return frame


@pytest.mark.asyncio
async def test_proactive_phase_runner_groups_modules_by_phase():
    calls: list[str] = []
    modules = [
        _Module("proactive.prompt.plugin", "proactive.prompt", calls),
        _Module("proactive.source.collect", "proactive.source", calls),
        _Module(
            "proactive.source.mcp_content",
            "proactive.source",
            calls,
            requires=("proactive.source.collect",),
        ),
    ]
    runner = ProactivePhaseRunner(modules)

    assert runner.modules_by_phase == {"default": modules}

    frame = await runner.run(ProactiveFrame(input=object()))  # type: ignore[arg-type]

    assert calls == []
    assert isinstance(frame.output, ProactiveTickResult)
    assert frame.output.base_score is None


@pytest.mark.asyncio
async def test_proactive_kernel_run_tick_returns_default_result():
    pipeline = _Pipeline()
    kernel = ProactiveKernel([pipeline])

    assert await kernel.run_tick("telegram:1") is None
    assert isinstance(kernel.last_result, ProactiveTickResult)
    assert pipeline.run_count == 0
    assert pipeline.slots is None
