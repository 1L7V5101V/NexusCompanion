"""C12 metrics label 白名单契约测试（ADR-4，task-12 验收第 2 条）。

代码 allowlist 与 fixture `tests/fixtures/metric_label_policy.json` 交叉校验；
高基数身份字段与内容字段注册即失败。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.telemetry.builtin import register_builtin_metrics
from core.telemetry.label_policy import (
    ALLOWED_METRIC_LABELS,
    FORBIDDEN_CONTENT_LABELS,
    FORBIDDEN_IDENTITY_LABELS,
    MetricLabelPolicyError,
    PolicyCheckedMetricRegistry,
    validate_label_names,
)
from core.telemetry.metrics import Counter

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "metric_label_policy.json"
)


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_fixture_allowlist_matches_code():
    data = _fixture()
    allowed = data["allowed_labels"]
    assert isinstance(allowed, list)
    assert set(allowed) == set(ALLOWED_METRIC_LABELS)


def test_forbidden_never_in_allowlist():
    assert FORBIDDEN_IDENTITY_LABELS & ALLOWED_METRIC_LABELS == frozenset()
    assert FORBIDDEN_CONTENT_LABELS & ALLOWED_METRIC_LABELS == frozenset()
    data = _fixture()
    forbidden = data["forbidden_labels"]
    assert isinstance(forbidden, dict)
    all_forbidden: set[str] = set()
    for group in forbidden.values():
        assert isinstance(group, list)
        all_forbidden.update(group)
    assert all_forbidden & set(ALLOWED_METRIC_LABELS) == set()


def test_validate_label_names_rejects_high_cardinality_and_content():
    with pytest.raises(MetricLabelPolicyError):
        validate_label_names(("message_id",))
    with pytest.raises(MetricLabelPolicyError):
        validate_label_names(("tool_args",))
    with pytest.raises(MetricLabelPolicyError):
        validate_label_names(("account_id", "session_key"))


def test_validate_label_names_accepts_allowlist_and_empty():
    validate_label_names(
        ("work_kind", "flow", "stage", "tenant_id", "channel", "model")
    )
    validate_label_names(())


def test_registry_rejects_forbidden_label_at_registration():
    registry = PolicyCheckedMetricRegistry()
    with pytest.raises(MetricLabelPolicyError):
        registry.counter("bad_metric", label_names=("message_id",))
    with pytest.raises(MetricLabelPolicyError):
        registry.timer("bad_timer", label_names=("tool_result",))
    assert "bad_metric" not in registry.names()


def test_registry_accepts_allowlist_labels():
    registry = PolicyCheckedMetricRegistry()
    counter = registry.counter(
        "turns_total_test", "test", label_names=("channel", "work_kind")
    )
    counter.inc(labels={"channel": "webchat", "work_kind": "interactive"})
    assert counter.get(labels={"channel": "webchat", "work_kind": "interactive"}) == 1


def test_c0_builtin_metrics_compatible():
    """C0 既有指标族（channel/backend label）在新策略下可正常注册与写入。"""
    registry = PolicyCheckedMetricRegistry()
    handle = register_builtin_metrics(registry)
    handle.turns_total.inc(labels={"channel": "telegram"})
    assert handle.turns_total.get(labels={"channel": "telegram"}) == 1


def test_direct_register_path_also_validated():
    registry = PolicyCheckedMetricRegistry()
    with pytest.raises(MetricLabelPolicyError):
        registry.register(Counter("bad_direct", label_names=("session_key",)))
