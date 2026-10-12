from __future__ import annotations

from plugins.default_proactive.runtime import ProactiveFlowRuntime, get_run_state
from proactive_v2.frame import ProactiveFrame


class DriftFlowModule:
    slot = "drift.flow"
    requires = ("route:selected",)
    produces = ("proposal:drift",)

    def __init__(self, runtime: ProactiveFlowRuntime) -> None:
        self._runtime = runtime

    async def run(self, frame: ProactiveFrame) -> ProactiveFrame:
        state = get_run_state(frame)
        # drift 的上下文帧在 `runtime.drift` 里构建，而那一层往下是同步的，所以预取必须
        # 在这个 async 入口做（c18 ADR-15）。与 proactive prepare 共用同一个方法，靠
        # ctx 上的标记保证一轮只解析一次。
        await self._runtime.prefetch_profile(state)
        await self._runtime.drift(state)
        frame.slots["proposal:drift"] = state.feed if state.ctx.drift_entered else None
        return frame


def build_drift_flow_modules(runtime: ProactiveFlowRuntime) -> list[object]:
    return [DriftFlowModule(runtime)]
