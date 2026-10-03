"""C12 §8.4 retention 接线（p0-retention-wiring）。

`RetentionSweeper` 是 PG 行级裁剪 + 文件 sweep 的单轮编排；`RetentionRuntime`
是进程内周期任务壳（形状对齐 C6 `AttachmentLifecycleRuntime`）。
"""

from bootstrap.retention.runtime import RetentionRuntime
from bootstrap.retention.sweeper import (
    IRRECOVERABLE,
    RECOVERABLE,
    EntityReport,
    RetentionRunReport,
    RetentionSweeper,
)

__all__ = [
    "IRRECOVERABLE",
    "RECOVERABLE",
    "EntityReport",
    "RetentionRunReport",
    "RetentionRuntime",
    "RetentionSweeper",
]
