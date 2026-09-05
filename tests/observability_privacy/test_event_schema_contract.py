"""C12 事件 schema fixture 契约测试（ADR-7，task-12 验收第 1/2 条的 schema 面）。

fixture `tests/fixtures/observability_event_schema.json` 是 §7.1 的单一来源转录；
本测试冻结「fixture ↔ §7.1」与「fixture ↔ label 白名单」两个方向的一致性。
"""

from __future__ import annotations

import json
from pathlib import Path

from core.telemetry.audit import (
    AUDIT_ACTION_DRILL_DOWN,
    AUDIT_ACTION_ENABLE_CONTENT_CAPTURE,
    AUDIT_ACTION_EXPORT,
    AUDIT_ACTION_VIEW_CONTENT,
)
from core.telemetry.label_policy import ALLOWED_METRIC_LABELS

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "observability_event_schema.json"
)

# §7.1 冻结字段（roadmap 逐字转录；fixture 缺项/漂移在此失败）。
FROZEN_FIELD_GROUPS: dict[str, set[str]] = {
    "identity": {
        "work_id",
        "turn_id",
        "tool_call_id",
        "message_id",
        "tenant_id",
        "session_key",
    },
    "classification": {"work_kind", "flow", "stage", "trigger", "tool_name", "channel"},
    "version": {"snapshot_id", "code_version", "model"},
    "lifecycle_time": {
        "enqueued_at",
        "started_at",
        "finished_at",
        "cancel_requested_at",
        "timeout_at",
    },
    "lifecycle_result": {
        "status",
        "result",
        "error_type",
        "retryable",
        "attempt",
        "last_error",
    },
    "side_effect_recovery": {
        "side_effect",
        "outcome_status",
        "compensation_status",
        "recovery_action",
    },
    "resource": {
        "input_tokens",
        "output_tokens",
        "cache_hit_tokens",
        "provider",
    },
}
FROZEN_DERIVED_METRICS = {
    "queue_wait_ms",
    "execution_ms",
    "end_to_end_ms",
    "tool_cancel_to_terminal_ms",
    "tool_timeout_to_terminal_ms",
    "ban_to_admission_reject_ms",
    "ban_to_terminal_ms",
    "restart_to_recovered_ms",
}
FROZEN_AGGREGATION_DIMENSIONS = {
    "tenant_id",
    "work_kind",
    "flow",
    "stage",
    "channel",
    "tool_name",
    "model",
    "time_window",
}
FROZEN_FORBIDDEN_CONTENT_FIELDS = {
    "raw_provider_payload",
    "full_prompt",
    "message_content",
    "tool_args",
    "tool_result",
    "attachment_content",
}
FROZEN_AUDIT_ACTIONS = {
    AUDIT_ACTION_VIEW_CONTENT,
    AUDIT_ACTION_DRILL_DOWN,
    AUDIT_ACTION_EXPORT,
    AUDIT_ACTION_ENABLE_CONTENT_CAPTURE,
}


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_field_groups_match_s71():
    data = _fixture()
    lifecycle = data["lifecycle_event"]
    assert isinstance(lifecycle, dict)
    groups = lifecycle["field_groups"]
    assert isinstance(groups, dict)
    assert set(groups.keys()) == set(FROZEN_FIELD_GROUPS.keys())
    for group, frozen in FROZEN_FIELD_GROUPS.items():
        declared = groups[group]
        assert isinstance(declared, list)
        assert frozen <= set(declared), f"{group} 缺少 §7.1 冻结字段"


def test_derived_metrics_match_s71():
    data = _fixture()
    lifecycle = data["lifecycle_event"]
    assert isinstance(lifecycle, dict)
    derived = lifecycle["derived_metrics"]
    assert isinstance(derived, dict)
    assert FROZEN_DERIVED_METRICS <= set(derived.keys())


def test_aggregation_dimensions_match_s71():
    data = _fixture()
    lifecycle = data["lifecycle_event"]
    assert isinstance(lifecycle, dict)
    dims = lifecycle["aggregation_dimensions"]
    assert isinstance(dims, list)
    assert FROZEN_AGGREGATION_DIMENSIONS <= set(dims)


def test_forbidden_content_fields_frozen():
    data = _fixture()
    lifecycle = data["lifecycle_event"]
    assert isinstance(lifecycle, dict)
    forbidden = lifecycle["forbidden_content_fields"]
    assert isinstance(forbidden, list)
    assert set(forbidden) == FROZEN_FORBIDDEN_CONTENT_FIELDS


def test_identity_fields_not_in_metric_label_allowlist():
    """事件身份字段与 metrics label 白名单交叉一致（§5.9.17）。

    §7.1 identity 组中的 tenant_id 是有界聚合维度、明确允许进 label
    （design ADR-4）；其余身份字段一律禁止。
    """
    data = _fixture()
    lifecycle = data["lifecycle_event"]
    assert isinstance(lifecycle, dict)
    groups = lifecycle["field_groups"]
    assert isinstance(groups, dict)
    identity = groups["identity"]
    assert isinstance(identity, list)
    forbidden_identity = set(identity) - {"tenant_id"}
    assert forbidden_identity & set(ALLOWED_METRIC_LABELS) == set()
    assert "tenant_id" in set(ALLOWED_METRIC_LABELS)


def test_classification_dimensions_are_valid_metric_labels():
    """§7.1 聚合维度中进入 metrics 的分类字段必须在白名单内。"""
    assert {
        "work_kind",
        "flow",
        "stage",
        "channel",
        "tool_name",
        "model",
        "tenant_id",
    } <= set(ALLOWED_METRIC_LABELS)


def test_audit_event_schema_matches_module():
    data = _fixture()
    audit = data["admin_access_audit_event"]
    assert isinstance(audit, dict)
    actions = audit["actions"]
    assert isinstance(actions, list)
    assert set(actions) == FROZEN_AUDIT_ACTIONS
    assert audit["retention_category"] == "audit"
    required = audit["required_fields"]
    assert isinstance(required, list)
    assert {"at", "principal", "action", "target_kind", "target_id", "reason"} <= set(
        required
    )
