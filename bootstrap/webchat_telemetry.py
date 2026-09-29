"""turn/tool_call/delivery 生命周期事件与指标记录点（并入 C12 §8.1 / E10）。

pg-durable-sot-cutover task 6.1：消除 C12 §8.1 余留的三个无 owner 记录点
（`turn`/`tool_call`/`delivery` 表的生命周期事件）。模式与
`work_queue_telemetry.py`（C15 在 `background_work_items` 上的落地）完全一致。

契约单一来源：`tests/fixtures/observability_event_schema.json`（C12 ADR-7）。

**字段白名单**复用 `work_queue_telemetry.ALLOWED_EVENT_FIELDS`（同一 fixture 的
同一导出，本模块不扩 fixture、不建第二份白名单）——事件字典只从白名单取字段，
`payload_json`/handler 输入输出/message content/tool args/tool result
**在结构上无法**进入事件（白名单构造而非过滤敏感字段）。

**词汇映射**（fixture 的 `status` 枚举只有四个终态词）：

- turn 终态 `completed`/`failed` 直接落 `status`（控制面 turn 状态本就在枚举内）；
- tool_call 原始终态（succeeded/failed/cancelled/unknown/rejected）放 `result`
  （fixture 无枚举约束），`status` 仅在能无损映射时设置，否则省略——同 C15
  claim 事件「过程事件不带 status」的先例；
- delivery 原始状态（sent/failed/dead_letter）放 `result`；`status` 语义映射：
  sent → `completed`（投递任务成功收束）、failed/dead_letter → `failed`
  （dead_letter 另以 `retryable=False` 区分）。

自由文本（`last_error`）入库前过 `core/telemetry/redaction.redact_text`；
metric label 只用 `core/telemetry/label_policy.ALLOWED_METRIC_LABELS` 内的
有界维度，注册期 `validate_label_names()` fail-fast；记录点异常不阻断主流程。
"""

from __future__ import annotations

import logging
from typing import Any

from core.telemetry.label_policy import validate_label_names
from core.telemetry.metrics import Counter, MetricRegistry
from core.telemetry.redaction import redact_text
from bootstrap.work_queue_telemetry import ALLOWED_EVENT_FIELDS, FROZEN_FINISH_STATUSES

logger = logging.getLogger(__name__)

__all__ = [
    "WebchatLifecycleMetrics",
    "WebchatLifecycleTelemetry",
    "build_default_lifecycle_telemetry",
    "register_webchat_lifecycle_metrics",
]

# delivery 原始状态 → (fixture status 枚举值, retryable 语义)
_DELIVERY_STATUS_MAP: dict[str, tuple[str, bool | None]] = {
    "sent": ("completed", None),
    "failed": ("failed", True),
    "dead_letter": ("failed", False),
}

# tool_call 原始终态 → fixture status 枚举（可无损映射才设置 status）
_TOOL_STATUS_MAP: dict[str, str] = {
    "succeeded": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "interrupted",
}


def _emit(phase: str, fields: dict[str, Any]) -> dict[str, Any]:
    """从白名单构造并记录一条生命周期事件（语义同 work_queue_telemetry._emit）。

    非白名单字段丢弃并报错；丢弃而不是抛错，是为了不让遥测把主流程打挂。
    """
    unknown = sorted(set(fields) - ALLOWED_EVENT_FIELDS)
    if unknown:
        logger.error(
            "webchat lifecycle 事件含非白名单字段，已丢弃: phase=%s fields=%s",
            phase,
            unknown,
        )
    event = {
        key: value
        for key, value in fields.items()
        if key in ALLOWED_EVENT_FIELDS and value is not None
    }
    logger.info("webchat lifecycle: %s", phase, extra=event)
    return event


