"""进程内建指标族：turn / 存储 / 迁移（C0 任务 4.2 展示口径）。

只注册 metric 定义（name/type/help/labels），值由真实事件记录者写入：
- turn：负载工具 drive_turns 在 turn 完成时记录（turns_total / turn_duration_seconds）；
- 存储/迁移：C0 不改迁移工具与存储写路径，此处仅注册定义，由 C1B/C1D 各自的
  记录点写入（本 change 不触碰写路径）。
register_builtin_metrics 幂等：对同一 registry 多次调用返回同一组句柄。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.telemetry.metrics import Counter, MetricRegistry, Timer

__all__ = ["BuiltinMetrics", "register_builtin_metrics"]


@dataclass(frozen=True)
class BuiltinMetrics:
    """内建指标族句柄。值字段留空表示该族当前未被记录（C1B/C1D 接入后才有值）。"""

    turns_total: Counter
    turn_duration_seconds: Timer
    storage_ops_total: Counter
    storage_op_duration_seconds: Timer
    migration_imported_rows_total: Counter
    migration_batch_duration_seconds: Timer


def register_builtin_metrics(registry: MetricRegistry) -> BuiltinMetrics:
    """幂等注册 turn/存储/迁移指标族，返回句柄（已注册则复用现有实例）。"""
    turns_total = registry.get("turns_total")
    if turns_total is None:
        turns_total = registry.counter(
            "turns_total",
            "处理的 turn 总数（按 channel 分）",
            label_names=("channel",),
        )
    assert isinstance(turns_total, Counter), "turns_total 必须注册为 counter"
    turn_duration_seconds = registry.get("turn_duration_seconds")
    if turn_duration_seconds is None:
        turn_duration_seconds = registry.timer(
            "turn_duration_seconds",
            "turn 处理耗时（秒）",
            label_names=("channel",),
        )
    assert isinstance(turn_duration_seconds, Timer), "turn_duration_seconds 必须注册为 timer"
    storage_ops_total = registry.get("storage_ops_total")
    if storage_ops_total is None:
        storage_ops_total = registry.counter(
            "storage_ops_total",
            "存储操作总数（按 backend 分）",
            label_names=("backend",),
        )
    assert isinstance(storage_ops_total, Counter), "storage_ops_total 必须注册为 counter"
    storage_op_duration_seconds = registry.get("storage_op_duration_seconds")
    if storage_op_duration_seconds is None:
        storage_op_duration_seconds = registry.timer(
            "storage_op_duration_seconds",
            "存储操作耗时（秒）",
            label_names=("backend",),
        )
    assert isinstance(storage_op_duration_seconds, Timer), "storage_op_duration_seconds 必须注册为 timer"
    migration_imported_rows_total = registry.get("migration_imported_rows_total")
    if migration_imported_rows_total is None:
        migration_imported_rows_total = registry.counter(
            "migration_imported_rows_total",
            "迁移导入行数总数（C1B 记录点写入）",
        )
    assert isinstance(migration_imported_rows_total, Counter), "migration_imported_rows_total 必须注册为 counter"
    migration_batch_duration_seconds = registry.get("migration_batch_duration_seconds")
    if migration_batch_duration_seconds is None:
        migration_batch_duration_seconds = registry.timer(
            "migration_batch_duration_seconds",
            "迁移批量处理耗时（秒）（C1B 记录点写入）",
        )
    assert isinstance(migration_batch_duration_seconds, Timer), "migration_batch_duration_seconds 必须注册为 timer"
    return BuiltinMetrics(
        turns_total=turns_total,
        turn_duration_seconds=turn_duration_seconds,
        storage_ops_total=storage_ops_total,
        storage_op_duration_seconds=storage_op_duration_seconds,
        migration_imported_rows_total=migration_imported_rows_total,
        migration_batch_duration_seconds=migration_batch_duration_seconds,
    )
