"""C12 ContentCaptureGate 契约测试（ADR-1/2）。

验收锚点（task-12 第 1 条负向测试）：内容默认关闭；admin 开关须
principal+reason+TTL 三要素齐备且开启即审计；TTL 过期自动回关闭态。
"""

from __future__ import annotations

import pytest

from core.telemetry.audit import (
    AUDIT_ACTION_ENABLE_CONTENT_CAPTURE,
    AdminAccessAuditEvent,
)
from core.telemetry.redaction import (
    ContentCaptureGate,
    ContentCaptureGateError,
    redact_text,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _fresh_gate() -> tuple[ContentCaptureGate, FakeClock]:
    clock = FakeClock()
    return ContentCaptureGate(clock=clock), clock


def test_default_closed():
    gate, _ = _fresh_gate()
    assert gate.is_open() is False
    assert gate.approve_capture() is False


def test_open_requires_principal_and_reason_and_valid_ttl():
    gate, _ = _fresh_gate()
    with pytest.raises(ContentCaptureGateError):
        gate.open(principal="", reason="排查")
    with pytest.raises(ContentCaptureGateError):
        gate.open(principal="admin-1", reason="  ")
    with pytest.raises(ContentCaptureGateError):
        gate.open(principal="admin-1", reason="排查", ttl_seconds=0)
    with pytest.raises(ContentCaptureGateError):
        gate.open(principal="admin-1", reason="排查", ttl_seconds=3601)
    assert gate.is_open() is False
    assert gate.audit_log == []


def test_open_success_produces_audit_event():
    gate, _ = _fresh_gate()
    session = gate.open(principal="admin-1", reason="排查 turn 异常", ttl_seconds=60)
    assert gate.is_open() is True
    assert session.principal == "admin-1"
    assert len(gate.audit_log) == 1
    event = gate.audit_log[0]
    assert isinstance(event, AdminAccessAuditEvent)
    assert event.action == AUDIT_ACTION_ENABLE_CONTENT_CAPTURE
    assert event.principal == "admin-1"
    assert event.reason == "排查 turn 异常"


def test_audit_sink_receives_event():
    received: list[AdminAccessAuditEvent] = []
    gate = ContentCaptureGate(clock=FakeClock(), audit_sink=received.append)
    gate.open(principal="admin-1", reason="导出排查", ttl_seconds=30)
    assert len(received) == 1
    assert received[0].action == AUDIT_ACTION_ENABLE_CONTENT_CAPTURE


def test_ttl_expiry_lazy_close():
    gate, clock = _fresh_gate()
    gate.open(principal="admin-1", reason="短期排查", ttl_seconds=60)
    clock.advance(59)
    assert gate.is_open() is True
    clock.advance(2)
    assert gate.is_open() is False
    assert gate.approve_capture() is False


def test_close_is_immediate():
    gate, _ = _fresh_gate()
    gate.open(principal="admin-1", reason="短期排查", ttl_seconds=60)
    gate.close()
    assert gate.is_open() is False


def test_capture_path_redacts_before_persist():
    """gate 开启期间捕获的内容入库前仍强制脱敏（ADR-1）。"""
    gate, _ = _fresh_gate()
    gate.open(principal="admin-1", reason="调试", ttl_seconds=60)
    assert gate.approve_capture() is True
    captured = redact_text(
        "user email a.b@x.com token=abcd1234efgh path /home/hp/x.log"
    )
    assert "a.b@x.com" not in captured
    assert "abcd1234efgh" not in captured
    assert "/home/hp" not in captured
    assert "[REDACTED:pii]" in captured
    assert "[REDACTED:secret]" in captured


def test_fresh_process_starts_closed():
    """进程内状态、无持久化：新实例（模拟重启）默认关闭。"""
    gate, _ = _fresh_gate()
    gate.open(principal="admin-1", reason="调试", ttl_seconds=600)
    restarted = ContentCaptureGate(clock=FakeClock())
    assert restarted.is_open() is False


def test_audit_event_to_dict_redacts_reason():
    gate = ContentCaptureGate(clock=FakeClock())
    gate.open(principal="admin-1", reason="email leak@x.com 排查", ttl_seconds=60)
    data = gate.audit_log[0].to_dict(redact=redact_text)
    assert "leak@x.com" not in str(data["reason"])
    assert "[REDACTED:pii]" in str(data["reason"])
