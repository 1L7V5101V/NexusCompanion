"""admin 访问审计事件契约（C12，ADR-7）。

admin 内容查看、跨 tenant 下钻、导出与内容采集开关都必须产生一条审计事件；
事件只含 metadata 与脱敏后的摘要，不含被查看内容原文（§5.9.17）。
schema 单一来源：tests/fixtures/observability_event_schema.json 的
admin_access_audit_event 段；retention 归 audit 类别（180 天）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AUDIT_ACTION_DRILL_DOWN",
    "AUDIT_ACTION_ENABLE_CONTENT_CAPTURE",
    "AUDIT_ACTION_EXPORT",
    "AUDIT_ACTION_VIEW_CONTENT",
    "AdminAccessAuditEvent",
]

AUDIT_ACTION_VIEW_CONTENT = "view_content"
AUDIT_ACTION_DRILL_DOWN = "drill_down"
AUDIT_ACTION_EXPORT = "export"
AUDIT_ACTION_ENABLE_CONTENT_CAPTURE = "enable_content_capture"


@dataclass(frozen=True)
class AdminAccessAuditEvent:
    """一次 admin 敏感访问的审计记录。

    `reason`/`target_id` 在序列化前过 redaction，保证审计事件本身不携带
    敏感原文；`at` 为 unix 秒。
    """

    principal: str
    action: str
    target_kind: str
    target_id: str = ""
    tenant_id: str | None = None
    reason: str = ""
    at: float = field(default_factory=time.time)

    def to_dict(self, *, redact: Any = None) -> dict[str, Any]:
        """序列化为 metadata dict；`redact` 注入 core.telemetry.redaction.redact_text。

        redact 缺省时原样输出（仅供无脱敏依赖的内部路径），观测落点必须传 redact。
        """
        data: dict[str, Any] = {
            "at": self.at,
            "principal": self.principal,
            "action": self.action,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "tenant_id": self.tenant_id,
            "reason": self.reason,
        }
        if redact is not None:
            data["target_id"] = redact(self.target_id)
            data["reason"] = redact(self.reason)
        return data
