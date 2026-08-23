"""进程内指标注册：counter / histogram / timer。

自描述（name/type/help/labels），单锁保护更新，导出（JSON / Prometheus 文本）
在 metrics_export.py。Metric 对象保留后续换 prometheus-client 的 seam（D1）。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterable, Mapping, TypeVar

__all__ = [
    "DEFAULT_HISTOGRAM_BUCKETS",
    "Metric",
    "Counter",
    "Histogram",
    "Timer",
    "MetricRegistry",
    "get_default_registry",
]

# 默认直方图桶上界（秒）：覆盖 sub-ms 到分钟级 turn/存储耗时。
DEFAULT_HISTOGRAM_BUCKETS: tuple[float, ...] = (
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    float("inf"),
)

MetricT = TypeVar("MetricT", bound="Metric")


def _key_from_labels(
    label_names: tuple[str, ...],
    labels: Mapping[str, str] | None,
) -> tuple[str, ...]:
    """把调用方 labels dict 规约成与 label_names 对齐的有序 tuple 作为存储 key。

    未提供 labels 时若定义了 label_names 视为缺省空值（全部为空串）。
    """
    if labels is None:
        return ("",) * len(label_names)
    return tuple(str(labels.get(name, "")) for name in label_names)


class Metric:
    """自描述指标基类：name / type / help / label_names，单锁保护更新。"""

    type: str = "untyped"

    def __init__(
        self,
        name: str,
        help_text: str = "",
        *,
        label_names: Iterable[str] = (),
    ) -> None:
        if not name:
            raise ValueError("metric name must not be empty")
        self.name = name
        self.help = help_text
        self.label_names = tuple(label_names)
        self._lock = threading.RLock()

    def snapshot(self) -> list[dict[str, Any]]:
        raise NotImplementedError


class Counter(Metric):
    """单调递增计数器。"""

    type = "counter"

    def __init__(
        self,
        name: str,
        help_text: str = "",
        *,
        label_names: Iterable[str] = (),
    ) -> None:
        super().__init__(name, help_text, label_names=label_names)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, *, labels: Mapping[str, str] | None = None) -> None:
        key = _key_from_labels(self.label_names, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def get(self, *, labels: Mapping[str, str] | None = None) -> float:
        key = _key_from_labels(self.label_names, labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._values.items())
        return [
            {
                "name": self.name,
                "type": self.type,
                "help": self.help,
                "label_names": list(self.label_names),
                "labels": dict(zip(self.label_names, key)),
                "value": value,
            }
            for key, value in sorted(items)
        ]


class Histogram(Metric):
    """直方图：cumulative 桶计数 + sum + count（Prometheus 语义）。"""

    type = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str = "",
        *,
        label_names: Iterable[str] = (),
        buckets: Iterable[float] = DEFAULT_HISTOGRAM_BUCKETS,
    ) -> None:
        super().__init__(name, help_text, label_names=label_names)
        self.buckets: tuple[float, ...] = tuple(sorted(float(b) for b in buckets))
        if not self.buckets or self.buckets[-1] != float("inf"):
            self.buckets = self.buckets + (float("inf"),)
        # key -> (count, sum, [bucket_count, ...])
        self._states: dict[tuple[str, ...], tuple[int, float, list[int]]] = {}

    def observe(self, value: float, *, labels: Mapping[str, str] | None = None) -> None:
        key = _key_from_labels(self.label_names, labels)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = (0, 0.0, [0] * len(self.buckets))
            count, total, bucket_counts = state
            count += 1
            total += float(value)
            for i, upper in enumerate(self.buckets):
                if value <= upper:
                    bucket_counts[i] += 1
            self._states[key] = (count, total, bucket_counts)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._states.items())
        return [
            {
                "name": self.name,
                "type": self.type,
                "help": self.help,
                "label_names": list(self.label_names),
                "labels": dict(zip(self.label_names, key)),
                "count": count,
                "sum": total,
                "buckets": [
                    {"le": upper, "count": bucket_counts[i]}
                    for i, upper in enumerate(self.buckets)
                ],
            }
            for key, (count, total, bucket_counts) in sorted(items)
        ]


class Timer(Histogram):
    """秒表：内部即直方图，追加 time() 上下文管理器记录耗时（秒）。"""

    type = "timer"

    def time(self, labels: Mapping[str, str] | None = None) -> "_TimerScope":
        return _TimerScope(self, labels)


class _TimerScope:
    """上下文管理器：进入时记起点，退出时把耗时 observe 到 Timer。"""

    def __init__(
        self,
        timer: Timer,
        labels: Mapping[str, str] | None,
    ) -> None:
        self._timer = timer
        self._labels = labels
        self._start = 0.0

    def __enter__(self) -> "_TimerScope":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self._timer.observe(time.perf_counter() - self._start, labels=self._labels)


class MetricRegistry:
    """进程内指标注册表：register / 便捷工厂 / 全量快照。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._metrics: dict[str, Metric] = {}

    def register(self, metric: MetricT) -> MetricT:
        with self._lock:
            if metric.name in self._metrics:
                raise ValueError(f"metric already registered: {metric.name!r}")
            self._metrics[metric.name] = metric
        return metric

    def counter(
        self,
        name: str,
        help_text: str = "",
        *,
        label_names: Iterable[str] = (),
    ) -> Counter:
        return self.register(Counter(name, help_text, label_names=label_names))

    def histogram(
        self,
        name: str,
        help_text: str = "",
        *,
        label_names: Iterable[str] = (),
        buckets: Iterable[float] = DEFAULT_HISTOGRAM_BUCKETS,
    ) -> Histogram:
        return self.register(
            Histogram(name, help_text, label_names=label_names, buckets=buckets)
        )

    def timer(
        self,
        name: str,
        help_text: str = "",
        *,
        label_names: Iterable[str] = (),
        buckets: Iterable[float] = DEFAULT_HISTOGRAM_BUCKETS,
    ) -> Timer:
        return self.register(
            Timer(name, help_text, label_names=label_names, buckets=buckets)
        )

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            names = list(self._metrics)
        samples: list[dict[str, Any]] = []
        for name in names:
            metric = self._metrics[name]
            samples.extend(metric.snapshot())
        return samples

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._metrics)

    def get(self, name: str) -> Metric | None:
        """按名取已注册 metric（未注册返回 None），供幂等注册/读取使用。"""
        with self._lock:
            return self._metrics.get(name)


_default_registry: MetricRegistry | None = None


def get_default_registry() -> MetricRegistry:
    """进程级默认注册表（D1「进程内 MetricRegistry」）。

    dashboard / 负载工具等同一进程内共用该实例，保证 `/metrics` 导出的是进程内
    已注册指标的全量快照。
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = MetricRegistry()
    return _default_registry
