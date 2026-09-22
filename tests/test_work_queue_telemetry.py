"""C15 work item 遥测：契约、内容边界与指标 label 白名单（无需 PG）。

契约单一来源 = `tests/fixtures/observability_event_schema.json`（C12 ADR-7）。
本文件是「实现 ↔ fixture」的 diff 检查，即 C12 要求各 Cxx 落地时提供的评审输入。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bootstrap.work_queue_telemetry import (
    ALLOWED_EVENT_FIELDS,
    WorkQueueTelemetry,
    _emit,
    register_work_queue_metrics,
)
from core.telemetry.label_policy import (
    FORBIDDEN_CONTENT_LABELS,
    FORBIDDEN_IDENTITY_LABELS,
    MetricLabelPolicyError,
    PolicyCheckedMetricRegistry,
)
from core.telemetry.metrics import MetricRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "observability_event_schema.json").read_text(
        encoding="utf-8"
    )
)
_LIFECYCLE = FIXTURE["lifecycle_event"]
FIXTURE_FIELDS = frozenset(
    field
    for fields in _LIFECYCLE["field_groups"].values()
    for field in fields
) | frozenset(_LIFECYCLE["derived_metrics"])
FORBIDDEN_CONTENT_FIELDS = frozenset(_LIFECYCLE["forbidden_content_fields"])

_T0 = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)


def _telemetry(metrics=None) -> WorkQueueTelemetry:
    return WorkQueueTelemetry(metrics)


# ── 契约：实现与 fixture 一致（ADR-7） ──


def test_allowed_event_fields_equal_fixture() -> None:
    """`ALLOWED_EVENT_FIELDS` 必须与 fixture 的 field_groups ∪ derived_metrics 完全一致。

    双向断言：既不能少（漏字段），也不能多（自造字段绕过契约）。
    """
    assert ALLOWED_EVENT_FIELDS == FIXTURE_FIELDS


def test_emitted_events_are_subset_of_fixture() -> None:
    """三个记录点产出的事件字段都必须落在 fixture 声明内。"""
    started = _T0
    events = [
        _telemetry().claim(
            work_id="w1",
            tenant_id="t1",
            work_kind="maintenance",
            flow="consolidation",
            enqueued_at=(started - timedelta(seconds=3)).isoformat(),
            started_at=started,
        ),
        _telemetry().finish(
            work_id="w1",
            tenant_id="t1",
            work_kind="maintenance",
            flow="consolidation",
            status="succeeded",
            started_at=started,
            finished_at=started + timedelta(seconds=2),
            attempt=1,
        ),
        _telemetry().recovery(
            work_id="w1",
            tenant_id="t1",
            work_kind="maintenance",
            flow="consolidation",
            recovery_action="recompute",
            restart_started_at=started,
            recovered_at=(started + timedelta(milliseconds=500)).isoformat(),
        ),
    ]
    for event in events:
        assert set(event) <= FIXTURE_FIELDS, f"事件含 fixture 之外的字段: {event}"


def test_no_forbidden_content_field_can_appear() -> None:
    """负向：fixture 的 forbidden_content_fields 结构上不可能出现在事件里。"""
    assert not (ALLOWED_EVENT_FIELDS & FORBIDDEN_CONTENT_FIELDS)
    for name in ("payload", "payload_json", "tool_args", "message_content", "full_prompt"):
        assert name not in ALLOWED_EVENT_FIELDS


def test_non_whitelisted_field_is_dropped_not_passed_through() -> None:
    """非白名单字段被丢弃并报错，而不是放行（防泄露 + 不让遥测打挂 worker）。"""
    event = _emit(
        "work_item.finish",
        {
            "work_id": "w1",
            "status": "succeeded",
            "payload": {"secret": "should-not-appear"},
            "tool_result": "should-not-appear",
        },
    )
    assert "payload" not in event
    assert "tool_result" not in event
    assert event["work_id"] == "w1"


# ── 派生耗时（§7.1 derived_metrics） ──


def test_claim_derives_queue_wait_ms() -> None:
    event = _telemetry().claim(
        work_id="w1",
        tenant_id="t1",
        work_kind="maintenance",
        flow="consolidation",
        enqueued_at=(_T0 - timedelta(seconds=1.5)).isoformat(),
        started_at=_T0,
    )
    assert event["queue_wait_ms"] == 1500.0
    assert event["status"] == "in_progress"


def test_finish_derives_execution_ms() -> None:
    event = _telemetry().finish(
        work_id="w1",
        tenant_id="t1",
        work_kind="maintenance",
        flow="consolidation",
        status="succeeded",
        started_at=_T0,
        finished_at=_T0 + timedelta(seconds=2.25),
        attempt=1,
    )
    assert event["execution_ms"] == 2250.0


def test_recovery_derives_restart_to_recovered_ms() -> None:
    event = _telemetry().recovery(
        work_id="w1",
        tenant_id="t1",
        work_kind="maintenance",
        flow="consolidation",
        recovery_action="recompute",
        restart_started_at=_T0,
        recovered_at=(_T0 + timedelta(milliseconds=250)).isoformat(),
    )
    assert event["restart_to_recovered_ms"] == 250.0
    assert event["recovery_action"] == "recompute"


# ── 内容边界：自由文本脱敏 ──


def test_error_text_is_redacted() -> None:
    """失败原因里的 secret / 本地路径入库前必须脱敏（C12 redaction_rule）。"""
    event = _telemetry().finish(
        work_id="w1",
        tenant_id="t1",
        work_kind="maintenance",
        flow="consolidation",
        status="failed",
        started_at=_T0,
        finished_at=_T0 + timedelta(seconds=1),
        error_type="RuntimeError",
        retryable=True,
        error=(
            "provider 拒绝: api_key=sk-abcdefghijklmnopqrstuvwxyz "
            "cache=C:\\Users\\alice\\.nexus\\secret.json"
        ),
    )
    last_error = event["last_error"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in last_error
    assert "C:\\Users\\alice" not in last_error
    assert "[REDACTED:secret]" in last_error
    assert "[REDACTED:local_path]" in last_error


def test_missing_error_is_omitted() -> None:
    event = _telemetry().finish(
        work_id="w1",
        tenant_id="t1",
        work_kind="maintenance",
        flow="consolidation",
        status="succeeded",
        started_at=_T0,
        finished_at=_T0,
    )
    assert "last_error" not in event


# ── 指标：注册幂等 + label 白名单 ──


def test_register_metrics_is_idempotent() -> None:
    registry = MetricRegistry()
    first = register_work_queue_metrics(registry)
    second = register_work_queue_metrics(registry)
    assert first.work_items_total is second.work_items_total
    assert first.work_item_execution_seconds is second.work_item_execution_seconds


def test_register_metrics_passes_policy_checked_registry() -> None:
    """注册进 `PolicyCheckedMetricRegistry` 不抛错 = 所有 label 都在 C12 白名单内。"""
    metrics = register_work_queue_metrics(PolicyCheckedMetricRegistry())
    assert metrics.work_queue_recovery_total is not None


def test_high_cardinality_identity_labels_are_rejected_by_policy() -> None:
    """负向：证明策略守卫是真的——高基数身份字段作 label 会被拒。"""
    for forbidden in ("work_id", "session_key"):
        assert forbidden in FORBIDDEN_IDENTITY_LABELS
        with pytest.raises(MetricLabelPolicyError):
            PolicyCheckedMetricRegistry().counter(
                "c15_probe_total", "probe", label_names=(forbidden,)
            )


def test_content_labels_are_rejected_by_policy() -> None:
    for forbidden in ("tool_args", "message_content"):
        assert forbidden in FORBIDDEN_CONTENT_LABELS
        with pytest.raises(MetricLabelPolicyError):
            PolicyCheckedMetricRegistry().timer(
                "c15_probe_seconds", "probe", label_names=(forbidden,)
            )


def test_metrics_record_with_whitelisted_labels() -> None:
    """三个记录点都能在真实 registry 上落值（labels 合法且不抛错）。"""
    registry = PolicyCheckedMetricRegistry()
    telemetry = _telemetry(register_work_queue_metrics(registry))
    telemetry.claim(
        work_id="w1",
        tenant_id="t1",
        work_kind="maintenance",
        flow="consolidation",
        enqueued_at=_T0.isoformat(),
        started_at=_T0,
    )
    telemetry.finish(
        work_id="w1",
        tenant_id="t1",
        work_kind="maintenance",
        flow="consolidation",
        status="succeeded",
        started_at=_T0,
        finished_at=_T0 + timedelta(seconds=1),
        attempt=1,
    )
    telemetry.recovery(
        work_id="w1",
        tenant_id="t1",
        work_kind="maintenance",
        flow="consolidation",
        recovery_action="recompute",
        restart_started_at=_T0,
        recovered_at=_T0.isoformat(),
    )
    names = set(registry.names())
    assert {
        "work_items_total",
        "work_item_queue_wait_seconds",
        "work_item_execution_seconds",
        "work_queue_recovery_total",
    } <= names
    snapshot = registry.snapshot()
    # label 用了白名单维度（有界枚举），且**绝不含**高基数身份字段/内容字段
    for entry in snapshot:
        labels = set(entry["labels"])
        assert not (labels & FORBIDDEN_IDENTITY_LABELS), f"指标 label 含身份字段: {labels}"
        assert not (labels & FORBIDDEN_CONTENT_LABELS), f"指标 label 含内容字段: {labels}"
    items = [entry for entry in snapshot if entry["name"] == "work_items_total"]
    assert items, "收束计数应至少有一个 label 组合"
    assert items[0]["labels"] == {
        "work_kind": "maintenance",
        "flow": "consolidation",
        "status": "succeeded",
    }
    assert items[0]["value"] == 1.0