class WebchatLifecycleTelemetry:
    """turn/tool_call/delivery 记录点（每个方法返回实际事件字典供契约测试断言）。"""

    def __init__(self, metrics: "WebchatLifecycleMetrics | None" = None) -> None:
        self._m = metrics

    # ── turn ──

    def turn_finished(
        self,
        *,
        turn_id: str,
        tenant_id: str,
        status: str,
        channel: str | None = None,
        started_at: Any = None,
        finished_at: Any = None,
        error: str | None = None,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        """turn 终态（finisher 成功 T2 / 失败收束 / 启动对账共用）。"""
        if status not in FROZEN_FINISH_STATUSES:
            raise ValueError(
                f"turn 事件 status 超出冻结枚举: {status!r}"
                f"（允许: {sorted(FROZEN_FINISH_STATUSES)}）"
            )
        event = _emit(
            "turn.finish",
            {
                "turn_id": turn_id,
                "tenant_id": tenant_id,
                "channel": channel,
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "error_type": error_type,
                "last_error": redact_text(error) if error else None,
            },
        )
        if self._m is not None:
            self._m.turns_total.inc(
                labels={
                    "status": status,
                    "channel": channel or "unset",
                    "tenant_id": tenant_id,
                }
            )
        return event

    # ── tool_call ──

    def tool_call_finished(
        self,
        *,
        tool_call_id: str,
        turn_id: str | None,
        tenant_id: str,
        tool_name: str,
        result: str,
        duration_ms: int | None = None,
        error_code: str | None = None,
        effect_class: str | None = None,
    ) -> dict[str, Any]:
        """工具调用终态（C7 审计流的 E10 投影；原始终态落 result）。

        `status` 仅在原始终态可无损映射进 fixture 枚举时设置
        （succeeded→completed 等）；`unknown`/`rejected` 不强行映射。
        """
        status = _TOOL_STATUS_MAP.get(result)
        event = _emit(
            "tool_call.finish",
            {
                "tool_call_id": tool_call_id,
                "turn_id": turn_id,
                "tenant_id": tenant_id,
                "tool_name": tool_name,
                "result": result,
                "status": status,
                "error_type": error_code,
            },
        )
        if self._m is not None:
            labels: dict[str, str] = {
                "tool_name": tool_name,
                "result": result,
                "tenant_id": tenant_id,
            }
            if effect_class:
                labels["effect_class"] = effect_class
            self._m.tool_calls_total.inc(labels=labels)
            if duration_ms is not None:
                self._m.tool_call_duration_seconds.observe(
                    duration_ms / 1000.0, labels=labels
                )
        return event

    # ── delivery ──

    def delivery_finished(
        self,
        *,
        message_id: str,
        turn_id: str | None,
        tenant_id: str,
        channel: str,
        result: str,
        attempt: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """delivery attempt 终态（worker sent/failed/dead_letter 收束点）。

        `result` = 原始投递状态；`status` = fixture 枚举映射
        （sent→completed、failed/dead_letter→failed，dead_letter retryable=False）。
        """
        if result not in _DELIVERY_STATUS_MAP:
            raise ValueError(f"非法 delivery 终态: {result!r}")
        status, retryable = _DELIVERY_STATUS_MAP[result]
        event = _emit(
            "delivery.finish",
            {
                "message_id": message_id,
                "turn_id": turn_id,
                "tenant_id": tenant_id,
                "channel": channel,
                "result": result,
                "status": status,
                "retryable": retryable,
                "attempt": attempt,
                "last_error": redact_text(error) if error else None,
            },
        )
        if self._m is not None:
            self._m.delivery_total.inc(
                labels={
                    "result": result,
                    "status": status,
                    "channel": channel or "unset",
                    "tenant_id": tenant_id,
                }
            )
        return event


class WebchatLifecycleMetrics:
    """turn/tool_call/delivery 指标族句柄。"""

    turns_total: Counter
    tool_calls_total: Counter
    tool_call_duration_seconds: Any
    delivery_total: Counter


_TURN_LABELS = ("status", "channel", "tenant_id")
_TOOL_LABELS = ("tool_name", "result", "tenant_id", "effect_class")
_DELIVERY_LABELS = ("result", "status", "channel", "tenant_id")


def register_webchat_lifecycle_metrics(
    registry: MetricRegistry,
) -> WebchatLifecycleMetrics:
    """幂等注册三个记录点的指标族；label 先过 C12 白名单校验（越界即抛错）。"""
    for label_names in (_TURN_LABELS, _TOOL_LABELS, _DELIVERY_LABELS):
        validate_label_names(label_names)

    turns_total = registry.get("webchat_turns_total")
    if turns_total is None:
        turns_total = registry.counter(
            "webchat_turns_total",
            "WebChat turn 终态总数（按终态/通道/租户分）",
            label_names=_TURN_LABELS,
        )
    assert isinstance(turns_total, Counter), "webchat_turns_total 须注册为 counter"

    tool_calls_total = registry.get("webchat_tool_calls_total")
    if tool_calls_total is None:
        tool_calls_total = registry.counter(
            "webchat_tool_calls_total",
            "工具调用终态总数（按工具/原始终态/租户分）",
            label_names=_TOOL_LABELS,
        )
    assert isinstance(tool_calls_total, Counter), "webchat_tool_calls_total 须注册为 counter"

    tool_duration = registry.get("webchat_tool_call_duration_seconds")
    if tool_duration is None:
        tool_duration = registry.timer(
            "webchat_tool_call_duration_seconds",
            "工具调用耗时（秒）",
            label_names=_TOOL_LABELS,
        )

    delivery_total = registry.get("webchat_delivery_total")
    if delivery_total is None:
        delivery_total = registry.counter(
            "webchat_delivery_total",
            "投递 attempt 终态总数（按原始状态/枚举终态/通道/租户分）",
            label_names=_DELIVERY_LABELS,
        )
    assert isinstance(delivery_total, Counter), "webchat_delivery_total 须注册为 counter"

    metrics = WebchatLifecycleMetrics()
    metrics.turns_total = turns_total
    metrics.tool_calls_total = tool_calls_total
    metrics.tool_call_duration_seconds = tool_duration
    metrics.delivery_total = delivery_total
    return metrics


def build_default_lifecycle_telemetry() -> WebchatLifecycleTelemetry:
    """按进程内默认指标注册表装配记录点（C0 dashboard /metrics 同源）。"""
    from core.telemetry.metrics import get_default_registry

    return WebchatLifecycleTelemetry(
        metrics=register_webchat_lifecycle_metrics(get_default_registry())
    )
