from __future__ import annotations

from fastapi.testclient import TestClient

from bootstrap.dashboard_api import create_dashboard_app
from session.store import SessionStore


def _seed_workspace(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.create_session(
        key="telegram:100",
        metadata={"title": "alpha room"},
        last_consolidated=2,
        last_user_at="2026-04-19T10:00:00+08:00",
    )
    store.create_session(
        key="cli:local",
        metadata={"title": "beta room"},
        last_proactive_at="2026-04-19T09:00:00+08:00",
    )
    store.insert_message(
        "telegram:100",
        role="user",
        content="你好，今晚睡觉了吗",
        ts="2026-04-19T10:01:00+08:00",
        seq=0,
        extra={"pinned": True},
    )
    store.insert_message(
        "telegram:100",
        role="assistant",
        content="还没睡呢",
        ts="2026-04-19T10:02:00+08:00",
        seq=1,
        tool_chain=[{"text": "reply", "calls": []}],
        extra={"source": "test"},
    )
    store.insert_message(
        "cli:local",
        role="user",
        content="hello from cli",
        ts="2026-04-19T09:01:00+08:00",
        seq=0,
    )
    store.close()


def test_list_sessions_with_filters(tmp_path) -> None:
    _seed_workspace(tmp_path)
    client = TestClient(create_dashboard_app(tmp_path))

    resp = client.get(
        "/api/dashboard/sessions",
        params={"q": "alpha", "channel": "telegram"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    assert payload["items"][0]["key"] == "telegram:100"
    assert payload["items"][0]["message_count"] == 2


def test_update_and_delete_session(tmp_path) -> None:
    _seed_workspace(tmp_path)
    client = TestClient(create_dashboard_app(tmp_path))

    patch_resp = client.patch(
        "/api/dashboard/sessions/telegram:100",
        json={"metadata": {"title": "patched"}, "last_consolidated": 9},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["metadata"]["title"] == "patched"
    assert patch_resp.json()["last_consolidated"] == 9

    delete_resp = client.delete("/api/dashboard/sessions/telegram:100")
    assert delete_resp.status_code == 200

    get_resp = client.get("/api/dashboard/sessions/telegram:100")
    assert get_resp.status_code == 404


def test_list_update_and_batch_delete_messages(tmp_path) -> None:
    _seed_workspace(tmp_path)
    client = TestClient(create_dashboard_app(tmp_path))

    list_resp = client.get(
        "/api/dashboard/sessions/telegram:100/messages",
        params={"q": "睡", "role": "assistant"},
    )
    assert list_resp.status_code == 200
    payload = list_resp.json()
    assert payload["total"] == 1
    message_id = payload["items"][0]["id"]

    patch_resp = client.patch(
        f"/api/dashboard/messages/{message_id}",
        json={"content": "已经睡了", "extra": {"edited": True}},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["content"] == "已经睡了"
    assert patch_resp.json()["edited"] is True

    batch_resp = client.post(
        "/api/dashboard/messages/batch-delete",
        json={"ids": [message_id, "cli:local:0"]},
    )
    assert batch_resp.status_code == 200
    assert batch_resp.json()["deleted_count"] == 2

    remain_resp = client.get("/api/dashboard/messages", params={"session_key": "telegram:100"})
    assert remain_resp.status_code == 200
    assert remain_resp.json()["total"] == 1
