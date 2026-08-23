"""Metric export 单测：JSON 字段完整、Prometheus 文本格式可解析。"""

import json

from core.telemetry.metrics import MetricRegistry
from core.telemetry.metrics_export import export_json, export_prometheus_text


def _registry() -> MetricRegistry:
    reg = MetricRegistry()
    reg.counter("turns_total", "total passive turns").inc(3)
    hist = reg.histogram(
        "turn_seconds",
        "turn duration",
        buckets=(0.1, 1.0, float("inf")),
    )
    hist.observe(0.05)
    hist.observe(2.0)
    reg.counter("by_channel_total", "", label_names=("channel",)).inc(
        2, labels={"channel": "telegram"}
    )
    return reg


def test_export_json_complete() -> None:
    text = export_json(_registry().snapshot())
    data = json.loads(text)
    by_name = {s["name"]: s for s in data}
    assert "turns_total" in by_name
    assert by_name["turns_total"]["value"] == 3.0
    assert by_name["turns_total"]["type"] == "counter"
    assert by_name["turns_total"]["help"] == "total passive turns"
    hist = by_name["turn_seconds"]
    assert hist["type"] == "histogram"
    assert hist["count"] == 2
    assert hist["sum"] == 2.05
    assert len(hist["buckets"]) == 3
    channel = by_name["by_channel_total"]
    assert channel["labels"] == {"channel": "telegram"}


def test_export_prometheus_text_parseable() -> None:
    text = export_prometheus_text(_registry().snapshot())
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # 标准 exposition 结构：# HELP / # TYPE 在样本前
    assert lines[0] == "# HELP turns_total total passive turns"
    assert "# TYPE turns_total counter" in lines
    assert "# TYPE turn_seconds histogram" in lines
    # counter 样本行
    assert "turns_total 3.0" in lines
    # histogram：_bucket 含 le=，_sum/_count
    assert any(ln.startswith("turn_seconds_bucket{le=") for ln in lines)
    assert any(ln.startswith("turn_seconds_bucket{le=\"+Inf\"") for ln in lines)
    assert any(ln.startswith("turn_seconds_sum") for ln in lines)
    assert any(ln.startswith("turn_seconds_count") for ln in lines)
    # 带 labels 的 counter
    assert 'by_channel_total{channel="telegram"} 2.0' in lines


def test_export_prometheus_text_timer_as_histogram() -> None:
    reg = MetricRegistry()
    timer = reg.timer("store_seconds", "store op duration")
    with timer.time():
        pass
    text = export_prometheus_text(reg.snapshot())
    assert "# TYPE store_seconds histogram" in text
    assert any(ln.startswith("store_seconds_bucket{") for ln in text.splitlines())
    assert any(ln.startswith("store_seconds_count") for ln in text.splitlines())


def test_export_prometheus_text_label_escaping() -> None:
    reg = MetricRegistry()
    reg.counter("escaped_total", 'help with "quote"', label_names=("chan",)).inc(
        1, labels={"chan": 'a"b'}
    )
    text = export_prometheus_text(reg.snapshot())
    assert 'escaped_total{chan="a\\"b"} 1.0' in text
    assert 'help with \\"quote\\"' in text


def test_export_prometheus_text_empty_registry() -> None:
    text = export_prometheus_text(MetricRegistry().snapshot())
    assert text == "\n"
