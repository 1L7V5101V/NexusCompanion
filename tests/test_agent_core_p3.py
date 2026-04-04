from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from agent.core.context_store import DefaultContextStore
from agent.core.types import InboundMessage, ToolCall, TurnRecord
from agent.core.history_types import RetrievalTrace
from agent.retrieval.protocol import RetrievalResult
from session.manager import SessionManager


class _FakeRetrievalPipeline:
    def __init__(self) -> None:
        self.requests = []

    async def retrieve(self, request):
        self.requests.append(request)
        return RetrievalResult(
            block="MEMORY_BLOCK",
            trace=RetrievalTrace(raw={"kind": "retrieval_trace"}),
        )


class _FakePostTurnPipeline:
    def __init__(self) -> None:
        self.events = []

    def schedule(self, event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_default_context_store_prepare_reads_history_and_retrieval(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "planner"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# planner", encoding="utf-8")

    session_manager = SessionManager(tmp_path)
    session = session_manager.get_or_create("cli:1")
    session.add_message("user", "old")
    await session_manager.append_messages(session, session.messages[-1:])

    retrieval = _FakeRetrievalPipeline()
    post_turn = _FakePostTurnPipeline()
    store = DefaultContextStore(
        session_manager=session_manager,
        retrieval_pipeline=retrieval,
        post_turn_pipeline=post_turn,
        workspace=tmp_path,
    )

    bundle = await store.prepare(
        InboundMessage(
            channel="cli",
            session_key="cli:1",
            sender="u",
            content="hello $planner",
        )
    )

    assert retrieval.requests
    assert retrieval.requests[0].session_key == "cli:1"
    assert bundle.memory_blocks == ["MEMORY_BLOCK"]
    assert bundle.metadata["skill_names"] == ["planner"]
    assert bundle.history[0].content == "old"


@pytest.mark.asyncio
async def test_default_context_store_commit_persists_and_schedules_post_turn(tmp_path: Path):
    session_manager = SessionManager(tmp_path)
    retrieval = _FakeRetrievalPipeline()
    post_turn = _FakePostTurnPipeline()
    store = DefaultContextStore(
        session_manager=session_manager,
        retrieval_pipeline=retrieval,
        post_turn_pipeline=post_turn,
        workspace=tmp_path,
    )

    turn = TurnRecord(
        msg=InboundMessage(
            channel="cli",
            session_key="cli:1",
            sender="u",
            content="hello",
            timestamp=datetime.now(),
        ),
        reply="ok",
        invocations=[
            ToolCall(
                id="call_1",
                name="tool_a",
                arguments={"x": 1},
            )
        ],
        metadata={
            "tools_used": ["tool_a"],
            "tool_chain": [
                {
                    "text": "",
                    "calls": [
                        {
                            "call_id": "call_1",
                            "name": "tool_a",
                            "arguments": {"x": 1},
                            "result": "done",
                        }
                    ],
                }
            ],
        },
    )

    await store.commit(turn)

    session = session_manager.get_or_create("cli:1")
    assert len(session.messages) == 2
    assert session.messages[0]["role"] == "user"
    assert session.messages[1]["role"] == "assistant"
    assert session.metadata["last_turn_tool_calls_count"] == 1
    assert session.metadata["last_turn_had_task_tool"] is False
    assert post_turn.events
    assert post_turn.events[0].assistant_response == "ok"


@pytest.mark.asyncio
async def test_default_context_store_prepare_ignores_unknown_skill_mentions(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "known"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# known", encoding="utf-8")

    store = DefaultContextStore(
        session_manager=SessionManager(tmp_path),
        retrieval_pipeline=_FakeRetrievalPipeline(),
        post_turn_pipeline=_FakePostTurnPipeline(),
        workspace=tmp_path,
    )

    bundle = await store.prepare(
        InboundMessage(
            channel="cli",
            session_key="cli:2",
            sender="u",
            content="$known $unknown $known",
        )
    )

    assert bundle.metadata["skill_names"] == ["known"]
