"""C15 work queue 冻结外部默认值的单一来源（ADR-3/ADR-7）。

重试预算（`max_attempts` + 退避表）与轮询背压参数散落多处会各自漂移
（worker 默认、仓储默认参数、config 默认与校验、文档）。本模块是它们的
**唯一字面量来源**；各消费方 import 常量而不是重写字面量：

- `bootstrap/work_queue_worker.py::WorkQueueWorkerConfig`（运行时默认）
- `bootstrap/db/repository/control_plane_repo.py::record_work_failed`（默认参数）
- `agent/config_models.py::WorkQueueConfig`（配置默认）
- `agent/config.py::_load_work_queue_config`（加载期校验上界）

本模块**无任何 import**（纯常量），故可在 agent 与 bootstrap 两侧安全引用，
不引入依赖环。
"""

from __future__ import annotations

# ── 重试预算（ADR-3 定案 (B) / ADR-7 冻结初始值）───────────────────────────
DEFAULT_MAX_ATTEMPTS: int = 5
"""业务失败最大尝试次数；超过即进 `failed` 死信终态（claim 的 due 判据不含
`failed`，死信不再被认领，仅 redrive 可回）。"""

DEFAULT_BACKOFF_SECONDS: tuple[float, ...] = (60.0, 300.0, 1800.0, 7200.0, 21600.0)
"""失败重试退避表（1m/5m/30m/2h/6h），按 `attempt_count - 1` 取档。"""

# ── 轮询与背压（ADR-7 冻结初始值）─────────────────────────────────────────
DEFAULT_LEASE_TTL_SECONDS: float = 60.0
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: float = 20.0
DEFAULT_POLL_INTERVAL_SECONDS: float = 1.0
DEFAULT_BATCH_SIZE: int = 10
DEFAULT_MAINTENANCE_ACQUIRE_TIMEOUT_SECONDS: float = 5.0
DEFAULT_RELEASE_DELAY_SECONDS: float = 60.0
DEFAULT_ERROR_BACKOFF_SECONDS: float = 5.0
DEFAULT_MAX_ERROR_BACKOFF_SECONDS: float = 60.0

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_LEASE_TTL_SECONDS",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAINTENANCE_ACQUIRE_TIMEOUT_SECONDS",
    "DEFAULT_RELEASE_DELAY_SECONDS",
    "DEFAULT_ERROR_BACKOFF_SECONDS",
    "DEFAULT_MAX_ERROR_BACKOFF_SECONDS",
]