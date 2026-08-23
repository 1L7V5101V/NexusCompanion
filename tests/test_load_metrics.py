"""负载工具把 turn 指标记录进进程内 MetricRegistry（C0 任务 4.2 观测面）。

drive_turns 在传入 metric_registry 时，注册内建 turn 指标族并把每轮成功/耗时写入，
保证负载运行后的 /metrics 导出包含真实 turn 数据（C1B/C1D 复用同一 Registry）。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from agent.config_models import StorageConfig
from core.telemetry.builtin import register_builtin_metrics
from core.telemetry.metrics import MetricRegistry
from scripts.load.driver import build_core, drive_turns
from scripts.load.result import LoadSpec


def test_drive_turns_records_turn_metrics(tmp_path: Path) -> None:
    tenant = f"metric_{uuid.uuid4().hex[:6]}"
    spec = LoadSpec(
        scenario="simulate",
        backend="sqlite",
        tenants=(tenant,),
        channels=("cli", "telegram"),
        rounds=2,
        concurrency=2,
        reasoner="script",
        seed=42,
        workspace=str(tmp_path),
    )
    registry = MetricRegistry()
    builtin = register_builtin_metrics(registry)
    core, _sm, runtime = build_core(
        workspace=tmp_path,
        storage=StorageConfig(backend="sqlite"),
        spec=spec,
    )
    try:
        stats = asyncio.run(drive_turns(core, spec, metric_registry=registry))
    finally:
        runtime.close()

    assert stats.overall.attempted == 4
    assert stats.overall.succeeded == 4
    # 每个 (channel, 轮) 都记录一次 turns_total，且 turn 耗时直方图有 count。
    assert builtin.turns_total.get(labels={"channel": "cli"}) == 2.0
    assert builtin.turns_total.get(labels={"channel": "telegram"}) == 2.0
    # timer 按 channel 分系列，两个 channel 各 2 轮，跨系列计数之和为 4。
    duration_samples = builtin.turn_duration_seconds.snapshot()
    assert sum(s["count"] for s in duration_samples) == 4
