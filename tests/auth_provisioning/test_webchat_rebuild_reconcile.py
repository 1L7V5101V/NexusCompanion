"""durable 重放窗口 / REST canonical 重建 / 启动对账验收（task 5.1/5.2/5.3）。

复用 C5 auth PG fixtures（真实 provisioning + 真实 AuthRuntime）：
- hello 水位与补拉窗口（含 retention 清理后的窗口外语义）；
- REST 重建分支：canonical 流为权威 + tenant 由 session 派生（越权 404）；
- 启动对账：非终态 turn → failed（不产 intent）+ turn.failed 帧 + inbox 收束
  + 派生视图重建调用。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.repository.control_plane_repo import (
    DeliveryRepository,
    IngressRepository,
    TurnControlRepository,
    WebchatReplayRepository,
)
from bootstrap.webchat_durable import (
    WebchatDurableGateway,
    reconcile_webchat_on_startup,
)
from tests.auth_provisioning.test_http_contract import (
    DEV_ORIGIN,
    jar,
    make_runtime,
    parse_cookies,
    seed_user_credentials,
)

pytestmark = pytest.mark.postgres


def _ack(client_message_id: str) -> dict[str, Any]:
    return {
        "type": "message.accepted",
        "seq": None,
        "client_message_id": client_message_id,
        "session_key": "chat:tenant",
    }


class _Identity:
    def __init__(self, account_id: str, tenant_id: str, conversation_id: str) -> None:
        self.account_id = account_id
        self.tenant_id = tenant_id
        self.conversation_id = conversation_id
        self.session_key = f"chat:{tenant_id}"
        self.chat_id = tenant_id


async def test_gateway_hello_seq_and_replay_window(
    c5_factory: async_sessionmaker, c5_pg_url, c5_reset
) -> None:
    c5_reset()
    import asyncio

    runtime = make_runtime(c5_pg_url, __import__("pathlib").Path("ws-hello"))
    try:
        await _hello_window_body(c5_factory, runtime)
    finally:
        try:
            await runtime.aclose()
        except RuntimeError:
            pass


async def _hello_window_body(c5_factory: async_sessionmaker, runtime) -> None:
    account, _raw = await seed_user_credentials(runtime)
    conversations = await runtime.canonical_repo.list_conversations_by_account(
        account["id"]
    )
    conv = conversations[0]
    identity = _Identity(
        str(account["id"]), str(conv["tenant_id"]), str(conv["id"])
    )
    gateway = WebchatDurableGateway(c5_factory, None)  # type: ignore[arg-type]
    ingress = IngressRepository(c5_factory)

    assert await gateway.hello_seq(identity) == 0  # 空会话水位 0

    cmid = f"rp-{uuid.uuid4().hex[:12]}"
    await ingress.accept_inbound(
        identity.tenant_id,
        identity.conversation_id,
        account_id=identity.account_id,
        client_message_id=cmid,
        content="问题",
        replay_frame=_ack(cmid),
    )
    assert await gateway.hello_seq(identity) == 1

    frames = await gateway.replay_after(identity, 0)
    assert frames is not None and [f["seq"] for f in frames] == [1]
    assert await gateway.replay_after(identity, 1) == []  # 已到最新
    assert await gateway.replay_after(identity, 5) is None  # 游标超前 → 窗口外

    # retention 清理后：窗口内无帧，早于水位的游标 → replay_required。
    await WebchatReplayRepository(c5_factory).delete_frames_before(
        identity.tenant_id, identity.conversation_id, 99
    )
    assert await gateway.replay_after(identity, 0) is None


async def test_rest_rebuild_serves_canonical_with_ownership(
    c5_factory: async_sessionmaker, c5_pg_url, c5_reset, tmp_path
) -> None:
    from bootstrap.chat_api import create_chat_app
    from infra.channels.web_chat_channel import WebChatChannel

    c5_reset()
    import asyncio

    runtime = make_runtime(c5_pg_url, tmp_path / "ws-rest")
    try:
        await _rest_body(c5_factory, runtime, tmp_path)
    finally:
        try:
            await runtime.aclose()
        except RuntimeError:
            pass


async def _rest_body(
    c5_factory: async_sessionmaker, runtime, tmp_path
) -> None:
    from bootstrap.chat_api import create_chat_app
    from infra.channels.web_chat_channel import WebChatChannel

    account, raw = await seed_user_credentials(runtime)
    conversations = await runtime.canonical_repo.list_conversations_by_account(
        account["id"]
    )
    conv = conversations[0]
    identity = _Identity(
        str(account["id"]), str(conv["tenant_id"]), str(conv["id"])
    )
    gateway = WebchatDurableGateway(c5_factory, None)  # type: ignore[arg-type]
    cmid = f"rb-{uuid.uuid4().hex[:12]}"
    await IngressRepository(c5_factory).accept_inbound(
        identity.tenant_id,
        identity.conversation_id,
        account_id=identity.account_id,
        client_message_id=cmid,
        content="重建我",
        replay_frame=_ack(cmid),
    )

    channel = WebChatChannel()
    app = create_chat_app(
        workspace=tmp_path,
        channel=channel,
        auth_runtime=runtime,
        static_root=tmp_path / "chat",
        # REST 分支只消费 session_factory（与 auth runtime 同库）
        durable_runtime=SimpleNamespace(session_factory=c5_factory),
    )
    client = TestClient(app, client=("127.0.0.1", 54321))
    resp = client.post(
        "/api/auth/exchange", json={"token": raw}, headers={"origin": DEV_ORIGIN}
    )
    assert resp.status_code == 200
    cookie = jar(
        nexus_session=parse_cookies(resp.headers.get_list("set-cookie"))[
            "nexus_session"
        ]
    )

    own = client.get(
        f"/api/chat/sessions/chat:{identity.tenant_id}/messages?sort_order=asc",
        headers={"cookie": cookie},
    )
    assert own.status_code == 200
    items = own.json()["items"]
    assert [i["role"] for i in items] == ["user"]
    assert items[0]["content"] == "重建我"
    assert items[0]["seq"] == 0  # canonical 0-based
    assert items[0]["created_at"]

    # 越权：请求其它 tenant 的会话（存在另一个账号的 chat:{tenant}）→ 404。
    other_account, other_raw = await seed_user_credentials(runtime)
    other_conv = (
        await runtime.canonical_repo.list_conversations_by_account(
            other_account["id"]
        )
    )[0]
    resp2 = client.post(
        "/api/auth/exchange",
        json={"token": other_raw},
        headers={"origin": DEV_ORIGIN},
    )
    cookie2 = jar(
        nexus_session=parse_cookies(resp2.headers.get_list("set-cookie"))[
            "nexus_session"
        ]
    )
    cross = client.get(
        f"/api/chat/sessions/chat:{identity.tenant_id}/messages",
        headers={"cookie": cookie2},
    )
    assert cross.status_code == 404


class _StubSessionManager:
    """记录重建调用的最小 session_manager 假件。"""

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str, bool]] = []
        self.created: list[tuple[str, str]] = []
        self.appended: list[tuple[str, list[dict[str, Any]]]] = []
        self._storage = SimpleNamespace(
            delete_session=lambda key, *, cascade: self.deleted.append(
                (key, "tenant", cascade)
            )
            or True
        )

    def _view(self, tenant_id: str) -> Any:
        return self._storage

    def get_or_create(self, tenant_id: str, key: str) -> Any:
        self.created.append((tenant_id, key))
        return SimpleNamespace(messages=[])

    async def append_messages(self, session: Any, messages: list[dict]) -> None:
        self.appended.append(("session", list(messages)))


async def test_reconcile_fails_pending_turns_and_rebuilds_view(
    c5_factory: async_sessionmaker, c5_pg_url, c5_reset
) -> None:
    c5_reset()
    import asyncio

    runtime = make_runtime(c5_pg_url, __import__("pathlib").Path("ws-rc"))
    try:
        await _reconcile_body(c5_factory, runtime)
    finally:
        try:
            await runtime.aclose()
        except RuntimeError:
            pass


async def _reconcile_body(c5_factory: async_sessionmaker, runtime) -> None:
    account, _raw = await seed_user_credentials(runtime)
    conversations = await runtime.canonical_repo.list_conversations_by_account(
        account["id"]
    )
    conv = conversations[0]
    identity = _Identity(
        str(account["id"]), str(conv["tenant_id"]), str(conv["id"])
    )
    gateway = WebchatDurableGateway(c5_factory, None)  # type: ignore[arg-type]
    ingress = IngressRepository(c5_factory)
    cmid = f"rc-{uuid.uuid4().hex[:12]}"
    accepted = await ingress.accept_inbound(
        identity.tenant_id,
        identity.conversation_id,
        account_id=identity.account_id,
        client_message_id=cmid,
        content="中断前",
        replay_frame=_ack(cmid),
    )
    assert accepted.turn_id is not None

    runtime_stub = SimpleNamespace(session_factory=c5_factory)
    stub_sm = _StubSessionManager()
    summary = await reconcile_webchat_on_startup(
        runtime_stub, session_manager=stub_sm
    )

    assert summary["reconciled_turns"] >= 1
    turns_repo = TurnControlRepository(c5_factory)
    turn = await turns_repo.get_turn(identity.tenant_id, accepted.turn_id)
    assert turn is not None and turn["status"] == "failed"
    inbox = await ingress.get_inbox(identity.tenant_id, accepted.inbox_id)
    assert inbox is not None and inbox["status"] == "processed"
    frames = await WebchatReplayRepository(c5_factory).frames_after(
        identity.tenant_id, identity.conversation_id, 0
    )
    assert [f["type"] for f in frames] == ["message.accepted", "turn.failed"]
    # 失败终态不产 intent；派生视图按 canonical 重建（session_key=chat:{tenant}）。
    assert await DeliveryRepository(c5_factory).claim_batch("other-owner") == []
    assert (identity.tenant_id, f"chat:{identity.tenant_id}") in stub_sm.created
    assert stub_sm.appended and stub_sm.appended[0][1][0]["content"] == "中断前"

    # 幂等：再次对账无待收束项。
    summary2 = await reconcile_webchat_on_startup(
        runtime_stub, session_manager=stub_sm
    )
    assert summary2["reconciled_turns"] == 0
