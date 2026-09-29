"""E10 turn/tool_call/delivery 记录点契约测试（pg-durable-sot-cutover task 6.1）。

对齐 C15 模式（tests/test_work_queue_telemetry.py）：
- 事件字段 ⊆ fixture 白名单（双向：白名单 = fixture，事件不得自造字段）；
- `status` 只取 fixture 冻结枚举（未映射的原始词汇落 `result`，不强行映射）；
- 指标注册 label ⊆ C12 label policy（注册期 fail-fast 即验证）；
- delivery worker 的记录点回调在 sent/failed/dead_letter 三态触发。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bootstrap.webchat_telemetry import (
    WebchatLifecycleTelemetry,
    build_default_lifecycle_telemetry,
)
from bootstrap.work_queue_telemetry import ALLOWED_EVENT_FIELDS

FIXTURE = json.loads(
    (
        Path(__file__).parent / "fixtures" / "observability_event_schema.json"
    ).read_text(encoding="utf-8")
)
FIXTURE_FIELDS = frozenset(
    FIXTURE["lifecycle_event"]["field_groups"]["identity"]
    + FIXTURE["lifecycle_event"]["field_groups"]["classification"]
    + FIXTURE["lifecycle_event"]["field_groups"]["version"]
    + FIXTURE["lifecycle_event"]["field_groups"]["lifecycle_time"]
    + FIXTURE["lifecycle_event"]["field_groups"]["lifecycle_result"]
    + FIXTURE["lifecycle_event"]["field_groups"]["side_effect_recovery"]
    + FIXTURE["lifecycle_event"]["field_groups"]["resource"]
    + list(FIXTURE["lifecycle_event"]["derived_metrics"])
)
FIXTURE_STATUS_ENUM = frozenset(FIXTURE["lifecycle_event"]["enums"]["status"])


def test_allowed_event_fields_equal_fixture() -> None:
    """双向断言：本记录点沿用 work_queue_telemetry 的单一白名单且与 fixture 一致。"""
    assert ALLOWED_EVENT_FIELDS == FIXTURE_FIELDS


def test_turn_event_fields_within_fixture() -> None:
    telemetry = WebchatLifecycleTelemetry()
    event = telemetry.turn_finished(
        turn_id="t-1",
        tenant_id="t1",
        status="completed",
        channel="chat",
        started_at="2026-09-29T00:00:00+00:00",
        finished_at="2026-09-29T00:00:01+00:00",
        error=None,
        error_type=None,
    )
    # 类型化签名是第一层结构保证（未知键直接 TypeError）；_emit 白名单是
    # dict 构造路径的纵深防御。这里断言产出 ⊆ fixture。
    assert set(event) <= ALLOWED_EVENT_FIELDS
    assert event["status"] == "completed"


def test_turn_event_rejects_status_outside_enum() -> None:
    telemetry = WebchatLifecycleTelemetry()
    with pytest.raises(ValueError):
        telemetry.turn_finished(
            turn_id="t-1", tenant_id="t1", status="sent"  # type: ignore[arg-type]
        )


def test_delivery_status_mapping_and_fields() -> None:
    telemetry = WebchatLifecycleTelemetry()
    sent = telemetry.delivery_finished(
        message_id="m-1", turn_id="t-1", tenant_id="t1", channel="chat",
        result="sent", attempt=1,
    )
    assert sent["status"] == "completed" and sent["result"] == "sent"
    failed = telemetry.delivery_finished(
        message_id="m-1", turn_id="t-1", tenant_id="t1", channel="chat",
        result="failed", attempt=2, error="无在线连接",
    )
    assert failed["status"] == "failed" and failed["retryable"] is True
    assert failed["last_error"] == "无在线连接"  # redact_text 对非敏感文本原样保留
    dead = telemetry.delivery_finished(
        message_id="m-1", turn_id="t-1", tenant_id="t1", channel="chat",
        result="dead_letter", attempt=5, error="无在线连接",
    )
    assert dead["status"] == "failed" and dead["retryable"] is False
    for event in (sent, failed, dead):
        assert set(event) <= ALLOWED_EVENT_FIELDS
        assert event["status"] in FIXTURE_STATUS_ENUM
    with pytest.raises(ValueError):
        telemetry.delivery_finished(
            message_id="m", turn_id=None, tenant_id="t1", channel="chat",
            result="pending",  # type: ignore[arg-type]
        )


def test_tool_call_result_vocabulary_and_status_mapping() -> None:
    telemetry = WebchatLifecycleTelemetry()
    ok = telemetry.tool_call_finished(
        tool_call_id="c-1", turn_id="t-1", tenant_id="t1",
        tool_name="web_search", result="succeeded", duration_ms=120,
        effect_class="network",
    )
    assert ok["status"] == "completed" and ok["result"] == "succeeded"
    unknown = telemetry.tool_call_finished(
        tool_call_id="c-2", turn_id="t-1", tenant_id="t1",
        tool_name="web_search", result="unknown",
    )
    # 无损映射不可得时 status 省略（同 C15 claim 事件先例），原始终态保留在 result。
    assert "status" not in unknown and unknown["result"] == "unknown"
    rejected = telemetry.tool_call_finished(
        tool_call_id="c-3", turn_id=None, tenant_id="t1",
        tool_name="shell", result="rejected",
    )
    assert "status" not in rejected and rejected["result"] == "rejected"
    for event in (ok, unknown, rejected):
        assert set(event) <= ALLOWED_EVENT_FIELDS


def test_metric_registration_labels_within_policy() -> None:
    # build_default_lifecycle_telemetry 内部 validate_label_names 越界即抛错；
    # 构造成功即证明全部 label 在 C12 白名单内。
    telemetry = build_default_lifecycle_telemetry()
    assert telemetry is not None
    # 幂等注册（同 registry 二次构造不抛错）。
    again = build_default_lifecycle_telemetry()
    assert again is not None


# ── delivery worker 记录点回调（task 6.1；fake repo 无 PG） ────────────


class _FakeDeliveryRepo:
    """最小 DeliveryRepository 假件：一批一个 intent，sent/failed 可编排。"""

    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.claimed = False

    async def claim_batch(self, owner: str, **_: Any) -> list[dict[str, Any]]:
        if self.claimed:
            return []
        self.claimed = True
        return [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "tenant_id": "t1",
                "conversation_id": "22222222-2222-2222-2222-222222222222",
                "message_id": "33333333-3333-3333-3333-333333333333",
                "turn_id": "t-1",
                "idempotency_key": "msg:m-1",
                "channel": "chat",
                "target_chat_id": "t1",
                "payload": {},
                "attempt_count": 1,
                "status": "attempting",
            }
        ]

    async def heartbeat(self, *a: Any, **k: Any) -> bool:
        return True

    async def record_attempt_sent(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {"status": "sent", "attempt_count": 1}

    async def record_attempt_failed(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {"status": "failed" if not self.fail else "dead_letter", "attempt_count": 1, "next_attempt_at": None}


@pytest.mark.asyncio
async def test_worker_emits_delivery_events_on_sent_and_failed() -> None:
    from bootstrap.delivery_worker import OutboundDeliveryWorker

    events: list[dict[str, Any]] = []

    async def send_ok(envelope: Any) -> str | None:
        return "webchat:1"

    async def send_fail(envelope: Any) -> str | None:
        raise RuntimeError("无在线连接")

    worker = OutboundDeliveryWorker(
        _FakeDeliveryRepo(fail=False), send_ok,  # type: ignore[arg-type]
        on_delivery_finished=events.append,
    )
    await worker.process_once()
    assert [e["result"] for e in events] == ["sent"]
    assert events[0]["intent"]["turn_id"] == "t-1"

    worker2 = OutboundDeliveryWorker(
        _FakeDeliveryRepo(fail=True), send_fail,  # type: ignore[arg-type]
        on_delivery_finished=events.append,
        config=_dead_letter_cfg(),
    )
    await worker2.process_once()
    assert events[-1]["result"] == "dead_letter"


def _dead_letter_cfg() -> Any:
    from bootstrap.delivery_worker import DeliveryWorkerConfig

    return DeliveryWorkerConfig(max_attempts=1, backoff_seconds=(60.0,))
