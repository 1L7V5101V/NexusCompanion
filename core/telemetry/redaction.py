"""观测数据脱敏契约（C12，ADR-1/2/3）。

两层防线：

1. `redact_text` / `redact_value` — secret（键名启发式 + Bearer/sk- 值模式）、
   credential URL、本地路径、PII 四类确定性正则替换，占位符保留类别不保留
   原文；任何规则执行异常时整字段替换为 `[REDACTED:error]`，绝不放行原文。
2. `ContentCaptureGate` — 内容类字段（raw provider payload、完整 prompt、
   message content、tool args/result、attachment content）默认拒绝采集；
   debug 采集须 admin principal + reason + TTL 三要素齐备，开启即产生审计
   事件，TTL 惰性过期，进程内状态、重启回关闭态（fail-safe）。

规则为黑名单启发式，可能过度脱敏（安全方向）；content 默认关闭与 label
白名单是另外两道独立防线（见 change design ADR-3/ADR-4）。
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.telemetry.audit import (
    AUDIT_ACTION_ENABLE_CONTENT_CAPTURE,
    AdminAccessAuditEvent,
)

__all__ = [
    "CAPTURE_PLACEHOLDER",
    "DEFAULT_CAPTURE_TTL_SECONDS",
    "MAX_CAPTURE_TTL_SECONDS",
    "ContentCaptureGate",
    "ContentCaptureGateError",
    "ContentCaptureSession",
    "RedactionCategory",
    "RedactionStats",
    "PatternRule",
    "default_redaction_rules",
    "redact_text",
    "redact_value",
]

CAPTURE_PLACEHOLDER = "[REDACTED:error]"
DEFAULT_CAPTURE_TTL_SECONDS = 300.0
MAX_CAPTURE_TTL_SECONDS = 3600.0


class RedactionCategory:
    """脱敏类别（占位符中的类型标签）。"""

    SECRET = "secret"
    CREDENTIAL = "credential"
    LOCAL_PATH = "local_path"
    PII = "pii"
    ERROR = "error"


@dataclass(frozen=True)
class PatternRule:
    """一条脱敏规则：类别 + 预编译正则 + 替换模板。"""

    category: str
    pattern: re.Pattern[str]
    replacement: str


def default_redaction_rules() -> tuple[PatternRule, ...]:
    """构造默认规则序列（顺序即应用顺序，前类命中优先）。

    值模式（Bearer / sk-）先于键值对规则，避免 `Authorization: Bearer x`
    的值被键值对规则截断后泄露长 token；键名启发式覆盖常见 secret 命名；
    路径集合覆盖 Windows 盘符、UNC、常见 POSIX 用户/系统目录与 `~`。
    """
    rules: tuple[PatternRule, ...] = (
        PatternRule(
            RedactionCategory.SECRET,
            re.compile(r"(?i)\bbearer\s+[a-z0-9._\-]{8,}"),
            "[REDACTED:secret]",
        ),
        PatternRule(
            RedactionCategory.SECRET,
            re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
            "[REDACTED:secret]",
        ),
        PatternRule(
            RedactionCategory.SECRET,
            re.compile(
                r"(?i)\b(?P<key>api[_-]?key|api[_-]?secret|access[_-]?token|refresh[_-]?token"
                r"|auth[_-]?token|session[_-]?token|token|secret|password|passwd|pwd"
                r"|credential|authorization|cookie)\b\s*(?P<sep>[:=]+)\s*"
                r"(?:\"[^\"]{4,}\"|'[^']{4,}'|[^\s,;&\"']{4,})"
            ),
            r"\g<key>\g<sep>[REDACTED:secret]",
        ),
        PatternRule(
            RedactionCategory.CREDENTIAL,
            re.compile(
                r"(?i)\b(?P<scheme>[a-z][a-z0-9+.\-]{1,15})://[^\s/:@]+:[^\s/@]+@"
            ),
            r"\g<scheme>://[REDACTED:credential]@",
        ),
        PatternRule(
            RedactionCategory.LOCAL_PATH,
            re.compile(r"(?i)\b[a-z]:\\[^\s\"'<>|]{2,}"),
            "[REDACTED:local_path]",
        ),
        PatternRule(
            RedactionCategory.LOCAL_PATH,
            re.compile(r"\\\\[\w.\-]+\\[^\s\"']+"),
            "[REDACTED:local_path]",
        ),
        PatternRule(
            RedactionCategory.LOCAL_PATH,
            re.compile(
                r"(?:/(?:home|Users|root|opt|tmp|var)/[^\s\"'`,;:)\]}]*"
                r"|~/[^\s\"'`,;:)\]}]+)"
            ),
            "[REDACTED:local_path]",
        ),
        PatternRule(
            RedactionCategory.PII,
            re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
            "[REDACTED:pii]",
        ),
        PatternRule(
            RedactionCategory.PII,
            re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
            "[REDACTED:pii]",
        ),
        PatternRule(
            RedactionCategory.PII,
            re.compile(r"(?<!\d)\+\d{1,3}[- ]\d{6,12}(?!\d)"),
            "[REDACTED:pii]",
        ),
    )
    return rules


_DEFAULT_RULES = default_redaction_rules()


@dataclass
class RedactionStats:
    """脱敏命中/异常计数（观测脱敏自身的运行状况，不含内容）。"""

    hits: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add_hit(self, category: str) -> None:
        with self._lock:
            self.hits[category] = self.hits.get(category, 0) + 1

    def add_error(self) -> None:
        with self._lock:
            self.errors += 1


def _apply_rules(text: str, rules: Sequence[PatternRule], stats: RedactionStats) -> str:
    result = text
    for rule in rules:
        try:
            result, n = rule.pattern.subn(rule.replacement, result)
        except Exception:
            # fail-safe 契约：规则任何异常（坏正则、坏组引用等）→ 整字段占位，
            # 绝不放行原文。
            stats.add_error()
            return CAPTURE_PLACEHOLDER
        if n:
            stats.add_hit(rule.category)
    return result


def redact_text(
    text: str,
    *,
    rules: Sequence[PatternRule] | None = None,
    stats: RedactionStats | None = None,
) -> str:
    """对单条文本做四类脱敏；规则异常时返回 `[REDACTED:error]`。"""
    active_stats = stats if stats is not None else RedactionStats()
    active_rules = _DEFAULT_RULES if rules is None else rules
    return _apply_rules(text, active_rules, active_stats)


def redact_value(
    value: Any,
    *,
    max_depth: int = 8,
    max_items: int = 1000,
    rules: Sequence[PatternRule] | None = None,
    stats: RedactionStats | None = None,
) -> Any:
    """递归脱敏任意结构：str 过规则；mapping/list 递归（深度/条数封顶）；
    bytes 视为不透明内容整体替换；其余标量原样返回；无法识别对象 str 化后脱敏。
    """
    active_stats = stats if stats is not None else RedactionStats()
    active_rules = _DEFAULT_RULES if rules is None else rules
    return _redact_value(value, max_depth, max_items, 0, active_rules, active_stats)


def _redact_value(
    value: Any,
    max_depth: int,
    max_items: int,
    depth: int,
    rules: Sequence[PatternRule] | None,
    stats: RedactionStats,
) -> Any:
    if depth > max_depth:
        stats.add_error()
        return CAPTURE_PLACEHOLDER
    if isinstance(value, str):
        return redact_text(value, rules=rules, stats=stats)
    if isinstance(value, Mapping):
        items = list(value.items())[:max_items]
        return {
            str(k): _redact_value(v, max_depth, max_items, depth + 1, rules, stats)
            for k, v in items
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)[:max_items]
        redacted = [
            _redact_value(v, max_depth, max_items, depth + 1, rules, stats)
            for v in items
        ]
        if isinstance(value, tuple):
            return tuple(redacted)
        return redacted
    if isinstance(value, (bytes, bytearray)):
        stats.add_hit("bytes")
        return f"[REDACTED:bytes:{len(value)}]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value), rules=rules, stats=stats)


@dataclass(frozen=True)
class ContentCaptureSession:
    """一次已批准的内容采集窗口（gate 开启快照）。"""

    principal: str
    reason: str
    opened_at: float
    ttl_seconds: float


class ContentCaptureGateError(ValueError):
    """开启内容采集时要素不齐备或参数非法。"""


class ContentCaptureGate:
    """内容采集 gate（ADR-1/2）：默认关闭；admin 三要素开启；TTL 惰性过期。

    进程内状态，不持久化——重启回关闭态是刻意的 fail-safe。开启动作产生
    `enable_content_capture` 审计事件，写入注入的 sink 或内部 `audit_log`。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        audit_sink: Callable[[AdminAccessAuditEvent], None] | None = None,
        default_ttl_seconds: float = DEFAULT_CAPTURE_TTL_SECONDS,
        max_ttl_seconds: float = MAX_CAPTURE_TTL_SECONDS,
    ) -> None:
        self._clock = clock
        self._audit_sink = audit_sink
        self._default_ttl = float(default_ttl_seconds)
        self._max_ttl = float(max_ttl_seconds)
        self._session: ContentCaptureSession | None = None
        self._lock = threading.Lock()
        self.audit_log: list[AdminAccessAuditEvent] = []

    @property
    def default_ttl_seconds(self) -> float:
        return self._default_ttl

    @property
    def max_ttl_seconds(self) -> float:
        return self._max_ttl

    def open(
        self,
        *,
        principal: str,
        reason: str,
        ttl_seconds: float | None = None,
        tenant_id: str | None = None,
    ) -> ContentCaptureSession:
        """开启采集窗口：principal/reason 非空且 0 < ttl <= max_ttl 才允许。

        成功即产生一条 `enable_content_capture` 审计事件（先于窗口生效）。
        """
        principal_clean = (principal or "").strip()
        reason_clean = (reason or "").strip()
        if not principal_clean:
            raise ContentCaptureGateError("开启内容采集需要非空 principal")
        if not reason_clean:
            raise ContentCaptureGateError("开启内容采集需要非空 reason")
        effective_ttl = self._default_ttl if ttl_seconds is None else float(ttl_seconds)
        if not (0.0 < effective_ttl <= self._max_ttl):
            raise ContentCaptureGateError(
                f"ttl_seconds 必须落在 (0, {self._max_ttl}]，收到 {effective_ttl}"
            )
        event = AdminAccessAuditEvent(
            principal=principal_clean,
            action=AUDIT_ACTION_ENABLE_CONTENT_CAPTURE,
            target_kind="content_capture_gate",
            target_id="process",
            tenant_id=tenant_id,
            reason=reason_clean,
        )
        session = ContentCaptureSession(
            principal=principal_clean,
            reason=reason_clean,
            opened_at=self._clock(),
            ttl_seconds=effective_ttl,
        )
        with self._lock:
            self._session = session
            self.audit_log.append(event)
        if self._audit_sink is not None:
            self._audit_sink(event)
        return session

    def is_open(self) -> bool:
        """gate 当前是否开放（TTL 惰性检查，过期自动关闭）。"""
        with self._lock:
            session = self._session
            if session is None:
                return False
            if self._clock() - session.opened_at >= session.ttl_seconds:
                self._session = None
                return False
            return True

    def approve_capture(self) -> bool:
        """采集调用点询问接口：等价 `is_open()`（语义命名，供 emit 侧使用）。"""
        return self.is_open()

    def close(self) -> None:
        """显式关闭采集窗口（如 admin 处置完成）。"""
        with self._lock:
            self._session = None

    def current_session(self) -> ContentCaptureSession | None:
        """当前生效窗口快照；已过期返回 None。"""
        return self._session if self.is_open() else None
