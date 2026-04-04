from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.core.history_types import HistoryMessage, to_tool_call_groups
from agent.postturn.protocol import PostTurnEvent
from agent.core.types import ChatMessage, ContextBundle, InboundMessage, TurnRecord
from agent.skills import SkillsLoader

if TYPE_CHECKING:
    from agent.postturn.protocol import PostTurnPipeline
    from agent.retrieval.protocol import MemoryRetrievalPipeline
    from core.observe.writer import TraceWriter
    from proactive_v2.presence import PresenceStore
    from session.manager import SessionManager


class ContextStore(ABC):
    """
    ┌──────────────────────────────────────┐
    │ ContextStore                         │
    ├──────────────────────────────────────┤
    │ 1. prepare() 准备本轮上下文          │
    │ 2. commit() 提交本轮结果             │
    └──────────────────────────────────────┘
    """

    @abstractmethod
    async def prepare(self, msg: InboundMessage) -> ContextBundle:
        """准备本轮上下文"""

    @abstractmethod
    async def commit(self, turn: TurnRecord) -> None:
        """提交本轮结果"""


class DefaultContextStore(ContextStore):
    """
    ┌──────────────────────────────────────┐
    │ DefaultContextStore                  │
    ├──────────────────────────────────────┤
    │ 1. 读取 session 历史                 │
    │ 2. 执行 retrieval                    │
    │ 3. 识别 skill mentions               │
    │ 4. 提交 session / post-turn          │
    └──────────────────────────────────────┘
    """

    def __init__(
        self,
        *,
        session_manager: "SessionManager",
        retrieval_pipeline: "MemoryRetrievalPipeline",
        post_turn_pipeline: "PostTurnPipeline",
        workspace: Path,
        presence: "PresenceStore | None" = None,
        observe_writer: "TraceWriter | None" = None,
    ) -> None:
        self._session_manager = session_manager
        self._retrieval_pipeline = retrieval_pipeline
        self._post_turn_pipeline = post_turn_pipeline
        self._skills = SkillsLoader(workspace)
        self._presence = presence
        self._observe_writer = observe_writer

    async def prepare(self, msg: InboundMessage) -> ContextBundle:
        # 1. 读取 session 历史
        session = self._session_manager.get_or_create(msg.session_key)
        history = session.get_history()
        history_messages = _to_history_messages(history)

        # 2. 内部事件允许跳过 retrieval
        if bool(msg.metadata.get("_skip_retrieval")):
            retrieval_result = None
        else:
            chat_id = _chat_id_from_session_key(msg.session_key, msg.channel)
            retrieval_result = await self._retrieval_pipeline.retrieve(
                RetrievalRequest(
                    message=msg.content,
                    session_key=msg.session_key,
                    channel=msg.channel,
                    chat_id=chat_id,
                    history=history_messages,
                    session_metadata=(
                        session.metadata if isinstance(session.metadata, dict) else {}
                    ),
                    timestamp=msg.timestamp,
                )
            )

        # 3. 提取 skill mentions
        skill_names = self._collect_skill_mentions(msg)

        # 4. 返回本轮上下文
        return ContextBundle(
            history=[
                ChatMessage(role=item.role, content=item.content)
                for item in history_messages
            ],
            memory_blocks=(
                [retrieval_result.block]
                if retrieval_result is not None and retrieval_result.block
                else []
            ),
            metadata={
                "raw_history": history,
                "skill_names": skill_names,
                "retrieved_memory_block": (
                    retrieval_result.block if retrieval_result is not None else ""
                ),
                "retrieval_raw": (
                    retrieval_result.trace.raw
                    if retrieval_result is not None and retrieval_result.trace is not None
                    else None
                ),
            },
        )

    async def commit(self, turn: TurnRecord) -> None:
        # 1. 取出 session 和 trace 元数据
        session = self._session_manager.get_or_create(turn.msg.session_key)
        tools_used = _extract_tools_used(turn)
        tool_chain = _extract_tool_chain(turn)
        persisted_user_content = str(
            turn.msg.metadata.get("_persist_user_content", turn.msg.content) or ""
        )

        # 2. 落 session 消息
        if self._presence is not None:
            self._presence.record_user_message(session.key)
        session.add_message(
            "user",
            persisted_user_content,
            media=turn.msg.media if turn.msg.media else None,
        )
        session.add_message(
            "assistant",
            turn.reply,
            tools_used=tools_used if tools_used else None,
            tool_chain=tool_chain if tool_chain else None,
        )
        _update_session_runtime_metadata(
            session,
            tools_used=tools_used,
            tool_chain=tool_chain,
        )
        await self._session_manager.append_messages(session, session.messages[-2:])

        # 3. 发送 post-turn 后台任务
        self._post_turn_pipeline.schedule(
            PostTurnEvent(
                session_key=turn.msg.session_key,
                channel=turn.msg.channel,
                chat_id=_chat_id_from_session_key(turn.msg.session_key, turn.msg.channel),
                user_message=persisted_user_content,
                assistant_response=turn.reply,
                tools_used=tools_used,
                tool_chain=to_tool_call_groups(tool_chain),
                session=session,
                timestamp=turn.msg.timestamp,
                extra=(
                    {"skip_post_memory": True}
                    if turn.msg.metadata.get("skip_post_memory")
                    else {}
                ),
            )
        )

        # 4. 写 turn trace
        self._emit_observe_trace(turn, tool_chain)

    def _collect_skill_mentions(self, msg: InboundMessage) -> list[str]:
        # 1. 提取 $skill 提及
        raw_names = re.findall(r"\$([a-zA-Z0-9_-]+)", msg.content)
        if not raw_names:
            return []

        # 2. 与可用技能集合求交
        available = {
            item["name"]
            for item in self._skills.list_skills(
                filter_unavailable=False
            )
        }

        # 3. 保持顺序去重
        names: list[str] = []
        seen: set[str] = set()
        for name in raw_names:
            if name in available and name not in seen:
                seen.add(name)
                names.append(name)
        return names

    def _emit_observe_trace(self, turn: TurnRecord, tool_chain: list[dict]) -> None:
        # 1. 没有 observe writer 时直接跳过
        if self._observe_writer is None:
            return

        # 2. 组装精简 tool calls
        tool_calls = [
            {
                "name": call.get("name", ""),
                "args": str(call.get("arguments", ""))[:300],
                "result": str(call.get("result", ""))[:500],
            }
            for group in tool_chain
            for call in (group.get("calls") or [])
        ]

        # 3. 发出 turn trace
        from core.observe.events import TurnTrace as TurnTraceEvent

        self._observe_writer.emit(
            TurnTraceEvent(
                source="agent",
                session_key=turn.msg.session_key,
                user_msg=turn.msg.content,
                llm_output=turn.reply,
                raw_llm_output=turn.reply,
                tool_calls=tool_calls,
                tool_chain_json=(
                    json.dumps(_slim_tool_chain(tool_chain), ensure_ascii=False)
                    if tool_chain
                    else None
                ),
            )
        )
        retrieval_raw = turn.metadata.get("retrieval_raw")
        if retrieval_raw is not None:
            self._observe_writer.emit(retrieval_raw)


