"""C15 work item 生命周期事件与指标记录点（并入 C12 §8.1 / E10）。

契约单一来源：`tests/fixtures/observability_event_schema.json`（C12 ADR-7）。本模块是该
fixture 在 `background_work_items` 这个 canonical store 上的落地实现。

**内容边界（C12 ADR-1/ADR-2、§5.9.17）——按「白名单构造」而不是「过滤敏感字段」**

- 事件字典**只从 fixture 声明的字段里取**（`_ALLOWED_EVENT_FIELDS`）。因此
  `payload_json`、handler 的输入/输出、message content、tool args/result、raw provider
  payload **在结构上无法**进入事件——不是靠记得过滤，而是靠不在白名单里。
- 出现非白名单字段时**丢弃该字段并报错**（既不泄露，也不让遥测把 worker 打挂）。
- 自由文本（失败原因）入库前过 `core/telemetry/redaction.redact_text`。
- metric label 只用 `core/telemetry/label_policy.ALLOWED_METRIC_LABELS` 内的有界维度，
  注册期先 `validate_label_names()` fail-fast。注意 `tenant_id` **在白名单内**（C12 明确
  允许：Pilot 规模有界、§7.1 要求按其聚合）；被禁的是 `work_id`/`session_key`/`message_id`
  等高基数身份字段与全部内容字段。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.telemetry.label_policy import validate_label_names
from core.telemetry.metrics import Counter, MetricRegistry, Timer
from core.telemetry.redaction import redact_text

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_EVENT_FIELDS",
    "WorkQueueMetrics",
    "WorkQueueTelemetry",
    "register_work_queue_metrics",
]

# ── 事件字段白名单 = fixture 的 7 个 field_groups ∪ derived_metrics ──
# 不含 forbidden_content_fields（raw_provider_payload/full_prompt/message_content/
# tool_args/tool_result/attachment_content）——它们**不在**白名单里，所以进不来。
ALLOWED_EVENT_FIELDS: frozenset[str] = frozenset(
    {
        # identity
        "work_id", "turn_id", "tool_call_id", "message_id", "tenant_id", "session_key",
        # classification
        "work_kind", "flow", "stage", "trigger", "tool_name", "channel",
        # version
        "snapshot_id", "code_version", "model",
        # lifecycle_time
        "enqueued_at", "started_at", "finished_at",
        "cancel_requested_at", "timeout_at",
        # lifecycle_result
        "status", "result", "error_type", "retryable", "attempt", "last_error",
        # side_effect_recovery
        "side_effect", "outcome_status", "compensation_status", "recovery_action",
        # resource
        "input_tokens", "output_tokens", "cache_hit_tokens", "provider",
        "db_latency_ms", "external_latency_ms",
        # derived_metrics
        "queue_wait_ms", "execution_ms", "end_to_end_ms",
        "tool_cancel_to_terminal_ms", "tool_timeout_to_terminal_ms",
        "ban_to_admission_reject_ms", "ban_to_terminal_ms", "restart_to_recovered_ms",
    }
)

_ITEMS_TOTAL_LABELS = ("work_kind", "flow", "status")
_ITEM_WAIT_LABELS = ("work_kind", "flow")
_ITEM_EXECUTION_LABELS = ("work_kind", "flow")
_RECOVERY_LABELS = ("work_kind", "recovery_action")

_FLOW_UNSET = "unset"
"""`flow` 为 NULL 时的 label 值（intake 已校验 flow 合法，NULL 只可能来自历史行）。"""


@dataclass(frozen=True)
class WorkQueueMetrics:
    """work item 指标族句柄。"""

    work_items_total: Counter
    work_item_queue_wait_seconds: Timer
    work_item_execution_seconds: Timer
    work_queue_recovery_total: Counter


def register_work_queue_metrics(registry: MetricRegistry) -> WorkQueueMetrics:
    """幂等注册 work item 指标族；label 先过 C12 白名单校验（越界即抛错）。"""
    for label_names in (
        _ITEMS_TOTAL_LABELS,
        _ITEM_WAIT_LABELS,
        _ITEM_EXECUTION_LABELS,
        _RECOVERY_LABELS,
    ):
        validate_label_names(label_names)

    work_items_total = registry.get("work_items_total")
    if work_items_total is None:
        work_items_total = registry.counter(
            "work_items_total",
            "后台工作项收束总数（按任务类型/链路/终态分）",
            label_names=_ITEMS_TOTAL_LABELS,
        )
    assert isinstance(work_items_total, Counter), "work_items_total 必须注册为 counter"

    work_item_queue_wait_seconds = registry.get("work_item_queue_wait_seconds")
    if work_item_queue_wait_seconds is None:
        work_item_queue_wait_seconds = registry.timer(
            "work_item_queue_wait_seconds",
            "工作项排队等待耗时（秒）= started_at - enqueued_at",
            label_names=_ITEM_WAIT_LABELS,
        )
    assert isinstance(work_item_queue_wait_seconds, Timer), (
        "work_item_queue_wait_seconds 必须注册为 timer"
    )

    work_item_execution_seconds = registry.get("work_item_execution_seconds")
    if work_item_execution_seconds is None:
        work_item_execution_seconds = registry.timer(
            "work_item_execution_seconds",
            "工作项执行耗时（秒）= finished_at - started_at",
            label_names=_ITEM_EXECUTION_LABELS,
        )
    assert isinstance(work_item_execution_seconds, Timer), (
        "work_item_execution_seconds 必须注册为 timer"
    )

    work_queue_recovery_total = registry.get("work_queue_recovery_total")
    if work_queue_recovery_total is None:
        work_queue_recovery_total = registry.counter(
            "work_queue_recovery_total",
            "崩溃遗留工作项被清扫复位的总数（按恢复动作分）",
            label_names=_RECOVERY_LABELS,
        )
    assert isinstance(work_queue_recovery_total, Counter), (
        "work_queue_recovery_total 必须注册为 counter"
    )

    return WorkQueueMetrics(
        work_items_total=work_items_total,
        work_item_queue_wait_seconds=work_item_queue_wait_seconds,
        work_item_execution_seconds=work_item_execution_seconds,
        work_queue_recovery_total=work_queue_recovery_total,
    )


class WorkQueueTelemetry:
    """work item 三个记录点：`claim` / `finish` / `recovery`。

    每个方法返回**实际记录的事件字典**（供契约测试断言），并写结构化日志；`metrics`
    为 `None` 时只记日志（测试与未装配指标的路径）。
    """

    def __init__(self, metrics: WorkQueueMetrics | None = None) -> None:
        self._m = metrics

    # ── 记录点 ──

    def claim(
        self,
        *,
        work_id: str,
        tenant_id: str,
        work_kind: str,
        flow: str | None,
        enqueued_at: Any,
        started_at: datetime,
    ) -> dict[str, Any]:
        """认领：`queue_wait_ms` = started_at - enqueued_at。"""
        wait_ms = _delta_ms(started_at, enqueued_at)
        event = _emit(
            "work_item.claim",
            {
                "work_id": work_id,
                "tenant_id": tenant_id,
                "work_kind": work_kind,
                "flow": flow,
                "enqueued_at": _iso(enqueued_at),
                "started_at": started_at.isoformat(),
                "status": "in_progress",
                "queue_wait_ms": wait_ms,
            },
        )
        if self._m is not None and wait_ms is not None:
            labels = _item_labels(work_kind, flow)
            self._m.work_item_queue_wait_seconds.observe(wait_ms / 1000.0, labels=labels)
        return event

    def finish(
        self,
        *,
        work_id: str,
        tenant_id: str,
        work_kind: str,
        flow: str | None,
        status: str,
        started_at: datetime,
        finished_at: datetime,
        attempt: int | None = None,
        error_type: str | None = None,
        retryable: bool | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """收束：`execution_ms` = finished_at - started_at；自由文本过 redaction。"""
        exec_ms = _delta_ms(finished_at, started_at)
        event = _emit(
            "work_item.finish",
            {
                "work_id": work_id,
                "tenant_id": tenant_id,
                "work_kind": work_kind,
                "flow": flow,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "status": status,
                "attempt": attempt,
                "error_type": error_type,
                "retryable": retryable,
                # 自由文本：入库前强制脱敏（C12 的 redaction_rule）
                "last_error": redact_text(error) if error else None,
                "execution_ms": exec_ms,
            },
        )
        if self._m is not None:
            labels = _item_labels(work_kind, flow)
            self._m.work_items_total.inc(
                labels={**labels, "status": status}
            )
            if exec_ms is not None:
                self._m.work_item_execution_seconds.observe(exec_ms / 1000.0, labels=labels)
        return event

    def recovery(
        self,
        *,
        work_id: str,
        tenant_id: str,
        work_kind: str,
        flow: str | None,
        recovery_action: str,
        restart_started_at: datetime | None,
        recovered_at: Any,
    ) -> dict[str, Any]:
        """崩溃恢复：`restart_to_recovered_ms` = recovered_at - restart_started_at。"""
        recovered_dt = _parse_iso(recovered_at)
        lag_ms = _delta_ms(recovered_dt, restart_started_at)
        event = _emit(
            "work_item.recovery",
            {
                "work_id": work_id,
                "tenant_id": tenant_id,
                "work_kind": work_kind,
                "flow": flow,
                "status": "queued",
                "recovery_action": recovery_action,
                "finished_at": _iso(recovered_at),
                "restart_to_recovered_ms": lag_ms,
            },
        )
        if self._m is not None:
            self._m.work_queue_recovery_total.inc(
                labels={"work_kind": work_kind, "recovery_action": recovery_action}
            )
        return event


def _emit(phase: str, fields: dict[str, Any]) -> dict[str, Any]:
    """从白名单构造并记录一条生命周期事件。

    非白名单字段一律**丢弃**并报错——事件内容由 `ALLOWED_EVENT_FIELDS` 决定，所以
    payload/内容字段结构上无法进入；丢弃而不是抛错，是为了不让遥测把 worker 打挂。
    """
    unknown = sorted(set(fields) - ALLOWED_EVENT_FIELDS)
    if unknown:
        logger.error("work item 事件含非白名单字段，已丢弃: phase=%s fields=%s", phase, unknown)
    event = {
        key: value
        for key, value in fields.items()
        if key in ALLOWED_EVENT_FIELDS and value is not None
    }
    logger.info("work item lifecycle: %s", phase, extra=event)
    return event


def _item_labels(work_kind: str, flow: str | None) -> dict[str, str]:
    return {"work_kind": work_kind, "flow": flow or _FLOW_UNSET}


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _delta_ms(later: datetime | None, earlier: Any) -> float | None:
    start = _parse_iso(earlier)
    if later is None or start is None:
        return None
    return round((later - start).total_seconds() * 1000, 1)


def _iso(value: Any) -> str | None:
    parsed = _parse_iso(value)
    return parsed.isoformat() if parsed is not None else (
        value if isinstance(value, str) else None
    )
