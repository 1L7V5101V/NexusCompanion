"""Durable control plane repository（C2）。

三事务边界（openspec/changes/c2-durable-control-plane/design.md §1，
PILOT_ROADMAP §5.9.11 / §10 DECIDED）：

- **T1 入站接受事务**：`IngressRepository.accept_inbound()` —— dedupe
  （`ON CONFLICT DO NOTHING`，冲突即回查返回既有身份、零写入）+ canonical
  user message（复用 C1 冻结语义：单事务 `UPDATE ... RETURNING` 取号 +
  INSERT）+ `inbox_records(accepted)` + `turns(queued)` + 可选
  `background_work_items(queued)`，全部同一事务。
- **T2 执行完成事务**：`TurnControlRepository.complete_turn_with_delivery()`
  —— final assistant message（取号）+ turn 终态 + pending outbox intent，
  同一事务。
- **T3 delivery ack**：`DeliveryRepository` 的 claim/heartbeat/attempt 推进
  均为独立短事务，不与任何业务写入合并；`sent` 只能由发送方成功确认推进
  （无 ack 永不 sent）。

本模块没有任何旧单体存储回退路径（§10 DECIDED：不 fallback、不双写）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.models.canonical import (
    CanonicalConversationModel,
    CanonicalMessageModel,
)
from bootstrap.db.models.control_plane import (
    BackgroundWorkItemModel,
    DeliveryAttemptModel,
    InboxRecordModel,
    MessageDeduplicationKeyModel,
    OutboundDeliveryIntentModel,
    ToolCallModel,
    TurnModel,
)

__all__ = [
    "AcceptInboundResult",
    "CompletionResult",
    "ControlPlaneError",
    "DeliveryIntentNotFoundError",
    "DeliveryRepository",
    "IngressRepository",
    "LeaseLostError",
    "NotFoundError",
    "RedriveNotAllowedError",
    "TransitionError",
    "TurnControlRepository",
    "TurnNotFoundError",
]

# 单事务认领批量（`FOR UPDATE SKIP LOCKED`：多扫描器互不阻塞；stale lease 由
# 第二个分支自然接管，design.md ADR-5）。pending/failed 分支按本周期
# attempt_count < max_attempts 把门，防止 dead_letter 前无限认领；stale
# attempting 分支不受该门限制（worker 崩溃残留必须可接管收束）。
_CLAIM_SQL = text(
    """
    WITH due AS (
        SELECT id FROM outbound_delivery_intents
        WHERE (
            (status IN ('pending', 'failed')
             AND next_attempt_at <= now() AND attempt_count < :max_attempts)
            OR (status = 'attempting' AND lease_expires_at < now())
        )
        ORDER BY next_attempt_at
        LIMIT :batch_size
        FOR UPDATE SKIP LOCKED
    )
    UPDATE outbound_delivery_intents i
    SET status = 'attempting',
        lease_owner = :owner,
        lease_expires_at = now() + make_interval(secs => :lease_ttl),
        attempt_count = i.attempt_count + 1,
        updated_at = now()
    FROM due
    WHERE i.id = due.id
    RETURNING i.id, i.tenant_id, i.conversation_id, i.message_id, i.turn_id,
              i.idempotency_key, i.channel, i.target_chat_id, i.payload_json,
              i.status, i.attempt_count, i.lease_owner, i.lease_expires_at,
              i.next_attempt_at, i.last_error, i.sent_at, i.created_at, i.updated_at
    """
)

_WORK_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


class ControlPlaneError(Exception):
    """control plane 仓储错误基类。"""


class NotFoundError(ControlPlaneError, LookupError):
    """目标行不存在或 tenant 不匹配（fail-closed：不落默认值、不泄露归属）。"""


class TurnNotFoundError(NotFoundError):
    """目标 turn 不存在或 tenant 不匹配。"""


class DeliveryIntentNotFoundError(NotFoundError):
    """目标投递意图不存在或 tenant 不匹配。"""


class TransitionError(ControlPlaneError):
    """CAS 状态推进失败：当前状态与 expected 不符。"""


class LeaseLostError(ControlPlaneError):
    """投递租约已失（他人接管或状态已变）；本次尝试不得再推进 sent（ADR-5）。"""


class RedriveNotAllowedError(ControlPlaneError):
    """非 dead_letter 状态的 redrive 请求被拒绝（ADR-6）。"""


@dataclass(frozen=True)
class AcceptInboundResult:
    """T1 结果：duplicate=True 时返回既有身份（幂等成功路径，零新写入）。"""

    duplicate: bool
    inbox_id: str
    message_id: str
    sequence: int
    turn_id: str | None
    work_item_ids: tuple[str, ...]


@dataclass(frozen=True)
class CompletionResult:
    """T2 结果：final message / turn 终态 / pending intent 同事务产物。"""

    message: dict[str, Any]
    turn: dict[str, Any]
    intent: dict[str, Any]


def _new_uuid() -> uuid.UUID:
    return uuid.uuid4()


def _coerce_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _require_uuid(value: uuid.UUID | str, what: str) -> uuid.UUID:
    coerced = _coerce_uuid(value)
    if coerced is None:
        raise ValueError(f"{what} 需要有效 UUID: {value!r}")
    return coerced


def _to_json(data: Any) -> str | None:
    return json.dumps(data, ensure_ascii=False) if data is not None else None


def _from_json(s: str | None) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


class IngressRepository:
    """T1 入站接受事务与 inbox 收束（`complete_inbound` 等价语义）。"""

    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    async def accept_inbound(
        self,
        tenant_id: str,
        conversation_id: uuid.UUID | str,
        *,
        source_channel: str | None = None,
        source_identity_id: str | None = None,
        source_message_id: str | None = None,
        account_id: uuid.UUID | str | None = None,
        client_message_id: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
        create_turn: bool = True,
        turn_id: uuid.UUID | str | None = None,
        work_items: list[dict[str, Any]] | None = None,
        inbox_id: uuid.UUID | str | None = None,
        message_id: uuid.UUID | str | None = None,
        dedup_key_id: uuid.UUID | str | None = None,
    ) -> AcceptInboundResult:
        """原子接受一条入站消息；重复注入返回既有身份（ADR-3），零新写入。

        幂等键二选一：Telegram 侧 `source_channel + source_identity_id +
        source_message_id`，或 WebChat 侧 `account_id + client_message_id`。
        `work_items` 每项为
        ``{"work_kind": str, "idempotency_key": str | None, "payload": dict | None,
        "work_item_id": uuid | str | None}``。
        会话不存在 / tenant 不符时整体回滚（零写入）。
        """
        conv_id = _require_uuid(conversation_id, "conversation_id")
        has_source_key = source_message_id is not None
        has_client_key = client_message_id is not None
        if has_source_key == has_client_key:
            raise ValueError(
                "accept_inbound 需要恰好一类幂等键：Telegram source 三元组或 "
                "WebChat account_id + client_message_id"
            )
        if has_source_key and not (source_channel and source_identity_id):
            raise ValueError("Telegram 幂等键需要 source_channel + source_identity_id")
        if has_client_key and account_id is None:
            raise ValueError("WebChat 幂等键需要 account_id")

        dedup_id = _coerce_uuid(dedup_key_id) or _new_uuid()
        msg_id = _coerce_uuid(message_id) or _new_uuid()
        inbx_id = _coerce_uuid(inbox_id) or _new_uuid()
        trn_id = _coerce_uuid(turn_id)

        async with self._sf() as sess, sess.begin():
            # 1. dedupe 插入：冲突目标必须是部分唯一索引的谓词（ADR-2/ADR-3）。
            if has_source_key:
                dedup_stmt = (
                    _pg_insert(MessageDeduplicationKeyModel)
                    .values(
                        id=dedup_id,
                        tenant_id=tenant_id,
                        source_channel=source_channel,
                        source_identity_id=source_identity_id,
                        source_message_id=source_message_id,
                    )
                    .on_conflict_do_nothing(
                        index_where=MessageDeduplicationKeyModel.source_message_id.is_not(None)
                    )
                )
            else:
                dedup_stmt = (
                    _pg_insert(MessageDeduplicationKeyModel)
                    .values(
                        id=dedup_id,
                        tenant_id=tenant_id,
                        account_id=_coerce_uuid(account_id),
                        client_message_id=client_message_id,
                    )
                    .on_conflict_do_nothing(
                        index_where=MessageDeduplicationKeyModel.client_message_id.is_not(None)
                    )
                )
            inserted = (await sess.execute(dedup_stmt)).rowcount
            if inserted == 0:
                # 重复注入：回查既有身份，零新写入（本事务只读）。
                existing_key = await self._find_dedup_key(
                    sess,
                    tenant_id,
                    source_channel=source_channel,
                    source_identity_id=source_identity_id,
                    source_message_id=source_message_id,
                    account_id=_coerce_uuid(account_id),
                    client_message_id=client_message_id,
                )
                if existing_key is None:  # 正常流程不可达：约束冲突必有既有行。
                    raise NotFoundError(
                        f"dedupe 冲突但既有键不可回查: tenant_id={tenant_id!r}"
                    )
                return await self._existing_inbound_result(sess, existing_key)

            # 2. canonical user message：单事务取号 + 写入（C1 冻结语义）。
            allocated = (
                await sess.execute(
                    update(CanonicalConversationModel)
                    .where(
                        CanonicalConversationModel.id == conv_id,
                        CanonicalConversationModel.tenant_id == tenant_id,
                    )
                    .values(next_sequence=CanonicalConversationModel.next_sequence + 1)
                    .returning(CanonicalConversationModel.next_sequence - 1)
                )
            ).scalar_one_or_none()
            if allocated is None:
                # 覆盖「会话不存在」与「tenant 不符」，不区分泄露归属信息。
                raise NotFoundError(
                    f"canonical conversation 不存在: conversation_id={conv_id!r} "
                    f"tenant_id={tenant_id!r}"
                )
            message_row = CanonicalMessageModel(
                id=msg_id,
                tenant_id=tenant_id,
                conversation_id=conv_id,
                sequence=allocated,
                role="user",
                content=content,
                source_channel=source_channel,
                source_identity_id=source_identity_id,
                source_message_id=source_message_id,
                client_message_id=client_message_id,
                metadata_json=_to_json(metadata),
            )
            sess.add(message_row)
            await sess.flush()

            # 3. inbox 记录（accepted）。
            inbox_row = InboxRecordModel(
                id=inbx_id,
                tenant_id=tenant_id,
                conversation_id=conv_id,
                dedup_key_id=dedup_id,
                canonical_message_id=msg_id,
                status="accepted",
            )
            sess.add(inbox_row)
            await sess.flush()

            # 4. queued turn（唯一锚定本 inbox）。
            turn_id_str: str | None = None
            if create_turn:
                turn_row = TurnModel(
                    id=trn_id or _new_uuid(),
                    tenant_id=tenant_id,
                    conversation_id=conv_id,
                    inbox_record_id=inbx_id,
                    status="queued",
                )
                sess.add(turn_row)
                await sess.flush()
                turn_id_str = str(turn_row.id)

            # 5. 可选 queued 后台工作项（幂等键冲突 → 复用既有行）。
            work_item_ids: list[str] = []
            for spec in work_items or []:
                work_item_ids.append(
                    await _insert_work_item(sess, tenant_id, conv_id, spec)
                )

            return AcceptInboundResult(
                duplicate=False,
                inbox_id=str(inbox_row.id),
                message_id=str(message_row.id),
                sequence=allocated,
                turn_id=turn_id_str,
                work_item_ids=tuple(work_item_ids),
            )

    async def _find_dedup_key(
        self,
        sess: Any,
        tenant_id: str,
        *,
        source_channel: str | None,
        source_identity_id: str | None,
        source_message_id: str | None,
        account_id: uuid.UUID | None,
        client_message_id: str | None,
    ) -> MessageDeduplicationKeyModel | None:
        stmt = select(MessageDeduplicationKeyModel).where(
            MessageDeduplicationKeyModel.tenant_id == tenant_id
        )
        if source_message_id is not None:
            stmt = stmt.where(
                MessageDeduplicationKeyModel.source_channel == source_channel,
                MessageDeduplicationKeyModel.source_identity_id == source_identity_id,
                MessageDeduplicationKeyModel.source_message_id == source_message_id,
            )
        else:
            stmt = stmt.where(
                MessageDeduplicationKeyModel.account_id == account_id,
                MessageDeduplicationKeyModel.client_message_id == client_message_id,
            )
        return (await sess.execute(stmt)).scalar_one_or_none()

    async def _existing_inbound_result(
        self, sess: Any, key: MessageDeduplicationKeyModel
    ) -> AcceptInboundResult:
        inbox = (
            await sess.execute(
                select(InboxRecordModel).where(InboxRecordModel.dedup_key_id == key.id)
            )
        ).scalar_one_or_none()
        if inbox is None:  # 正常流程不可达：接受事务原子，键存在则 inbox 存在。
            raise NotFoundError(f"dedupe 键存在但 inbox 缺失: dedup_key_id={key.id}")
        sequence = (
            await sess.execute(
                select(CanonicalMessageModel.sequence).where(
                    CanonicalMessageModel.id == inbox.canonical_message_id
                )
            )
        ).scalar_one_or_none()
        turn_id: str | None = None
        turn = (
            await sess.execute(
                select(TurnModel).where(TurnModel.inbox_record_id == inbox.id)
            )
        ).scalar_one_or_none()
        if turn is not None:
            turn_id = str(turn.id)
        return AcceptInboundResult(
            duplicate=True,
            inbox_id=str(inbox.id),
            message_id=str(inbox.canonical_message_id),
            sequence=int(sequence) if sequence is not None else -1,
            turn_id=turn_id,
            work_item_ids=(),
        )

    async def mark_inbox_processed(
        self, tenant_id: str, inbox_id: uuid.UUID | str
    ) -> dict[str, Any]:
        """accepted → processed（幂等）；不表示 channel 已成功展示（§5.9.11）。"""
        inbx_id = _require_uuid(inbox_id, "inbox_id")
        async with self._sf() as sess, sess.begin():
            row = await sess.get(InboxRecordModel, inbx_id, with_for_update=True)
            if row is None or row.tenant_id != tenant_id:
                raise NotFoundError(f"inbox 不存在: inbox_id={inbx_id!r}")
            if row.status == "accepted":
                row.status = "processed"
                row.processed_at = datetime.now(UTC)
            return _inbox_to_dict(row)

    async def get_inbox(
        self, tenant_id: str, inbox_id: uuid.UUID | str
    ) -> dict[str, Any] | None:
        inbx_id = _coerce_uuid(inbox_id)
        if inbx_id is None:
            return None
        async with self._sf() as sess:
            row = await sess.get(InboxRecordModel, inbx_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _inbox_to_dict(row)


class TurnControlRepository:
    """turn/tool/work 状态推进与 T2 执行完成事务。"""

    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    async def transition_turn(
        self,
        tenant_id: str,
        turn_id: uuid.UUID | str,
        *,
        expected_status: str,
        new_status: str,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """CAS 推进 turn 状态（沿用 ConversationRuntime 的 expected 语义）。"""
        trn_id = _require_uuid(turn_id, "turn_id")
        async with self._sf() as sess, sess.begin():
            row = (
                await sess.execute(
                    select(TurnModel)
                    .where(TurnModel.id == trn_id, TurnModel.tenant_id == tenant_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise TurnNotFoundError(f"turn 不存在: turn_id={trn_id!r}")
            if row.status != expected_status:
                raise TransitionError(
                    f"turn 状态推进失败: turn_id={trn_id!r} "
                    f"expected={expected_status!r} current={row.status!r}"
                )
            row.status = new_status
            row.error_json = _to_json(error)
            row.updated_at = datetime.now(UTC)
            return _turn_to_dict(row)

    async def complete_turn_with_delivery(
        self,
        tenant_id: str,
        conversation_id: uuid.UUID | str,
        turn_id: uuid.UUID | str,
        *,
        expected_status: str,
        response_content: str | None,
        delivery_channel: str,
        delivery_target: str,
        delivery_idempotency_key: str | None = None,
        delivery_payload: dict[str, Any] | None = None,
        message_metadata: dict[str, Any] | None = None,
        message_id: uuid.UUID | str | None = None,
        intent_id: uuid.UUID | str | None = None,
    ) -> CompletionResult:
        """T2：final assistant message + turn 终态 + pending intent 单事务原子提交。

        投递幂等键默认 ``msg:<message_id>``（一条 final 恰一个意图，ADR-4）；
        重复键触发唯一约束 → 整体回滚（turn 保持原状态，无半写入）。
        """
        conv_id = _require_uuid(conversation_id, "conversation_id")
        trn_id = _require_uuid(turn_id, "turn_id")
        msg_id = _coerce_uuid(message_id) or _new_uuid()
        itnt_id = _coerce_uuid(intent_id) or _new_uuid()

        async with self._sf() as sess, sess.begin():
            turn = (
                await sess.execute(
                    select(TurnModel)
                    .where(TurnModel.id == trn_id, TurnModel.tenant_id == tenant_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if turn is None:
                raise TurnNotFoundError(f"turn 不存在: turn_id={trn_id!r}")
            if turn.status != expected_status:
                raise TransitionError(
                    f"turn 完成失败: turn_id={trn_id!r} "
                    f"expected={expected_status!r} current={turn.status!r}"
                )

            allocated = (
                await sess.execute(
                    update(CanonicalConversationModel)
                    .where(
                        CanonicalConversationModel.id == conv_id,
                        CanonicalConversationModel.tenant_id == tenant_id,
                    )
                    .values(next_sequence=CanonicalConversationModel.next_sequence + 1)
                    .returning(CanonicalConversationModel.next_sequence - 1)
                )
            ).scalar_one_or_none()
            if allocated is None:
                # 覆盖「会话不存在」与「tenant 不符」，不区分泄露归属信息。
                raise NotFoundError(
                    f"canonical conversation 不存在: conversation_id={conv_id!r} "
                    f"tenant_id={tenant_id!r}"
                )

            message_row = CanonicalMessageModel(
                id=msg_id,
                tenant_id=tenant_id,
                conversation_id=conv_id,
                sequence=allocated,
                role="assistant",
                content=response_content,
                metadata_json=_to_json(message_metadata),
            )
            sess.add(message_row)
            await sess.flush()

            turn.status = "completed"
            turn.final_message_id = msg_id
            turn.updated_at = datetime.now(UTC)

            intent_row = OutboundDeliveryIntentModel(
                id=itnt_id,
                tenant_id=tenant_id,
                conversation_id=conv_id,
                message_id=msg_id,
                turn_id=trn_id,
                idempotency_key=delivery_idempotency_key or f"msg:{msg_id}",
                channel=delivery_channel,
                target_chat_id=delivery_target,
                payload_json=_to_json(delivery_payload),
                status="pending",
            )
            sess.add(intent_row)
            await sess.flush()

            return CompletionResult(
                message=_message_to_dict(message_row),
                turn=_turn_to_dict(turn),
                intent=_intent_to_dict(intent_row),
            )

    async def get_turn(
        self, tenant_id: str, turn_id: uuid.UUID | str
    ) -> dict[str, Any] | None:
        trn_id = _coerce_uuid(turn_id)
        if trn_id is None:
            return None
        async with self._sf() as sess:
            row = await sess.get(TurnModel, trn_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _turn_to_dict(row)

    async def record_tool_call(
        self,
        tenant_id: str,
        turn_id: uuid.UUID | str,
        tool_name: str,
        *,
        tool_call_id: uuid.UUID | str | None = None,
    ) -> dict[str, Any]:
        """记录一次工具调用（running 起点）；终态经 `finish_tool_call` CAS 推进。"""
        trn_id = _require_uuid(turn_id, "turn_id")
        call_id = _coerce_uuid(tool_call_id) or _new_uuid()
        async with self._sf() as sess, sess.begin():
            row = ToolCallModel(
                id=call_id,
                tenant_id=tenant_id,
                turn_id=trn_id,
                tool_name=tool_name,
                status="running",
            )
            sess.add(row)
            await sess.flush()
            return _tool_call_to_dict(row)

    async def finish_tool_call(
        self,
        tenant_id: str,
        tool_call_id: uuid.UUID | str,
        *,
        status: str,
        outcome: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """running → 终态 CAS（succeeded/failed/cancelled/unknown，§5.9.6）。"""
        call_id = _require_uuid(tool_call_id, "tool_call_id")
        if status not in {"succeeded", "failed", "cancelled", "unknown"}:
            raise ValueError(f"非法工具终态: {status!r}")
        async with self._sf() as sess, sess.begin():
            row = (
                await sess.execute(
                    select(ToolCallModel)
                    .where(ToolCallModel.id == call_id, ToolCallModel.tenant_id == tenant_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"tool call 不存在: tool_call_id={call_id!r}")
            if row.status != "running":
                raise TransitionError(
                    f"tool call 已终态: tool_call_id={call_id!r} current={row.status!r}"
                )
            row.status = status
            row.outcome_json = _to_json(outcome)
            row.finished_at = datetime.now(UTC)
            return _tool_call_to_dict(row)

    async def create_work_item(
        self,
        tenant_id: str,
        work_kind: str,
        *,
        conversation_id: uuid.UUID | str | None = None,
        idempotency_key: str | None = None,
        payload: dict[str, Any] | None = None,
        work_item_id: uuid.UUID | str | None = None,
    ) -> dict[str, Any]:
        """幂等创建后台工作项（同 idempotency_key 返回既有行）。"""
        conv_id = _coerce_uuid(conversation_id)
        async with self._sf() as sess, sess.begin():
            item_id = await _insert_work_item(
                sess,
                tenant_id,
                conv_id,
                {
                    "work_kind": work_kind,
                    "idempotency_key": idempotency_key,
                    "payload": payload,
                    "work_item_id": work_item_id,
                },
            )
            row = await sess.get(BackgroundWorkItemModel, _coerce_uuid(item_id))
            if row is None:  # 正常流程不可达：插入或既有两者必有其一。
                raise NotFoundError(f"work item 创建失败: id={item_id!r}")
            return _work_item_to_dict(row)

    async def transition_work_item(
        self,
        tenant_id: str,
        work_item_id: uuid.UUID | str,
        *,
        expected_status: str,
        new_status: str,
    ) -> dict[str, Any]:
        """CAS 推进后台工作项状态；到达终态时写 finished_at。"""
        item_id = _require_uuid(work_item_id, "work_item_id")
        async with self._sf() as sess, sess.begin():
            row = (
                await sess.execute(
                    select(BackgroundWorkItemModel)
                    .where(
                        BackgroundWorkItemModel.id == item_id,
                        BackgroundWorkItemModel.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"work item 不存在: work_item_id={item_id!r}")
            if row.status != expected_status:
                raise TransitionError(
                    f"work item 状态推进失败: work_item_id={item_id!r} "
                    f"expected={expected_status!r} current={row.status!r}"
                )
            row.status = new_status
            if new_status in _WORK_TERMINAL_STATUSES:
                row.finished_at = datetime.now(UTC)
            return _work_item_to_dict(row)


class DeliveryRepository:
    """delivery worker 侧仓储：lease 认领 / heartbeat / attempt 推进 / redrive。"""

    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    async def claim_batch(
        self,
        owner: str,
        *,
        batch_size: int = 10,
        lease_ttl_seconds: float = 60.0,
        max_attempts: int = 5,
    ) -> list[dict[str, Any]]:
        """认领到期 intent（pending/failed 到期或 stale attempting），置 attempting。

        每个被认领行 `attempt_count + 1` 并写入租约属主/到期时间；单条语句原子。
        """
        if batch_size < 1:
            raise ValueError("batch_size 必须 >= 1")
        # 单语句事务：认领必须真实提交，否则 lease 写入随 session close 回滚。
        async with self._sf() as sess, sess.begin():
            rows = (
                await sess.execute(
                    _CLAIM_SQL,
                    {
                        "owner": owner,
                        "batch_size": batch_size,
                        "lease_ttl": lease_ttl_seconds,
                        "max_attempts": max_attempts,
                    },
                )
            ).mappings().all()
            return [_intent_row_to_dict(dict(row)) for row in rows]

    async def heartbeat(
        self,
        tenant_id: str,
        intent_id: uuid.UUID | str,
        owner: str,
        *,
        lease_ttl_seconds: float = 60.0,
    ) -> bool:
        """续租；返回 False = 租约已失（他人接管），本次尝试不得再推进 sent。"""
        itnt_id = _require_uuid(intent_id, "intent_id")
        # 原生 SQL：lease 到期时间必须与 claim 的比较同源（DB 时钟，ADR-5）。
        stmt = text(
            """
            UPDATE outbound_delivery_intents
            SET lease_expires_at = now() + make_interval(secs => :lease_ttl),
                updated_at = now()
            WHERE id = :intent_id AND tenant_id = :tenant_id
              AND lease_owner = :owner AND status = 'attempting'
            """
        )
        async with self._sf() as sess, sess.begin():
            result = await sess.execute(
                stmt,
                {
                    "intent_id": itnt_id,
                    "tenant_id": tenant_id,
                    "owner": owner,
                    "lease_ttl": lease_ttl_seconds,
                },
            )
            return bool(result.rowcount)

    async def record_attempt_sent(
        self,
        tenant_id: str,
        intent_id: uuid.UUID | str,
        owner: str,
        *,
        provider_receipt: str | None = None,
    ) -> dict[str, Any]:
        """仅在租约仍归属 owner 时推进 sent（ack 驱动，ADR-5）；并追加 attempt 行。"""
        itnt_id = _require_uuid(intent_id, "intent_id")
        async with self._sf() as sess, sess.begin():
            result = await sess.execute(
                update(OutboundDeliveryIntentModel)
                .where(
                    OutboundDeliveryIntentModel.id == itnt_id,
                    OutboundDeliveryIntentModel.tenant_id == tenant_id,
                    OutboundDeliveryIntentModel.lease_owner == owner,
                    OutboundDeliveryIntentModel.status == "attempting",
                )
                .values(
                    status="sent",
                    sent_at=func.now(),
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=func.now(),
                )
            )
            if result.rowcount == 0:
                row = await sess.get(OutboundDeliveryIntentModel, itnt_id)
                if row is None or row.tenant_id != tenant_id:
                    raise DeliveryIntentNotFoundError(
                        f"投递意图不存在: intent_id={itnt_id!r}"
                    )
                raise LeaseLostError(
                    f"租约已失，不得推进 sent: intent_id={itnt_id!r} "
                    f"status={row.status!r}"
                )
            sess.add(
                DeliveryAttemptModel(
                    intent_id=itnt_id,
                    outcome="sent",
                    provider_receipt=provider_receipt,
                    finished_at=datetime.now(UTC),
                )
            )
            row = await sess.get(OutboundDeliveryIntentModel, itnt_id)
            assert row is not None  # 上一条 UPDATE 已证明行存在
            return _intent_to_dict(row)

    async def record_attempt_failed(
        self,
        tenant_id: str,
        intent_id: uuid.UUID | str,
        owner: str,
        error: str,
        *,
        max_attempts: int = 5,
        backoff_seconds: tuple[float, ...] = (60.0, 300.0, 1800.0, 7200.0, 21600.0),
    ) -> dict[str, Any]:
        """attempt 失败：追加 attempt 行、释放租约、按退避排程或进入 dead_letter。

        `attempt_count >= max_attempts` → `dead_letter`（终态，仅 redrive 可回
        pending）；否则 `failed` 且 `next_attempt_at = now() + backoff[count-1]`
        （DB 时钟，与 claim 的到期比较同源，ADR-5）。
        """
        itnt_id = _require_uuid(intent_id, "intent_id")
        async with self._sf() as sess, sess.begin():
            row = (
                await sess.execute(
                    select(OutboundDeliveryIntentModel)
                    .where(
                        OutboundDeliveryIntentModel.id == itnt_id,
                        OutboundDeliveryIntentModel.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise DeliveryIntentNotFoundError(
                    f"投递意图不存在: intent_id={itnt_id!r}"
                )
            if row.status != "attempting" or row.lease_owner != owner:
                raise LeaseLostError(
                    f"租约已失，不得记录失败: intent_id={itnt_id!r} "
                    f"status={row.status!r} owner={row.lease_owner!r}"
                )
            attempt_count = int(row.attempt_count)
            is_dead = attempt_count >= max_attempts
            db_now = (
                await sess.execute(select(func.now()))
            ).scalar_one()
            if is_dead:
                row.status = "dead_letter"
            else:
                row.status = "failed"
                idx = min(max(attempt_count - 1, 0), len(backoff_seconds) - 1)
                row.next_attempt_at = db_now + timedelta(seconds=backoff_seconds[idx])
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_error = error
            row.updated_at = db_now
            sess.add(
                DeliveryAttemptModel(
                    intent_id=itnt_id,
                    outcome="failed",
                    error=error,
                    finished_at=db_now,
                )
            )
            await sess.flush()
            return _intent_to_dict(row)

    async def redrive_dead_letter(
        self,
        tenant_id: str,
        intent_id: uuid.UUID | str,
        reason: str,
        *,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """管理员 redrive：追加处置记录（保留全部历史）并复位为 pending（ADR-6）。"""
        itnt_id = _require_uuid(intent_id, "intent_id")
        if not reason.strip():
            raise ValueError("redrive 需要非空原因")
        detail = reason if operator is None else f"[{operator}] {reason}"
        async with self._sf() as sess, sess.begin():
            row = (
                await sess.execute(
                    select(OutboundDeliveryIntentModel)
                    .where(
                        OutboundDeliveryIntentModel.id == itnt_id,
                        OutboundDeliveryIntentModel.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise DeliveryIntentNotFoundError(
                    f"投递意图不存在: intent_id={itnt_id!r}"
                )
            if row.status != "dead_letter":
                raise RedriveNotAllowedError(
                    f"仅 dead_letter 可 redrive: intent_id={itnt_id!r} "
                    f"status={row.status!r}"
                )
            sess.add(
                DeliveryAttemptModel(
                    intent_id=itnt_id,
                    outcome="redrive",
                    error=detail,
                    finished_at=datetime.now(UTC),
                )
            )
            row.status = "pending"
            row.attempt_count = 0
            row.next_attempt_at = datetime.now(UTC)
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = datetime.now(UTC)  # 显式赋值，避开 onupdate 刷新 SELECT（MissingGreenlet）
            await sess.flush()
            return _intent_to_dict(row)

    async def get_intent(
        self, tenant_id: str, intent_id: uuid.UUID | str
    ) -> dict[str, Any] | None:
        itnt_id = _coerce_uuid(intent_id)
        if itnt_id is None:
            return None
        async with self._sf() as sess:
            row = await sess.get(OutboundDeliveryIntentModel, itnt_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _intent_to_dict(row)

    async def list_intents_by_status(
        self, tenant_id: str, status: str
    ) -> list[dict[str, Any]]:
        """管理员可见查询（按租户过滤，§5.9.11 stale/dead-letter 处置入口的底座）。"""
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(OutboundDeliveryIntentModel)
                    .where(
                        OutboundDeliveryIntentModel.tenant_id == tenant_id,
                        OutboundDeliveryIntentModel.status == status,
                    )
                    .order_by(OutboundDeliveryIntentModel.created_at.asc())
                )
            ).scalars().all()
            return [_intent_to_dict(r) for r in rows]

    async def list_delivery_attempts(
        self, tenant_id: str, intent_id: uuid.UUID | str
    ) -> list[dict[str, Any]]:
        """某意图的全部 attempt/redrive 记录（只追加审计流，按时间升序）。"""
        itnt_id = _require_uuid(intent_id, "intent_id")
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(DeliveryAttemptModel)
                    .where(
                        DeliveryAttemptModel.intent_id.in_(
                            select(OutboundDeliveryIntentModel.id).where(
                                OutboundDeliveryIntentModel.id == itnt_id,
                                OutboundDeliveryIntentModel.tenant_id == tenant_id,
                            )
                        )
                    )
                    .order_by(DeliveryAttemptModel.started_at.asc(), DeliveryAttemptModel.id.asc())
                )
            ).scalars().all()
            return [_attempt_to_dict(r) for r in rows]


# ── helpers ─────────────────────────────────────────────────


def _pg_insert(model):  # noqa: ANN001, ANN202
    """PG 方言 INSERT（配合 `.on_conflict_do_nothing(...)` 实现幂等创建）。"""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    return pg_insert(model)


async def _insert_work_item(
    sess: Any,
    tenant_id: str,
    conversation_id: uuid.UUID | None,
    spec: dict[str, Any],
) -> str:
    """幂等插入一条 queued 工作项；idempotency_key 冲突时复用既有行。"""
    work_kind = spec.get("work_kind")
    if not isinstance(work_kind, str) or not work_kind:
        raise ValueError("work item 需要 work_kind")
    item_id = _coerce_uuid(spec.get("work_item_id")) or _new_uuid()
    idempotency_key = spec.get("idempotency_key")
    stmt = _pg_insert(BackgroundWorkItemModel).values(
        id=item_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        work_kind=work_kind,
        status="queued",
        idempotency_key=idempotency_key,
        payload_json=_to_json(spec.get("payload")),
    )
    if idempotency_key is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=["idempotency_key"])
        await sess.execute(stmt)
        row = (
            await sess.execute(
                select(BackgroundWorkItemModel).where(
                    BackgroundWorkItemModel.idempotency_key == idempotency_key
                )
            )
        ).scalar_one()
        if row.tenant_id != tenant_id:
            # 键全局唯一（PG 约束不分租户）：跨租户键碰撞 fail-closed，不复用他租户行。
            raise NotFoundError(
                f"work item 幂等键已属其它租户: idempotency_key={idempotency_key!r}"
            )
    else:
        await sess.execute(stmt)
        row = await sess.get(BackgroundWorkItemModel, item_id)
        if row is None:  # 正常流程不可达：刚插入必可回查。
            raise NotFoundError(f"work item 插入失败: id={item_id!r}")
    return str(row.id)


def _inbox_to_dict(row: InboxRecordModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "conversation_id": str(row.conversation_id),
        "dedup_key_id": str(row.dedup_key_id),
        "canonical_message_id": str(row.canonical_message_id),
        "status": row.status,
        "created_at": _iso(row.created_at),
        "processed_at": _iso(row.processed_at) or None,
    }


def _turn_to_dict(row: TurnModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "conversation_id": str(row.conversation_id),
        "inbox_record_id": str(row.inbox_record_id) if row.inbox_record_id else None,
        "status": row.status,
        "error": _from_json(row.error_json),
        "final_message_id": str(row.final_message_id) if row.final_message_id else None,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _message_to_dict(row: CanonicalMessageModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "conversation_id": str(row.conversation_id),
        "sequence": row.sequence,
        "role": row.role,
        "content": row.content,
        "metadata": _from_json(row.metadata_json) or {},
        "created_at": _iso(row.created_at),
    }


def _tool_call_to_dict(row: ToolCallModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "turn_id": str(row.turn_id),
        "tool_name": row.tool_name,
        "status": row.status,
        "outcome": _from_json(row.outcome_json),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at) or None,
    }


def _work_item_to_dict(row: BackgroundWorkItemModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "conversation_id": str(row.conversation_id) if row.conversation_id else None,
        "work_kind": row.work_kind,
        "status": row.status,
        "idempotency_key": row.idempotency_key,
        "payload": _from_json(row.payload_json),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "finished_at": _iso(row.finished_at) or None,
    }


def _intent_to_dict(row: OutboundDeliveryIntentModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "conversation_id": str(row.conversation_id),
        "message_id": str(row.message_id),
        "turn_id": str(row.turn_id) if row.turn_id else None,
        "idempotency_key": row.idempotency_key,
        "channel": row.channel,
        "target_chat_id": row.target_chat_id,
        "payload": _from_json(row.payload_json),
        "status": row.status,
        "attempt_count": row.attempt_count,
        "lease_owner": row.lease_owner,
        "lease_expires_at": _iso(row.lease_expires_at) or None,
        "next_attempt_at": _iso(row.next_attempt_at),
        "last_error": row.last_error,
        "sent_at": _iso(row.sent_at) or None,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _attempt_to_dict(row: DeliveryAttemptModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "intent_id": str(row.intent_id),
        "outcome": row.outcome,
        "provider_receipt": row.provider_receipt,
        "error": row.error,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at) or None,
    }


def _intent_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """claim 原生行 → intent dict（与 ORM 映射同形）。"""
    payload = row.get("payload_json")
    expires_at = row.get("lease_expires_at")
    next_attempt_at = row.get("next_attempt_at")
    sent_at = row.get("sent_at")
    created_at = row.get("created_at")
    updated_at = row.get("updated_at")
    turn_id = row.get("turn_id")
    return {
        "id": str(row["id"]),
        "tenant_id": row["tenant_id"],
        "conversation_id": str(row["conversation_id"]),
        "message_id": str(row["message_id"]),
        "turn_id": str(turn_id) if turn_id else None,
        "idempotency_key": row["idempotency_key"],
        "channel": row["channel"],
        "target_chat_id": row["target_chat_id"],
        "payload": _from_json(payload if isinstance(payload, str) else None),
        "status": row["status"],
        "attempt_count": int(row["attempt_count"]),
        "lease_owner": row["lease_owner"],
        "lease_expires_at": _iso(expires_at) if expires_at else None,
        "next_attempt_at": _iso(next_attempt_at) if next_attempt_at else None,
        "last_error": row.get("last_error"),
        "sent_at": _iso(sent_at) if sent_at else None,
        "created_at": _iso(created_at) if created_at else "",
        "updated_at": _iso(updated_at) if updated_at else "",
    }
