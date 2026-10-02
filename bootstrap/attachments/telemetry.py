"""attachment 生命周期事件记录点（C12 §8.1 伴随落地，E10 投影）。

模式与 ``webchat_telemetry.py``（pg-durable-sot-cutover task 6.1）完全一致：

- **契约单一来源**：`tests/fixtures/observability_event_schema.json`（C12 ADR-7）；
  字段白名单复用 ``work_queue_telemetry.ALLOWED_EVENT_FIELDS``（同一导出，本模块
  不扩 fixture、不建第二份白名单）——事件字典只从白名单取字段，attachment 内容
  （filename/路径/完整正文）在**结构上无法**进入事件；
- **redaction 兜底**：自由文本字段（error/last_error 等）过
  ``core/telemetry/redaction.redact_text``；
- **label 白名单**：metric label 只用 ``label_policy.ALLOWED_METRIC_LABELS`` 内
  的有界维度（attachment_id 不在其内 → 注册被拒即为负向契约）；
- **记录点异常不阻断主流程**：非白名单字段丢弃并报错，emit 抛错由调用方
  try/except 守护。

事件：``upload.finished`` / ``fetch.finished`` / ``delete.finished`` /
``cleanup.finished``。metrics 钩子最小化（size_bytes/status 均可落已有 metric
族；不新增 label；量化由聚合 API 承担）。
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
    "AttachmentTelemetry",
    "register_attachment_metrics",
    "build_default_attachment_telemetry",
]

# attachment 事件可用的 status 枚举（fixture 冻结四终态词）
_OK_STATUSES = {"succeeded", "completed"}


def _emit(phase: str, fields: dict[str, Any]) -> dict[str, Any]:
    """从白名单构造并记录一条生命周期事件（语义同 webchat_telemetry._emit）。

    非白名单字段丢弃并报错；丢弃而不是抛错，是为了不让遥测把主流程打挂。
    """
    unknown = sorted(set(fields) - ALLOWED_EVENT_FIELDS)
    if unknown:
        logger.error(
            "attachment 事件含非白名单字段，已丢弃: phase=%s fields=%s",
            phase,
            unknown,
        )
    event = {
        key: value
        for key, value in fields.items()
        if key in ALLOWED_EVENT_FIELDS and value is not None
    }
    logger.info("attachment lifecycle: %s", phase, extra=event)
    return event


class AttachmentTelemetry:
    """upload/fetch/delete/cleanup 记录点（每个方法返回实际事件字典供契约测试断言）。"""

    def __init__(self, metrics: "AttachmentMetrics | None" = None) -> None:
        self._m = metrics

    # ── upload ──

    def upload_finished(
        self,
        *,
        tenant_id: str,
        status: str,
        size_bytes: int | None = None,
        error: str | None = None,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        if status not in _OK_STATUSES and error is None:
            raise ValueError(f"upload 事件 status 非成功且无 error: {status!r}")
        # 事件字段严格 ⊆ ALLOWED_EVENT_FIELDS：size_bytes 不在白名单（fixture 无
        # 该字段），量化走 metric；如需事件内可用的容量维度，后续在 fixture 增加
        # 字段后再放宽（C12 单一来源先行）。
        fields: dict[str, Any] = {
            "tenant_id": tenant_id,
            "status": status,
        }
        if error is not None:
            fields["last_error"] = redact_text(error)
            fields["error_type"] = error_type
        event = _emit("upload.finished", fields)
        m = self._m
        if m is not None and status in _OK_STATUSES:
            m.uploads_total.inc(labels={"status": status})
        return event

    # ── fetch ──

    def fetch_finished(
        self,
        *,
        tenant_id: str,
        status: str,
        error: str | None = None,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "tenant_id": tenant_id,
            "status": status,
        }
        if error is not None:
            fields["last_error"] = redact_text(error)
            fields["error_type"] = error_type
        event = _emit("fetch.finished", fields)
        m = self._m
        if m is not None and status in _OK_STATUSES:
            m.fetch_total.inc(labels={"status": status})
        return event

    # ── delete ──

    def delete_finished(
        self,
        *,
        tenant_id: str,
        status: str,
        error: str | None = None,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "tenant_id": tenant_id,
            "status": status,
        }
        if error is not None:
            fields["last_error"] = redact_text(error)
            fields["error_type"] = error_type
        event = _emit("delete.finished", fields)
        m = self._m
        if m is not None:
            m.delete_total.inc(labels={"status": status})
        return event

    # ── cleanup ──

    def cleanup_finished(
        self,
        *,
        event_name: str,
        deleted: int,
        removed_orphans: int,
        marked_missing: int,
        tenants: tuple[str, ...],
    ) -> dict[str, Any]:
        # 事件字段严格 ⊆ ALLOWED_EVENT_FIELDS：量化走已注册 counter，事件只留
        # status + （白名单外的 tenants 以数量折叠到日志，不进事件字典）。
        fields: dict[str, Any] = {
            "status": "completed",
        }
        event = _emit("cleanup.finished", fields)
        m = self._m
        if m is not None:
            m.cleanup_total.inc(labels={"status": "completed"})
        if tenants:
            logger.info(
                "attachment cleanup tenants=%d (%s)",
                len(tenants),
                ",".join(tenants[:8]),
            )
        return event


# ── metrics ─────────────────────────────────────────────────────────


class AttachmentMetrics:
    """attachment 事件计数（注册期 label 白名单校验；量化后续补）。"""

    def __init__(
        self,
        uploads_total: Counter,
        fetch_total: Counter,
        delete_total: Counter,
        cleanup_total: Counter,
    ) -> None:
        self.uploads_total = uploads_total
        self.fetch_total = fetch_total
        self.delete_total = delete_total
        self.cleanup_total = cleanup_total


def register_attachment_metrics(registry: MetricRegistry) -> AttachmentMetrics:
    """注册 attachment 指标族（label 仅用白名单内有界维度）。"""
    labels = ("status",)
    validate_label_names(labels)
    return AttachmentMetrics(
        uploads_total=registry.counter("attachment_uploads_total", "", label_names=labels),
        fetch_total=registry.counter("attachment_fetch_total", "", label_names=labels),
        delete_total=registry.counter("attachment_delete_total", "", label_names=labels),
        cleanup_total=registry.counter("attachment_cleanup_total", "", label_names=labels),
    )


def build_default_attachment_telemetry() -> AttachmentTelemetry:
    return AttachmentTelemetry()


__all__ = [
    "AttachmentTelemetry",
    "AttachmentMetrics",
    "register_attachment_metrics",
    "build_default_attachment_telemetry",
]