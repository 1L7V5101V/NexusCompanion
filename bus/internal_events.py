from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from bus.events import InboundMessage

SPAWN_COMPLETED = "spawn_completed"
_EVENT_KEY = "internal_event"
_SPAWN_KEY = "spawn"
_SPAWN_COMPLETED_CONTENT = "[internal spawn completed]"


@dataclass(frozen=True)
class SpawnCompletionEvent:
    job_id: str
    label: str
    task: str
    status: str
    exit_reason: str
    result: str


def make_spawn_completion_message(
    *,
    channel: str,
    chat_id: str,
    event: SpawnCompletionEvent,
) -> InboundMessage:
    return InboundMessage(
        channel=channel,
        sender="spawn",
        chat_id=chat_id,
        content=_SPAWN_COMPLETED_CONTENT,
        metadata={
            _EVENT_KEY: SPAWN_COMPLETED,
            _SPAWN_KEY: asdict(event),
        },
    )


def is_spawn_completion_message(msg: InboundMessage) -> bool:
    md = msg.metadata if isinstance(msg.metadata, dict) else {}
    return md.get(_EVENT_KEY) == SPAWN_COMPLETED


def parse_spawn_completion(msg: InboundMessage) -> SpawnCompletionEvent:
    md = msg.metadata if isinstance(msg.metadata, dict) else {}
    raw = md.get(_SPAWN_KEY, {}) if isinstance(md, dict) else {}
    payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return SpawnCompletionEvent(
        job_id=str(payload.get("job_id", "") or ""),
        label=str(payload.get("label", "") or ""),
        task=str(payload.get("task", "") or ""),
        status=str(payload.get("status", "") or ""),
        exit_reason=str(payload.get("exit_reason", "") or ""),
        result=str(payload.get("result", "") or ""),
    )
