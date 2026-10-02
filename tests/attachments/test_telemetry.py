"""C6 attachment 事件记录点契约测试（C12 §8.1 伴随落地）。

对齐 C15/C2 模式（tests/test_webchat_telemetry.py）：
- 事件字段 ⊆ fixture 白名单（双向：白名单 = fixture，事件不得自造字段）；
- `status` 只取 fixture 冻结枚举；
- 指标注册 label ⊆ C12 label policy（`attachment_id` 注册被拒即为负向契约）；
- redact_text 兜底：error 自由文本过 redaction（secret 形如 sk-… 被掩码）、
  filename/路径/内容字段**结构上无法**进入事件（白名单构造而非过滤）；
- service 记录点：upload 成功/失败、fetch 成功/失败都触发对应事件。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bootstrap.attachments.telemetry import (
    AttachmentMetrics,
    AttachmentTelemetry,
    register_attachment_metrics,
)
from bootstrap.work_queue_telemetry import ALLOWED_EVENT_FIELDS
from core.telemetry.label_policy import (
    ALLOWED_METRIC_LABELS,
    MetricLabelPolicyError,
    validate_label_names,
)
from core.telemetry.metrics import MetricRegistry

FIXTURE = json.loads(
    (
        Path(__file__).parent.parent / "fixtures" / "observability_event_schema.json"
    ).read_text(encoding="utf-8")
)
FIXTURE_FIELDS = frozenset(
    FIXTURE["lifecycle_event"]["field_groups"]["identity"]
    + FIXTURE["lifecycle_event"]["field_groups"]["classification"]
    + FIXTURE["lifecycle_event"]["field_groups"]["version"]
    + FIXTURE["lifecycle_event"]["field_groups"]["lifecycle_time"]
    + FIXTURE["lifecycle_event"]["field_groups"]["lifecycle_result"]
    + FIXTURE["lifecycle_event"]["field_groups"]["side_effect_recovery"]
    + FIXTURE["lifecycle_event"]["field_groups"]["resource"]
    + list(FIXTURE["lifecycle_event"]["derived_metrics"])
)
FIXTURE_STATUS_ENUM = frozenset(FIXTURE["lifecycle_event"]["enums"]["status"])


def test_allowed_event_fields_equal_fixture() -> None:
    """双向断言：沿用 work_queue_telemetry 单一白名单且与 fixture 一致（不得扩）。"""
    assert ALLOWED_EVENT_FIELDS == FIXTURE_FIELDS


def test_upload_event_fields_within_fixture() -> None:
    telemetry = AttachmentTelemetry()
    event = telemetry.upload_finished(
        tenant_id="t1", status="succeeded", size_bytes=1024
    )
    assert set(event) <= ALLOWED_EVENT_FIELDS
    assert event["status"] == "succeeded"
    assert event["tenant_id"] == "t1"
    # size_bytes 不在白名单 → 不入事件（量化走 metric，不扩事件面）
    assert "size_bytes" not in event
    # 事件里不存在任何 filename/路径/内容键（白名单构造，结构上不可达）
    assert not any(k in event for k in ("filename", "storage_key", "path", "content"))


def test_upload_error_event_redacts_free_text() -> None:
    telemetry = AttachmentTelemetry()
    event = telemetry.upload_finished(
        tenant_id="t1",
        status="failed",
        error="reject sk-abcdef1234567890-upload",
        error_type="AttachmentError",
    )
    assert set(event) <= ALLOWED_EVENT_FIELDS
    assert event["error_type"] == "AttachmentError"
    # redact_text 兜底：secret 形自由文本被掩码
    assert "sk-abcdef1234567890" not in event.get("last_error", "")


def test_upload_status_outside_result_vocabulary_rejected() -> None:
    telemetry = AttachmentTelemetry()
    # 成功状态但无 error → 必须用已冻结词
    with pytest.raises(ValueError):
        telemetry.upload_finished(tenant_id="t1", status="bogus")  # type: ignore[arg-type]
    # 失败状态必须带 error（避免裸 failed 丢失根因）
    with pytest.raises(ValueError):
        telemetry.upload_finished(tenant_id="t1", status="failed")  # type: ignore[arg-type]


def test_fetch_and_delete_events_within_fixture() -> None:
    telemetry = AttachmentTelemetry()
    fetch_ok = telemetry.fetch_finished(tenant_id="t1", status="succeeded")
    delete_err = telemetry.delete_finished(
        tenant_id="t1", status="failed", error="boom", error_type="DeleteError"
    )
    assert set(fetch_ok) <= ALLOWED_EVENT_FIELDS
    assert set(delete_err) <= ALLOWED_EVENT_FIELDS
    assert fetch_ok["status"] == "succeeded"
    assert delete_err["error_type"] == "DeleteError"


def test_cleanup_event_within_fixture() -> None:
    telemetry = AttachmentTelemetry()
    event = telemetry.cleanup_finished(
        event_name="cleanup.finished",
        deleted=3,
        removed_orphans=0,
        marked_missing=1,
        tenants=("t1", "t2"),
    )
    assert set(event) <= ALLOWED_EVENT_FIELDS
    assert event["status"] == "completed"


def test_metric_labels_within_policy() -> None:
    """注册期 label 白名单校验：status 合法可注册；attachment_id 被拒。"""
    assert "status" in ALLOWED_METRIC_LABELS
    registry = MetricRegistry()
    metrics = register_attachment_metrics(registry)
    assert isinstance(metrics, AttachmentMetrics)
    # 负向契约：attachment_id 不在白名单 → 注册即抛
    with pytest.raises(MetricLabelPolicyError):
        validate_label_names(("attachment_id",))
    with pytest.raises(MetricLabelPolicyError):
        validate_label_names(("message_id",))


def test_metric_counters_increment() -> None:
    registry = MetricRegistry()
    metrics = register_attachment_metrics(registry)
    telemetry = AttachmentTelemetry(metrics)
    telemetry.upload_finished(tenant_id="t1", status="succeeded", size_bytes=1)
    telemetry.fetch_finished(tenant_id="t1", status="succeeded")
    telemetry.delete_finished(tenant_id="t1", status="failed", error="x", error_type="E")
    telemetry.cleanup_finished(
        event_name="cleanup.finished",
        deleted=0, removed_orphans=0, marked_missing=0, tenants=(),
    )
    assert metrics.uploads_total.get(labels={"status": "succeeded"}) == 1
    assert metrics.fetch_total.get(labels={"status": "succeeded"}) == 1
    assert metrics.delete_total.get(labels={"status": "failed"}) == 1
    assert metrics.cleanup_total.get(labels={"status": "completed"}) == 1