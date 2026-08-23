"""dashboard /metrics 导出端点（C0 任务 4.1）与 metric tile 数据源（4.2 后端侧）。

- 4.1：请求 /metrics 返回 200 与可解析的 JSON / Prometheus 文本（spec 场景「导出端点可达」）。
- 4.2：内建 turn/存储/迁移指标族经导出可见、含最新值（spec 场景「导出包含已注册指标」）；
  现有 SQLite 读路径（历史日志 / 交付记录）无回归。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bootstrap.dashboard_api import create_dashboard_app
from core.memory.engine import MemoryAdminApi
from core.telemetry.builtin import register_builtin_metrics
from core.telemetry.metrics import MetricRegistry


class _MemoryAdmin:
    """最小 memory_admin 桩：/metrics 与 SQLite 读路径不触碰 memory 写接口。"""

    class _Desc:
        name = "stub"

    def describe(self) -> _Desc:
        return self._Desc()

    def close(self) -> None:
        return None


def _make_app(tmp_path: Path, registry: MetricRegistry) -> FastAPI:
    return create_dashboard_app(
        tmp_path,
        memory_admin=cast(MemoryAdminApi, _MemoryAdmin()),
        metric_registry=registry,
    )


@pytest.fixture()
def registry() -> MetricRegistry:
    return MetricRegistry()


def _strict_json_loads(text: str) -> Any:
    """严格 JSON 解析：拒绝 Infinity / NaN 字面量（与浏览器 JSON.parse 一致）。

    Python json.loads 默认宽容接受 Infinity/NaN，浏览器会抛错——导出必须对
    两种消费者都合法。
    """
    return json.loads(
        text,
        parse_constant=lambda c: (_ for _ in ()).throw(ValueError(f"non-JSON constant: {c}")),
    )


def test_metrics_prometheus_text_ok_and_parseable(tmp_path: Path, registry: MetricRegistry) -> None:
    """4.1 Prometheus 文本：200 + text/plain，已记录指标含 HELP/TYPE 与样本行。"""
    builtin = register_builtin_metrics(registry)
    builtin.turns_total.inc(3, labels={"channel": "cli"})
    builtin.turn_duration_seconds.observe(0.05, labels={"channel": "cli"})
    app = _make_app(tmp_path, registry)
    with TestClient(app) as client:
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "# HELP turns_total " in body
    assert "# TYPE turns_total counter" in body
    assert 'turns_total{channel="cli"} 3.0' in body
    assert "# TYPE turn_duration_seconds histogram" in body


def test_metrics_json_ok_and_parseable(tmp_path, registry: MetricRegistry) -> None:
    """4.1 JSON：200 + 可 json.loads，含已记录指标的最新值。"""
    builtin = register_builtin_metrics(registry)
    builtin.turns_total.inc(3, labels={"channel": "cli"})
    builtin.turn_duration_seconds.observe(0.05, labels={"channel": "cli"})
    app = _make_app(tmp_path, registry)
    with TestClient(app) as client:
        resp = client.get("/metrics", params={"format": "json"})
    assert resp.status_code == 200
    samples = _strict_json_loads(resp.text)
    assert isinstance(samples, list)
    # +Inf bucket 上界在导出中必须是 null（JSON 无 Infinity 字面量，浏览器拒绝）
    inf_bucket = [
        b
        for s in samples
        if s["name"] == "turn_duration_seconds"
        for b in s["buckets"]
        if b["le"] is None
    ]
    assert len(inf_bucket) == 1
    by_name: dict[str, list[dict]] = {}
    for sample in samples:
        by_name.setdefault(sample["name"], []).append(sample)
    assert any(
        s["labels"] == {"channel": "cli"} and s["value"] == 3.0
        for s in by_name["turns_total"]
    )
    dur = by_name["turn_duration_seconds"][0]
    assert dur["count"] == 1
    assert dur["sum"] == pytest.approx(0.05)


def test_exported_metrics_reflect_latest_recorded_values(tmp_path, registry: MetricRegistry) -> None:
    """spec 场景「导出包含已注册指标」：注册并更新后两格式均含最新值。"""
    builtin = register_builtin_metrics(registry)
    builtin.turns_total.inc(1, labels={"channel": "cli"})
    builtin.turns_total.inc(4, labels={"channel": "cli"})
    builtin.turns_total.inc(2, labels={"channel": "telegram"})
    app = _make_app(tmp_path, registry)
    with TestClient(app) as client:
        prom = client.get("/metrics").text
        js = _strict_json_loads(client.get("/metrics", params={"format": "json"}).text)
    assert 'turns_total{channel="cli"} 5.0' in prom
    assert 'turns_total{channel="telegram"} 2.0' in prom
    turns = [s for s in js if s["name"] == "turns_total"]
    values = {s["labels"]["channel"]: s["value"] for s in turns}
    assert values == {"cli": 5.0, "telegram": 2.0}


def test_metric_registry_isolation(tmp_path) -> None:
    """显式传入的 registry 是 /metrics 唯一数据源，不污染进程默认注册表。"""
    registry = MetricRegistry()
    register_builtin_metrics(registry).turns_total.inc(7, labels={"channel": "cli"})
    app = _make_app(tmp_path, registry)
    with TestClient(app) as client:
        js = _strict_json_loads(client.get("/metrics", params={"format": "json"}).text)
    values = [s["value"] for s in js if s["name"] == "turns_total"]
    assert values == [7.0]


def test_existing_sqlite_read_paths_no_regression(tmp_path, registry: MetricRegistry) -> None:
    """4.2 回归：挂载 /metrics 后，历史日志与交付/会话读路径仍可用。"""
    app = _make_app(tmp_path, registry)
    with TestClient(app) as client:
        logs = client.get("/api/dashboard/logs")
        sessions = client.get("/api/dashboard/sessions")
        metrics = client.get("/metrics")
    assert logs.status_code == 200
    assert logs.json()["items"] == []
    assert sessions.status_code == 200
    assert metrics.status_code == 200
