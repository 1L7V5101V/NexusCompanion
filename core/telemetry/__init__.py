"""进程内遥测：指标注册、双格式导出、turn_id 追踪表面。

C12 叠加观测隐私契约：redaction / label 白名单 / retention（不改既有行为）。
"""

from core.telemetry.audit import AdminAccessAuditEvent
from core.telemetry.builtin import BuiltinMetrics, register_builtin_metrics
from core.telemetry.label_policy import (
    ALLOWED_METRIC_LABELS,
    MetricLabelPolicyError,
    PolicyCheckedMetricRegistry,
)
from core.telemetry.metrics import (
    DEFAULT_HISTOGRAM_BUCKETS,
    Counter,
    Histogram,
    Metric,
    MetricRegistry,
    Timer,
    get_default_registry,
)
from core.telemetry.redaction import (
    ContentCaptureGate,
    RedactionStats,
    redact_text,
    redact_value,
)
from core.telemetry.retention import (
    RetentionCategory,
    RetentionPolicy,
    sweep_roots,
)

__all__ = [
    "ALLOWED_METRIC_LABELS",
    "DEFAULT_HISTOGRAM_BUCKETS",
    "AdminAccessAuditEvent",
    "BuiltinMetrics",
    "ContentCaptureGate",
    "Counter",
    "Histogram",
    "Metric",
    "MetricLabelPolicyError",
    "MetricRegistry",
    "PolicyCheckedMetricRegistry",
    "RedactionStats",
    "RetentionCategory",
    "RetentionPolicy",
    "Timer",
    "get_default_registry",
    "redact_text",
    "redact_value",
    "register_builtin_metrics",
    "sweep_roots",
]
