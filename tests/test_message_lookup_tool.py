import json

import pytest

from agent.tools.message_lookup import FetchMessagesTool, SearchMessagesTool
from session.manager import SessionManager
from session.store import SessionStore


def _setup_session(store: SessionStore, key: str, n_messages: int) -> None:
    store.upsert_session(
        key,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        last_consolidated=0,
        metadata={},
    )
    roles = ["user", "assistant"]
    for seq in range(n_messages):
        store.insert_message(
            key,
            role=roles[seq % 2],
            content=f"msg-{seq}",
            ts=f"2026-01-01T00:00:{seq:02d}+00:00",
            seq=seq,
        )


@pytest.mark.asyncio
async def test_fetch_messages_returns_rows_in_input_order(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    _setup_session(store, "tg:1", 2)

    tool = FetchMessagesTool(store)
    payload = json.loads(await tool.execute(ids=["tg:1:1", "tg:1:0"]))

    assert payload["count"] == 2
    assert payload["matched_count"] == 2
    assert [m["id"] for m in payload["messages"]] == ["tg:1:1", "tg:1:0"]


@pytest.mark.asyncio
async def test_fetch_messages_with_context(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    _setup_session(store, "tg:1", 7)  # seq 0..6

    tool = FetchMessagesTool(store)
    # fetch seq=3, context=2 → expect seq 1..5
    payload = json.loads(await tool.execute(ids=["tg:1:3"], context=2))

    ids = [m["id"] for m in payload["messages"]]
    assert "tg:1:3" in ids
    assert "tg:1:1" in ids
    assert "tg:1:5" in ids
    assert "tg:1:0" not in ids
    assert "tg:1:6" not in ids
    assert payload["matched_count"] == 1
    assert payload["count"] == 5

    # in_source_ref flag: only the hit is True
    hit = next(m for m in payload["messages"] if m["id"] == "tg:1:3")
    ctx_msg = next(m for m in payload["messages"] if m["id"] == "tg:1:1")
    assert hit["in_source_ref"] is True
    assert ctx_msg["in_source_ref"] is False


@pytest.mark.asyncio
async def test_fetch_messages_context_clamps_at_seq_zero(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    _setup_session(store, "tg:1", 3)  # seq 0,1,2

    tool = FetchMessagesTool(store)
    payload = json.loads(await tool.execute(ids=["tg:1:0"], context=3))

    # context before seq 0 is clamped; should get seq 0,1,2,3 — but only 0-2 exist
    ids = [m["id"] for m in payload["messages"]]
    assert "tg:1:0" in ids
    assert "tg:1:1" in ids
    assert "tg:1:2" in ids
    assert payload["matched_count"] == 1


@pytest.mark.asyncio
async def test_search_messages_with_context(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    _setup_session(store, "tg:1", 5)  # seq 0..4; msg-2 is "msg-2"
    # override seq=2 to have a distinctive keyword
    store._conn.execute(
        "UPDATE messages SET content=? WHERE id=?", ("benchmark recall 0.62", "tg:1:2")
    )
    store._conn.execute(
        "INSERT INTO messages_fts(messages_fts) VALUES('rebuild')"
    )
    store._conn.commit()

    tool = SearchMessagesTool(store)
    payload = json.loads(await tool.execute(query="benchmark", session_key="tg:1", context=1))

    ids = [m["id"] for m in payload["messages"]]
    # hit = seq 2; context = seq 1 and seq 3
    assert "tg:1:2" in ids
    assert "tg:1:1" in ids
    assert "tg:1:3" in ids
    assert "tg:1:0" not in ids
    assert payload["matched_count"] == 1

    hit = next(m for m in payload["messages"] if m["id"] == "tg:1:2")
    assert hit["in_source_ref"] is True


@pytest.mark.asyncio
async def test_search_messages_supports_filters(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    store.upsert_session(
        "tg:1",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        last_consolidated=0,
        metadata={},
    )
    store.upsert_session(
        "tg:2",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        last_consolidated=0,
        metadata={},
    )

    store.insert_message("tg:1", role="user", content="benchmark recall 0.62", ts="2026-01-01T00:00:01+00:00", seq=0)
    store.insert_message("tg:1", role="assistant", content="benchmark done", ts="2026-01-01T00:00:02+00:00", seq=1)
    store.insert_message("tg:2", role="user", content="benchmark other", ts="2026-01-01T00:00:03+00:00", seq=0)

    tool = SearchMessagesTool(store)

    payload = json.loads(
        await tool.execute(
            query="benchmark",
            session_key="tg:1",
            role="user",
            limit=10,
        )
    )
    assert payload["count"] == 1
    assert payload["matched_count"] == 1
    assert payload["messages"][0]["session_key"] == "tg:1"
    assert payload["messages"][0]["role"] == "user"
    assert "0.62" in payload["messages"][0]["content"]


@pytest.mark.asyncio
async def test_search_messages_empty_query_returns_empty(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    tool = SearchMessagesTool(store)
    payload = json.loads(await tool.execute(query="   "))
    assert payload == {"count": 0, "matched_count": 0, "messages": []}


def test_next_seq_after_seq_zero_should_return_one(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:test")
    session.messages = [
        {
            "role": "assistant",
            "content": "prev",
            "timestamp": "2026-03-27T22:04:06+08:00",
        }
    ]
    manager.save(session)

    assert manager._store.next_seq("cli:test") == 1