def _slim_tool_chain(chain: list[dict]) -> list[dict]:
    out: list[dict] = []
    for group in chain:
        out.append(
            {
                "text": str(group.get("text") or ""),
                "calls": [
                    {
                        "name": call.get("name", ""),
                        "args": str(call.get("arguments", ""))[:800],
                        "result": str(call.get("result", ""))[:1200],
                    }
                    for call in (group.get("calls") or [])
                    if isinstance(call, dict)
                ],
            }
        )
    return out


def _extract_tools_used(turn: TurnRecord) -> list[str]:
    raw = turn.metadata.get("tools_used")
    if isinstance(raw, list):
        return [str(item) for item in raw if isinstance(item, str)]
    return [call.name for call in turn.invocations if call.name]


def _extract_tool_chain(turn: TurnRecord) -> list[dict]:
    raw = turn.metadata.get("tool_chain")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not turn.invocations:
        return []
    return [
        {
            "text": "",
            "calls": [
                {
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "result": "",
                }
                for call in turn.invocations
            ],
        }
    ]


def _chat_id_from_session_key(session_key: str, channel: str) -> str:
    prefix = f"{channel}:"
    if session_key.startswith(prefix):
        return session_key[len(prefix) :]
    if ":" in session_key:
        return session_key.split(":", 1)[1]
    return session_key


def _to_history_messages(history: list[dict[str, Any]]) -> list[HistoryMessage]:
    out: list[HistoryMessage] = []
    for item in history:
        raw_tools_used = item.get("tools_used") or []
        out.append(
            HistoryMessage(
                role=str(item.get("role", "") or ""),
                content=_stringify_content(item.get("content", "")),
                tools_used=[
                    str(name) for name in raw_tools_used if isinstance(name, str)
                ],
                tool_chain=to_tool_call_groups(item.get("tool_chain") or []),
            )
        )
    return out


def _stringify_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "") or ""))
        return "\n".join(part for part in parts if part).strip()
    return str(content or "")


def _extract_task_tools(tools_used: list[str]) -> list[str]:
    task_tools = []
    for name in tools_used:
        if name in {"task_note", "update_now"}:
            task_tools.append(name)
    return task_tools


def _update_session_runtime_metadata(
    session: object,
    *,
    tools_used: list[str],
    tool_chain: list[dict],
) -> None:
    md = session.metadata if isinstance(session.metadata, dict) else {}  # type: ignore[union-attr]
    call_count = 0
    for group in tool_chain:
        if not isinstance(group, dict):
            continue
        calls = group.get("calls") or []
        if isinstance(calls, list):
            call_count += len(calls)

    turn_task_tools = _extract_task_tools(tools_used)
    turns = md.get("_task_tools_turns")
    if not isinstance(turns, list):
        turns = []
    turns.append(turn_task_tools)
    turns = turns[-2:]

    flat_recent = []
    seen: set[str] = set()
    for turn in turns:
        if not isinstance(turn, list):
            continue
        for name in turn:
            if isinstance(name, str) and name not in seen:
                seen.add(name)
                flat_recent.append(name)

    md["last_turn_tool_calls_count"] = call_count
    md["recent_task_tools"] = flat_recent
    md["last_turn_had_task_tool"] = bool(turn_task_tools)
    md["last_turn_ts"] = datetime.now().astimezone().isoformat()
    md["_task_tools_turns"] = turns
    session.metadata = md  # type: ignore[union-attr]


from agent.retrieval.protocol import RetrievalRequest
