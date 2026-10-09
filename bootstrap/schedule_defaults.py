"""C11 显式 schedule 的冻结默认值单一来源（c11-explicit-schedules ADR-3/ADR-4）。

misfire 宽限与 tick 周期同时被 legacy 调度服务和 durable 调度服务读取，散落两处
会漂移。本模块**无任何 import**，故 agent 与 bootstrap 两侧可安全引用。

- `agent/scheduler.py::SchedulerService.GRACE_SECONDS`（legacy JSON 路径）
- `agent/config_models.py::SchedulerConfig.misfire_grace_seconds`（配置默认）
- `bootstrap/schedule_durable.py::DurableSchedulerService`（tick 周期）
"""

from __future__ import annotations

DEFAULT_MISFIRE_GRACE_SECONDS: int = 300
"""one-shot 超过该宽限即记 `missed`（§10 PROPOSED DEFAULT：5 分钟）。无论数值如何，
miss/attempt/outcome 一律持久化——grace 只决定「补执行还是记账」，不决定「是否留痕」。"""

DEFAULT_TICK_INTERVAL_SECONDS: float = 1.0
"""durable 调度服务的轮询周期，与 legacy 的每秒 tick 对齐。"""

__all__ = [
    "DEFAULT_MISFIRE_GRACE_SECONDS",
    "DEFAULT_TICK_INTERVAL_SECONDS",
]
