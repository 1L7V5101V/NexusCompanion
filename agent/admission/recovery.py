"""启动恢复扫描框架（PILOT_ROADMAP §5.9.6 / §6.1 B）。

恢复语义冻结为：

- final message、turn/tool terminal state、用户必须看到的业务结果不能丢；
- 启动时扫描非终态 work；纯内部、声明幂等的 work 可 ``recompute``；
  外部副作用 tool 先进入 ``unknown`` 或 ``compensation_required``，查询 outcome 后
  再决定补偿——**禁止无确认重放**（不先确认第一次调用是否成功就再执行一次外部操作）；
- 每次恢复记录 ``recovery_started_at`` / ``recovery_finished_at`` / ``recovery_action`` /
  ``recovery_result`` / 原始 work id / attempt（供 C12 §7.1 指标复用）。

P0 内置来源：control store（turn audit）非终态 turn → ``cancelled``（运行中
Python task 随重启中断，无外部副作用确认手段）。``unknown`` / ``compensation_required``
的 outcome 查询与补偿执行闭环依赖 C2 ``tool_calls`` durable 表，本模块先冻结
状态语义与记录结构。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from agent.control.models import TurnRecord, TurnStatus

logger = logging.getLogger(__name__)


class RecoveryAction(StrEnum):
    """§6.1 B 恢复动作枚举：每条未完成任务最终必须可解释为其中之一。"""

    REPLAY = "replay"
    RECOMPUTE = "recompute"
    COMPENSATE = "compensate"
    CANCELLED = "cancelled"
    MISSED = "missed"
    INTENTIONALLY_SKIPPED = "intentionally_skipped"


class ToolOutcomeStatus(StrEnum):
    """外部副作用 tool 的结果状态（§6.1 B 表格语义）。"""

    OK = "ok"
    FAILED = "failed"
    UNKNOWN = "unknown"
    COMPENSATION_REQUIRED = "compensation_required"


@dataclass(frozen=True)
class PendingWorkRecord:
    """恢复扫描发现的一条未完成 work。"""

    work_id: str
    work_kind: str
    tenant_id: str
    session_key: str
    status: str
    attempt: int = 1


@dataclass(frozen=True)
class RecoveryRecord:
    """一次恢复处置的观测记录（字段供 C12 §7.1 指标复用）。"""

    work_id: str
    work_kind: str
    tenant_id: str
    session_key: str
    recovery_action: RecoveryAction
    recovery_result: str
    recovery_started_at: datetime
    recovery_finished_at: datetime
    attempt: int = 1
    detail: str = ""

    def to_log_fields(self) -> dict[str, object]:
        return {
            "work_id": self.work_id,
            "work_kind": self.work_kind,
            "tenant_id": self.tenant_id,
            "session_key": self.session_key,
            "recovery_action": self.recovery_action.value,
            "recovery_result": self.recovery_result,
            "recovery_started_at": self.recovery_started_at.isoformat(),
            "recovery_finished_at": self.recovery_finished_at.isoformat(),
            "attempt": self.attempt,
            "detail": self.detail,
        }


class RecoverySource(Protocol):
    """恢复来源协议：发现未完成 work 并执行处置。

    C2 落地后由 durable 表来源实现本协议（inbox/work/tool/outbox），
    P0 来源为 control store 非终态 turn。
    """

    def name(self) -> str: ...

    def collect_pending(self) -> list[PendingWorkRecord]: ...

    def apply(self, pending: PendingWorkRecord) -> tuple[RecoveryAction, str]: ...


class TurnAuditStoreProtocol(Protocol):
    """turn audit 恢复来源所需的 store 面（SessionStore 提供实现）。"""

    def list_non_terminal_turns(self) -> list[dict[str, object]]: ...

    def transition_turn(
        self,
        turn_id: str,
        *,
        expected_status: "TurnStatus",
        status: "TurnStatus",
    ) -> "TurnRecord": ...


class TurnAuditRecoverySource:
    """P0 来源：control store 中非终态（queued/in_progress）turn。

    §6.1 B「active turn/tool：重启时将运行中的 Python task 视为被中断」——
    进程已消失，运行中 task 不可能继续；turn 无外部副作用确认手段（tool_calls
    表归 C2），统一处置为 ``cancelled`` 并在 detail 注明 unknown 边界。
    """

    def __init__(self, store: TurnAuditStoreProtocol) -> None:
        self._store = store

    def name(self) -> str:
        return "turn_audit"

    def collect_pending(self) -> list[PendingWorkRecord]:
        records: list[PendingWorkRecord] = []
        for row in self._store.list_non_terminal_turns():
            records.append(
                PendingWorkRecord(
                    work_id=str(row["id"]),
                    work_kind="turn",
                    tenant_id=str(row.get("tenant_id") or ""),
                    session_key=str(row["session_key"]),
                    status=str(row["status"]),
                )
            )
        return records

    def apply(self, pending: PendingWorkRecord) -> tuple[RecoveryAction, str]:
        terminal = self._store.transition_turn(
            pending.work_id,
            expected_status=TurnStatus(pending.status),
            status=TurnStatus.CANCELLED,
        )
        detail = (
            "restart interrupted in-process task; tool outcome confirmation "
            "requires C2 tool_calls table (unknown/compensation 未启用)"
        )
        return RecoveryAction.CANCELLED, f"{terminal.status.value}: {detail}"


class StartupRecoveryScanner:
    """启动时逐来源扫描未完成 work 并产出 :class:`RecoveryRecord`。"""

    def __init__(self, sources: list[RecoverySource]) -> None:
        self._sources = sources
        self.records: list[RecoveryRecord] = []

    def scan(self) -> list[RecoveryRecord]:
        """执行扫描；每个来源内部失败不阻断其他来源（恢复自身 must not raise）。"""
        self.records = []
        for source in self._sources:
            started = datetime.now(UTC)
            try:
                pending_items = source.collect_pending()
            except Exception:
                logger.exception("recovery source collect failed: %s", source.name())
                continue
            for pending in pending_items:
                finished = datetime.now(UTC)
                try:
                    action, result = source.apply(pending)
                except Exception as exc:
                    action, result = (
                        RecoveryAction.CANCELLED,
                        f"apply_failed: {type(exc).__name__}: {exc}",
                    )
                    logger.exception(
                        "recovery apply failed: source=%s work_id=%s",
                        source.name(),
                        pending.work_id,
                    )
                record = RecoveryRecord(
                    work_id=pending.work_id,
                    work_kind=pending.work_kind,
                    tenant_id=pending.tenant_id,
                    session_key=pending.session_key,
                    recovery_action=action,
                    recovery_result=result,
                    recovery_started_at=started,
                    recovery_finished_at=finished,
                    attempt=pending.attempt,
                )
                self.records.append(record)
                logger.info(
                    "startup recovery applied",
                    extra=record.to_log_fields(),
                )
        logger.info(
            "startup recovery scan finished: sources=%s records=%s",
            len(self._sources),
            len(self.records),
        )
        return self.records
