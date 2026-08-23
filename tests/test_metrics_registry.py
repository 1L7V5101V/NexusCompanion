"""MetricRegistry 单测：类型注册、更新、快照线程安全。"""

import threading

import pytest

from core.telemetry.metrics import (
    Counter,
    Histogram,
    MetricRegistry,
    Timer,
)


def _snapshot_by_name(registry: MetricRegistry) -> dict[str, list[dict]]:
    by_name: dict[str, list[dict]] = {}
    for sample in registry.snapshot():
        by_name.setdefault(sample["name"], []).append(sample)
    return by_name


def test_counter_inc_and_snapshot() -> None:
    registry = MetricRegistry()
    counter = registry.counter("turns_total", "total passive turns")
    counter.inc()
    counter.inc(3)
    samples = _snapshot_by_name(registry)["turns_total"]
    assert len(samples) == 1
    assert samples[0]["type"] == "counter"
    assert samples[0]["help"] == "total passive turns"
    assert samples[0]["value"] == 4.0
    assert counter.get() == 4.0


def test_counter_rejects_duplicate_name() -> None:
    registry = MetricRegistry()
    registry.counter("dup_total", "")
    try:
        registry.histogram("dup_total", "")
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate metric name must raise ValueError")


def test_counter_labels_are_isolated() -> None:
    registry = MetricRegistry()
    counter = registry.counter("by_channel_total", "", label_names=("channel",))
    counter.inc(1, labels={"channel": "telegram"})
    counter.inc(5, labels={"channel": "qq"})
    samples = _snapshot_by_name(registry)["by_channel_total"]
    by_label = {s["labels"]["channel"]: s["value"] for s in samples}
    assert by_label == {"telegram": 1.0, "qq": 5.0}
    assert counter.get(labels={"channel": "telegram"}) == 1.0


def test_histogram_observe_and_cumulative_buckets() -> None:
    registry = MetricRegistry()
    hist = registry.histogram(
        "turn_seconds",
        "turn duration",
        buckets=(0.01, 0.1, 1.0, float("inf")),
    )
    hist.observe(0.005)
    hist.observe(0.05)
    hist.observe(5.0)
    samples = _snapshot_by_name(registry)["turn_seconds"]
    assert len(samples) == 1
    sample = samples[0]
    assert sample["type"] == "histogram"
    assert sample["count"] == 3
    assert sample["sum"] == pytest.approx(5.055)
    le_counts = {b["le"]: b["count"] for b in sample["buckets"]}
    # cumulative：<=0.01 一个、<=0.1 两个、<=1.0 两个、+Inf 三个
    assert le_counts[0.01] == 1
    assert le_counts[0.1] == 2
    assert le_counts[1.0] == 2
    assert le_counts[float("inf")] == 3


def test_timer_records_elapsed() -> None:
    registry = MetricRegistry()
    timer = registry.timer("store_seconds", "store op duration")
    with timer.time():
        pass
    samples = _snapshot_by_name(registry)["store_seconds"]
    assert len(samples) == 1
    sample = samples[0]
    assert sample["type"] == "timer"
    assert sample["count"] == 1
    assert sample["sum"] >= 0.0


def test_registry_snapshot_names_sorted() -> None:
    registry = MetricRegistry()
    registry.counter("b_total", "")
    registry.counter("a_total", "")
    assert registry.names() == ["a_total", "b_total"]


def test_snapshot_thread_safe_concurrent_inc() -> None:
    registry = MetricRegistry()
    counter = registry.counter("threads_total", "")
    total = 2000
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(total):
                counter.inc()
        except BaseException as exc:  # noqa: BLE001 - 收集线程异常供断言
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"thread exceptions: {errors}"
    assert counter.get() == float(total * 8)
    # 快照在并发写后仍可完整读取
    samples = _snapshot_by_name(registry)["threads_total"]
    assert len(samples) == 1


def test_registry_types_self_describing() -> None:
    registry = MetricRegistry()
    counter = registry.counter("c_total", "counter help")
    hist = registry.histogram("h_seconds", "hist help")
    timer = registry.timer("t_seconds", "timer help")
    assert isinstance(counter, Counter)
    assert isinstance(hist, Histogram)
    assert isinstance(timer, Timer)
    for sample in registry.snapshot():
        assert sample["name"]
        assert sample["help"]
        assert sample["type"] in {"counter", "histogram", "timer"}


def test_registry_get_returns_metric_or_none() -> None:
    registry = MetricRegistry()
    assert registry.get("missing") is None
    counter = registry.counter("g_total", "get accessor")
    found = registry.get("g_total")
    assert found is not None
    assert found is counter
    assert found.name == "g_total"
