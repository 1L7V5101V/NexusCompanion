"""C7 task 7.1：tool_audit_events 审计流（design ADR-6）。

PG（control plane）：一行 = 一次工具调用的收束记录（终态时追加 INSERT，
含执行前拒绝 ``rejected``），``tool_call_id`` 为软引用（无 FK）——审计流 SHALL
NOT 因终态流的生命周期而丢行。SQLite 单机模式不建表，以**同字段结构化 JSON
行**兜底（``workspace/logs/tool_audit.ndjson``，design ADR-6 附录）。

脱敏规则（§5.8.6 统一 schema）：API key / OAuth token / 口令等敏感参数
SHALL NOT 原样进入审计——secret 键只存单向 hash；其余长值截断。
``arguments_hash`` = 原始参数规范化 JSON 的 sha256（同一调用可对账，
不可还原原文）。

写入 fail-open：审计失败不阻断工具调用本身（记录 warning 后吞掉）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# 终态枚举 = C2 四态 + rejected（执行前拒绝，无终态流行）。
AUDIT_STATUSES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "cancelled",
    "unknown",
    "rejected",
)

# 敏感参数键：命中即只存 hash（不存原文）。匹配为子串（大小写不敏感）。
_SECRET_KEY_FRAGMENTS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "passwd",
        "authorization",
        "cookie",
        "bearer",
        "auth",
        "credential",
    }
)

_MAX_STR_LEN = 500       # 内容截断阈值
_MAX_DEPTH = 3           # 递归深度上限
_MAX_ELEMENTS = 40       # 单层元素上限


def is_secret_key(key: str) -> bool:
    k = key.lower()
    return any(frag in k for frag in _SECRET_KEY_FRAGMENTS)


def redact_arguments(arguments: dict[str, Any]) -> tuple[str, str]:
    """返回 ``(redacted_json, arguments_hash)``。

    - secret 键：值替换为 ``{"__hash__": <sha256 原文>}``（不存明文）；
    - 其余字符串：截断到 ``_MAX_STR_LEN``；
    - 嵌套结构：深度/元素数上限，超出以占位符标注；
    - ``arguments_hash``：原始参数规范化 JSON 的 sha256（可对账、不可还原原文）。
    """
    def _redact(key: str, value: Any, depth: int) -> Any:
        if is_secret_key(key) and isinstance(value, str) and value:
            return {"__hash__": hashlib.sha256(value.encode("utf-8")).hexdigest()}
        if isinstance(value, str):
            if len(value) > _MAX_STR_LEN:
                return value[:_MAX_STR_LEN] + f"…<truncated {len(value) - _MAX_STR_LEN}>"
            return value
        if depth <= 0:
            return "<redacted:depth>"
        if isinstance(value, dict):
            items = list(value.items())[:_MAX_ELEMENTS]
            return {k: _redact(k, v, depth - 1) for k, v in items}
        if isinstance(value, (list, tuple)):
            return [_redact("", v, depth - 1) for v in value[:_MAX_ELEMENTS]]
        return value

    redacted = {k: _redact(k, v, _MAX_DEPTH) for k, v in arguments.items()}
    try:
        redacted_json = json.dumps(
            redacted, ensure_ascii=False, sort_keys=True, default=str
        )
    except Exception:  # noqa: BLE001 —— 脱敏输出失败不回退原参（宁缺毋泄）
        redacted_json = "<redacted:unserializable>"
    try:
        canonical = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, default=str
        )
    except Exception:  # noqa: BLE001
        canonical = repr(arguments)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return redacted_json, digest


@dataclass(frozen=True)
class ToolAuditEvent:
    """一行审计记录（字段对齐 alembic b3f7a1c5d9e2 / §5.8.6）。"""

    tenant_id: str
    tool_name: str
    effect_class: str
    status: str
    account_id: str = ""
    request_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    tool_call_id: str = ""
    tool_binding_id: str = ""
    arguments_redacted: str = ""
    arguments_hash: str = ""
    duration_ms: int | None = None
    error_code: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.status not in AUDIT_STATUSES:
            raise ValueError(f"非法审计终态: {self.status!r}")

    def to_row(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id or None,
            "account_id": self.account_id or None,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id or None,
            "turn_id": self.turn_id or None,
            "tool_call_id": self.tool_call_id or None,
            "tool_binding_id": self.tool_binding_id or None,
            "tool_name": self.tool_name,
            "effect_class": self.effect_class,
            "status": self.status,
            "arguments_redacted": self.arguments_redacted or None,
            "arguments_hash": self.arguments_hash or None,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
        }

    def to_json(self) -> dict[str, Any]:
        row = self.to_row()
        row["created_at"] = self.created_at.isoformat()
        return row


class ToolAuditSink(Protocol):
    """审计写入接缝（PG INSERT / 单机结构化日志 / 测试 Null 均实现）。"""

    async def write(self, event: ToolAuditEvent) -> None: ...


class LogAuditSink:
    """SQLite/单机兜底：同字段结构化 JSON 行追加到 ``<logs>/tool_audit.ndjson``。

    与 PG 行同字段、同脱敏规则（design ADR-6 附录）；写入失败仅告警不重试。
    """

    def __init__(self, log_dir: Path | str) -> None:
        self._path = Path(log_dir) / "tool_audit.ndjson"

    async def write(self, event: ToolAuditEvent) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")
        except OSError as exc:
            # 审计兜底本身失败：只告警，不阻断工具调用。
            logger.warning("tool_audit 日志兜底写入失败: %s", exc)


class NullAuditSink:
    """测试/未接线默认：丢弃（registry 不默认接审计，行为不变）。"""

    async def write(self, event: ToolAuditEvent) -> None:
        return None