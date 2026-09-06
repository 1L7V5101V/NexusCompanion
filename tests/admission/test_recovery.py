"""C3 启动恢复扫描：非终态 turn 处置、RecoveryRecord 字段、unknown/compensation 语义。"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.admission.recovery import (
    PendingWorkRecord,
    RecoveryAction,
    RecoveryRecord,
    StartupRecoveryScanner,
    ToolOutcomeStatus,
    TurnAuditRecoverySource,
)
from agent.control.models import TurnRecord, TurnStatus
from session.store import SessionStore


def _queued_turn(store: SessionStore, thread_id: str) -> TurnRecord:
    return store.create_turn(
        TurnRecord(
            id=f"turn-{thread_id}-1",
            thread_id=thread_id,
            status=TurnStatus.QUEUED,
            input="hello",
            metadata={"tenantId": thread_id},
            created_at=datetime.now(UTC),
            items=[],
            usage=None,
            error=None,
        )
    )


def test_unknown_and_compensation_semantics_distinct() -> None:
    # §6.1 B：unknown ≠ 成功/取消，只表示结果尚未可判定；compensation_required
    # 表示已确认/高度怀疑有副作用，必须先恢复外部状态。
    assert ToolOutcomeStatus.UNKNOWN.value == "unknown"
    assert ToolOutcomeStatus.COMPENSATION_REQUIRED.value == "compensation_required"
    assert {action.value for action in RecoveryAction} == {
        "replay",
        "recompute",
        "compensate",
        "cancelled",
        "missed",
        "intentionally_skipped",
    }


async def test_recovery_scan_marks_nonterminal_turns_cancelled(tmp_path) -> None:
    store = SessionStore(tmp_path / "control.db")
    _queued_turn(store, "thread-a")
    # in_progress turn：重启时运行中 task 被视为中断
    record = _queued_turn(store, "thread-b")
    store.transition_turn(
        record.id,
        expected_status=TurnStatus.QUEUED,
        status=TurnStatus.IN_PROGRESS,
    )
    # 终态 turn 不参与扫描
    done = _queued_turn(store, "thread-c")
    store.transition_turn(
        done.id,
        expected_status=TurnStatus.QUEUED,
        status=TurnStatus.IN_PROGRESS,
    )
    store.transition_turn(
        done.id,
        expected_status=TurnStatus.IN_PROGRESS,
        status=TurnStatus.COMPLETED,
        final_response="ok",
    )

    source = TurnAuditRecoverySource(store)
    pending = source.collect_pending()
    assert {p.work_id for p in pending} == {"turn-thread-a-1", "turn-thread-b-1"}
    assert all(p.status in ("queued", "in_progress") for p in pending)

    scanner = StartupRecoveryScanner([source])
    records = scanner.scan()
    assert len(records) == 2
    by_work = {r.work_id: r for r in records}
    for work_id in ("turn-thread-a-1", "turn-thread-b-1"):
        recovered = by_work[work_id]
        assert isinstance(recovered, RecoveryRecord)
        assert recovered.recovery_action is RecoveryAction.CANCELLED
        assert recovered.recovery_result.startswith("cancelled")
        assert recovered.recovery_started_at <= recovered.recovery_finished_at
        assert recovered.attempt == 1
        assert "C2 tool_calls" in recovered.recovery_result
    # 扫描后 store 中无残留非终态
    assert store.list_non_terminal_turns() == []
    assert store.read_turn("turn-thread-a-1") is not None


def test_scanner_isolates_failing_source() -> None:
    class BrokenSource:
        def name(self) -> str:
            return "broken"

        def collect_pending(self) -> list[PendingWorkRecord]:
            raise RuntimeError("boom")

        def apply(self, pending: PendingWorkRecord):
            raise AssertionError  # pragma: no cover

    class HealthySource:
        def __init__(self) -> None:
            self.applied = False

        def name(self) -> str:
            return "healthy"

        def collect_pending(self) -> list[PendingWorkRecord]:
            return [
                PendingWorkRecord(
                    work_id="w1",
                    work_kind="work",
                    tenant_id="t1",
                    session_key="s1",
                    status="queued",
                )
            ]

        def apply(self, pending: PendingWorkRecord):
            self.applied = True
            return RecoveryAction.RECOMPUTE, "recomputed from durable state"

    healthy = HealthySource()
    scanner = StartupRecoveryScanner([BrokenSource(), healthy])
    records = scanner.scan()
    assert healthy.applied
    assert len(records) == 1
    assert records[0].recovery_action is RecoveryAction.RECOMPUTE


def test_apply_failure_recorded_not_raised() -> None:
    class FailingApply:
        def name(self) -> str:
            return "failing-apply"

        def collect_pending(self) -> list[PendingWorkRecord]:
            return [
                PendingWorkRecord(
                    work_id="w2",
                    work_kind="turn",
                    tenant_id="t1",
                    session_key="s1",
                    status="queued",
                )
            ]

        def apply(self, pending: PendingWorkRecord):
            raise RuntimeError("transition conflict")

    records = StartupRecoveryScanner([FailingApply()]).scan()
    assert len(records) == 1
    assert records[0].recovery_result.startswith("apply_failed")


def test_recovery_record_log_fields_complete() -> None:
    started = datetime.now(UTC)
    record = RecoveryRecord(
        work_id="w1",
        work_kind="turn",
        tenant_id="t1",
        session_key="s1",
        recovery_action=RecoveryAction.CANCELLED,
        recovery_result="cancelled",
        recovery_started_at=started,
        recovery_finished_at=started,
        attempt=2,
    )
    fields = record.to_log_fields()
    # C12 §7.1 指标字段契约
    for key in (
        "recovery_started_at",
        "recovery_finished_at",
        "recovery_action",
        "recovery_result",
        "work_id",
        "attempt",
    ):
        assert key in fields
        assert fields[key] is not None
