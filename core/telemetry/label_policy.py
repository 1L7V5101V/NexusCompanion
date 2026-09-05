"""metrics label 白名单契约（C12，ADR-4）。

§5.9.17：metrics label 不允许包含 account/message/tool-call 高基数字段、
原始 tool args 或内容——这些只能进入有访问控制的 trace/audit storage。
本模块维护显式 allowlist，`PolicyCheckedMetricRegistry` 在注册期 fail-fast，
白名单之外的 label 名（含高基数身份字段与内容字段）注册即失败。

tenant_id 在 Pilot 10–30 受邀账号规模内基数有界、且 §7.1 要求按其聚合，
故在白名单内；规模越过 P4 闸门时须复评。fixture
tests/fixtures/metric_label_policy.json 与本表交叉校验（契约测试强制一致）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from core.telemetry.metrics import (
    DEFAULT_HISTOGRAM_BUCKETS,
    Counter,
    Histogram,
    Metric,
    MetricRegistry,
    Timer,
)

__all__ = [
    "ALLOWED_METRIC_LABELS",
    "FORBIDDEN_IDENTITY_LABELS",
    "FORBIDDEN_CONTENT_LABELS",
    "MetricLabelPolicyError",
    "PolicyCheckedMetricRegistry",
    "validate_label_names",
]

# 白名单：有界枚举维度（work_kind/flow/stage/trigger/channel/tool_name/model/
# provider/backend/snapshot_id/attempt）+ 有界结果枚举（status/result/error_type/
# retryable/side_effect/outcome_status/compensation_status/recovery_action）+
# Pilot 规模内有界的 tenant_id + C0 既有 channel/backend。
ALLOWED_METRIC_LABELS: frozenset[str] = frozenset(
    {
        "work_kind",
        "flow",
        "stage",
        "trigger",
        "channel",
        "tool_name",
        "model",
        "provider",
        "backend",
        "snapshot_id",
        "attempt",
        "status",
        "result",
        "error_type",
        "retryable",
        "side_effect",
        "outcome_status",
        "compensation_status",
        "recovery_action",
        "tenant_id",
    }
)

# 禁止集：仅作文档与测试断言用途；执行语义 = 不在 allowlist 即拒绝。
FORBIDDEN_IDENTITY_LABELS: frozenset[str] = frozenset(
    {
        "account_id",
        "message_id",
        "tool_call_id",
        "turn_id",
        "work_id",
        "session_key",
    }
)
FORBIDDEN_CONTENT_LABELS: frozenset[str] = frozenset(
    {
        "tool_args",
        "tool_result",
        "prompt",
        "message_content",
        "attachment_content",
        "raw_payload",
    }
)


class MetricLabelPolicyError(ValueError):
    """使用白名单之外的 label 名注册指标。"""


MetricT = TypeVar("MetricT", bound=Metric)


def validate_label_names(label_names: Iterable[str]) -> None:
    """校验 label 名集合全部在白名单内，任一越界即抛错。"""
    for name in label_names:
        if name not in ALLOWED_METRIC_LABELS:
            raise MetricLabelPolicyError(
                f"metrics label {name!r} 不在白名单内（§5.9.17：高基数字段/"
                "原始 args/内容不得进入 metrics label）"
            )


class PolicyCheckedMetricRegistry(MetricRegistry):
    """注册期执行 label 白名单的 MetricRegistry 包装。

    counter/histogram/timer 工厂先过 `validate_label_names` 再落注册表；
    register 直入路径同样校验（防绕过工厂）。
    """

    def counter(
        self,
        name: str,
        help_text: str = "",
        *,
        label_names: Iterable[str] = (),
    ) -> Counter:
        validate_label_names(label_names)
        return super().counter(name, help_text, label_names=label_names)

    def histogram(
        self,
        name: str,
        help_text: str = "",
        *,
        label_names: Iterable[str] = (),
        buckets: Iterable[float] = DEFAULT_HISTOGRAM_BUCKETS,
    ) -> Histogram:
        validate_label_names(label_names)
        return super().histogram(
            name, help_text, label_names=label_names, buckets=buckets
        )

    def timer(
        self,
        name: str,
        help_text: str = "",
        *,
        label_names: Iterable[str] = (),
        buckets: Iterable[float] = DEFAULT_HISTOGRAM_BUCKETS,
    ) -> Timer:
        validate_label_names(label_names)
        return super().timer(name, help_text, label_names=label_names, buckets=buckets)

    def register(self, metric: MetricT) -> MetricT:
        validate_label_names(metric.label_names)
        return super().register(metric)
