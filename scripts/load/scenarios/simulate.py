"""simulate 场景：驱动 N 轮真实 passive turn（C0 任务 2.4 reasoner 模式）。

走真实 PassiveTurnPipeline 全 phase 链（before_turn → before_reasoning →
reasoner → after_reasoning → after_turn）+ 真实 StorageRuntime/SessionManager，
仅 reasoner 可按 spec.reasoner 注入：
- script：无真实 LLM 调用，支持 sim_latency_ms / sim_fail_rate；
- llm：真实 DefaultReasoner + config provider（小批量冒烟，需要 API key）。

workspace 缺省为仓库根下 .load-workspace/（sqlite 后端落盘位置）。
"""

from __future__ import annotations

from pathlib import Path

from scripts.load.result import LoadSpec, RunStats
from scripts.load.scenarios import LoadDeps, register


@register("simulate")
async def run(spec: LoadSpec, deps: LoadDeps) -> dict[str, RunStats]:
    if spec.dry_run:
        raise RuntimeError("simulate 场景要求执行真实 turn，不支持 --dry-run")

    from agent.config_models import StorageConfig
    from core.telemetry.metrics import get_default_registry
    from scripts.load.driver import build_core, drive_turns

    workspace = Path(spec.workspace or ".load-workspace")
    storage = StorageConfig(backend=spec.backend)
    core, _session_manager, runtime = build_core(
        workspace=workspace,
        storage=storage,
        spec=spec,
    )
    try:
        stats = await drive_turns(core, spec, metric_registry=get_default_registry())
    finally:
        runtime.close()
    return {spec.backend: stats}
