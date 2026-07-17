from __future__ import annotations

import logging
from collections.abc import Iterable

from proactive_v2.frame import ProactiveFrame

logger = logging.getLogger(__name__)


class ProactivePhaseRunner:
    """Stub: 主动 phaser runner — 遍历模块执行各 tick 阶段。"""

    def __init__(self, modules: Iterable[object]) -> None:
        self.modules_by_phase: dict[str, list[object]] = {
            "default": list(modules),
        }
        logger.debug(
            "ProactivePhaseRunner initialized with %d modules",
            len(self.modules_by_phase["default"]),
        )

    async def run(self, frame: ProactiveFrame) -> ProactiveFrame:
        if frame.output is None:
            from proactive_v2.frame import ProactiveTickResult

            frame.output = ProactiveTickResult()
        return frame

    def inspect(self) -> str:
        return (
            "ProactivePhaseRunner (stub)\n"
            f"  phases: {list(self.modules_by_phase)}\n"
            f"  modules: {sum(len(v) for v in self.modules_by_phase.values())}"
        )
