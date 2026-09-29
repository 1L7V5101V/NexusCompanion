"""pg-durable-sot-cutover 端到端验收（task 7.1）。

全链路（真实 C5 provisioning 账号 + 真实控制面事务，NEXUS_REQUIRE_PG=1）：
发消息 → accepted（durable seq）→ loop 出站 → finisher T2（final+intent+帧）
→ 通道在线帧 + delivery worker 投递 → **重启模拟**（全部组件重建，仅 DB 存续）
→ 重发同 id 重放原 ack（同 seq）→ 补拉连续 → REST 重建一致（含越权 404）
→ 已 sent 意图不被二次认领 → 中断 turn 由启动对账收束失败。
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bootstrap.chat_api import create_chat_app
from bootstrap.db.repository.control_plane_repo import (
    DeliveryRepository,
    IngressRepository,
    TurnControlRepository,
    WebchatReplayRepository,
)
from bootstrap.delivery_worker import OutboundDeliveryWorker
from bootstrap.webchat_durable import (
    WebchatDeliveryAdapter,
    WebchatDurableGateway,
    WebchatDurableTurnFinisher,
    reconcile_webchat_on_startup,
)
from bus.events import OutboundMessage
from bus.queue import MessageBus
from infra.channels.web_chat_channel import (
    WebChatChannel,
    WebChatIdentity,
    _Connection,
)
from tests.auth_provisioning.test_http_contract import (
    DEV_ORIGIN,
    jar,
    make_runtime,
    parse_cookies,
    seed_user_credentials,
)
from tests.test_web_chat_channel import _FakeWebSocket

pytestmark = pytest.mark.postgres


async def _identity(runtime, account: dict[str, Any]) -> WebChatIdentity:
    conv = (await runtime.canonical_repo.list_conversations_by_account(account["id"]))[0]
    return WebChatIdentity(
        account_id=str(account["id"]),
        tenant_id=str(conv["tenant_id"]),
        conversation_id=str(conv["id"]),
        session_key=f"chat:{conv['tenant_id']}",
        chat_id=str(conv["tenant_id"]),
    )


def _exchange_cookie(client: TestClient, raw: str) -> str:
    resp = client.post(
        "/api/auth/exchange", json={"token": raw}, headers={"origin": DEV_ORIGIN}
    )
    assert resp.status_code == 200
    return jar(
        nexus_session=parse_cookies(resp.headers.get_list("set-cookie"))[
            "nexus_session"
        ]
    )


class _StubSessionManager:
    def __init__(self) -> None:
        self._storage = SimpleNamespace(
            delete_session=lambda key, *, cascade: True
        )

    def _view(self, tenant_id: str) -> Any:
        return self._storage

    def get_or_create(self, tenant_id: str, key: str) -> Any:
        return SimpleNamespace(messages=[])

    async def append_messages(self, session: Any, messages: list) -> None:
        _ = messages


async def _body(sot_factory: Any, sot_pg_url: str, tmp_path: Any) -> None:
    runtime = make_runtime(sot_pg_url, tmp_path / "ws-e2e")
    try:
        # ── 账号 1（主链路）与账号 2（越权对照）──
        account1, raw1 = await seed_user_credentials(runtime)
        identity = await _identity(runtime, account1)
        gateway = WebchatDurableGateway(sot_factory, MessageBus())
        finisher = WebchatDurableTurnFinisher(sot_factory, channel_name="chat")
        channel = WebChatChannel()

        # ── 生命周期 1：T1 接受（提交后才 accepted，durable seq）──
        cmid = f"e2e-{uuid.uuid4().hex[:12]}"
        outcome = await gateway.accept_send(
            identity=identity,
            client_message_id=cmid,
            content="你好，durable",
            media=[],
            sender="webchat",
        )
        assert outcome.kind == "accepted" and outcome.frame is not None
        accepted_seq = outcome.frame["seq"]

        # ── 执行出站（模拟 agent loop 回复）→ T2 → 通道在线帧 ──
        ws = _FakeWebSocket()
        channel._connections[ws] = _Connection(  # type: ignore[assignment]
            ws,  # type: ignore[arg-type]
            uuid.uuid4().hex,
            identity=identity,
        )
        turns_repo = TurnControlRepository(sot_factory)
        pending = await turns_repo.list_non_terminal_turns(identity.tenant_id)
        assert len(pending) == 1
        outbound = OutboundMessage(
            channel="chat",
            chat_id=identity.tenant_id,
            content="durable 回复",
            metadata={
                "nexus_pg_turn_id": str(pending[0]["id"]),
                "nexus_pg_inbox_id": str(pending[0]["inbox_record_id"]),
                "nexus_pg_conversation_id": identity.conversation_id,
                "tenant_id": identity.tenant_id,
                "client_message_id": cmid,
            },
            control_turn_id=str(pending[0]["id"]),
        )
        await finisher._on_outbound(outbound)
        assert outbound.metadata["nexus_replay_seq"] == accepted_seq + 1

        await channel._on_outbound(outbound)
        conn = next(iter(channel._connections.values()))
        frames: list[dict[str, Any]] = []
        while not conn.outbound.empty():
            item = conn.outbound.get_nowait()
            if item is not None:
                frames.append(item[0])
        terminal = [f for f in frames if f["type"] == "turn.completed"]
        assert terminal and terminal[0]["seq"] == accepted_seq + 1
        assert terminal[0]["content"] == "durable 回复"

        # delivery worker 投递（在线连接），intent 推进 sent。
        worker = OutboundDeliveryWorker(
            DeliveryRepository(sot_factory),
            WebchatDeliveryAdapter(sot_factory, channel),
        )
        assert await worker.process_once() == 1

        # ── 重启模拟：全部组件重建（仅 DB 存续）──
        gateway2 = WebchatDurableGateway(sot_factory, MessageBus())

        # 重发同 id：重放原 accepted（同 seq），不产生第二条消息/turn。
        dup = await gateway2.accept_send(
            identity=identity,
            client_message_id=cmid,
            content="重发",
            media=[],
            sender="webchat",
        )
        assert dup.kind == "duplicate" and dup.frame is not None
        assert dup.frame["seq"] == accepted_seq

        # 补拉：durable 帧连续（accepted + completed），无重复无乱序。
        replayed = await gateway2.replay_after(identity, 0)
        assert replayed is not None
        assert [f["type"] for f in replayed] == [
            "message.accepted",
            "turn.completed",
        ]
        assert [f["seq"] for f in replayed] == [accepted_seq, accepted_seq + 1]

        # REST 重建：cookie 属于账号 1 → canonical 流（user+assistant）。
        account2, raw2 = await seed_user_credentials(runtime)
        app = create_chat_app(
            workspace=tmp_path,
            channel=WebChatChannel(),
            auth_runtime=runtime,
            static_root=tmp_path / "chat",
            durable_runtime=SimpleNamespace(session_factory=sot_factory),
        )
        client = TestClient(app, client=("127.0.0.1", 54321))
        cookie1 = _exchange_cookie(client, raw1)
        rebuilt = client.get(
            f"/api/chat/sessions/chat:{identity.tenant_id}/messages"
            "?sort_order=asc",
            headers={"cookie": cookie1},
        )
        assert rebuilt.status_code == 200
        items = rebuilt.json()["items"]
        assert [(i["role"], i["content"]) for i in items] == [
            ("user", "你好，durable"),
            ("assistant", "durable 回复"),
        ]
        assert [i["seq"] for i in items] == list(range(len(items)))

        # 越权：账号 2 的 cookie 请求账号 1 的会话 → 404。
        _ = account2
        cookie2 = _exchange_cookie(client, raw2)
        cross = client.get(
            f"/api/chat/sessions/chat:{identity.tenant_id}/messages",
            headers={"cookie": cookie2},
        )
        assert cross.status_code == 404

        # 已 sent 的 delivery intent 不被二次认领（不重新生成/重投）。
        intents = await DeliveryRepository(sot_factory).claim_batch("x")
        assert intents == []

        # ── 生命周期 2：中断 turn（无出站）→ 启动对账收束失败 ──
        cmid2 = f"e2e2-{uuid.uuid4().hex[:12]}"
        outcome2 = await gateway2.accept_send(
            identity=identity,
            client_message_id=cmid2,
            content="会中断的消息",
            media=[],
            sender="webchat",
        )
        assert outcome2.kind == "accepted"
        summary = await reconcile_webchat_on_startup(
            SimpleNamespace(session_factory=sot_factory),
            session_manager=_StubSessionManager(),
        )
        assert summary["reconciled_turns"] == 1
        replayed2 = await gateway2.replay_after(identity, accepted_seq + 1)
        assert replayed2 is not None
        # 第二条消息的 accepted（seq+2）+ 对账收束的 turn.failed（seq+3）。
        assert [f["type"] for f in replayed2] == [
            "message.accepted",
            "turn.failed",
        ]
        assert replayed2[-1]["seq"] == accepted_seq + 3
    finally:
        try:
            await runtime.aclose()
        except RuntimeError:
            pass


def test_full_roundtrip_with_restart(
    sot_factory: Any, sot_pg_url: str, sot_reset: Any, tmp_path: Any
) -> None:
    sot_reset()
    asyncio.run(_body(sot_factory, sot_pg_url, tmp_path))
