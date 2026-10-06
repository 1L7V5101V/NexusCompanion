"""C10 telegram-binding-sync 端到端验收（task 7.1–7.7）。

全链路（真实 C5 provisioning 账号 + 真实控制面事务，NEXUS_REQUIRE_PG=1）：
绑定码签发 → Telegram 侧兑换（未绑定指引/兑换成功）→ durable 入站（T1，
source 三元组幂等）→ WebChat 在线连接实时收 accepted/turn.completed 帧 →
出站 finisher T2（final + intent channel=telegram + 重放帧）→ delivery 路由
（telegram → Bot API 假件，receipt 推进 sent）→ 重连补拉连续 → REST 重建一致
→ 解绑不删历史 / 换绑不继承 → 跨账号越权 404 → 绑定码负向（过期/重放/冲突）
→ 双重唯一并发竞态。
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bootstrap.chat_api import create_chat_app
from bootstrap.db.repository.control_plane_repo import (
    DeliveryRepository,
    WebchatReplayRepository,
)
from bootstrap.db.repository.telegram_repo import (
    TelegramBindingConflictError,
    TelegramBindingRepository,
    TelegramCodeRejectedError,
)
from bootstrap.telegram_binding import TelegramBindingService
from bootstrap.telegram_durable import (
    ChannelRoutingDeliveryAdapter,
    TelegramDeliveryAdapter,
    TelegramDurableGateway,
    TelegramPilotIngress,
)
from bootstrap.webchat_durable import (
    WebchatDurableGateway,
    WebchatDurableTurnFinisher,
)
from bus.queue import MessageBus
from infra.channels.web_chat_channel import WebChatChannel, WebChatIdentity, _Connection
from tests.auth_provisioning.test_http_contract import (
    DEV_ORIGIN,
    jar,
    make_runtime,
    parse_cookies,
    seed_user_credentials,
)
from tests.test_web_chat_channel import _FakeWebSocket

pytestmark = pytest.mark.postgres


def _ws_connection(channel: WebChatChannel, identity: WebChatIdentity) -> _Connection:
    ws = _FakeWebSocket()
    conn = _Connection(  # type: ignore[assignment]
        ws,  # type: ignore[arg-type]
        uuid.uuid4().hex,
        identity=identity,
    )
    channel._connections[ws] = conn
    return conn


def _drain(conn: _Connection) -> list[dict[str, Any]]:
    """从连接的 outbound 队列取帧（测试未起 sender loop，直接读队列）。"""
    frames: list[dict[str, Any]] = []
    while not conn.outbound.empty():
        item = conn.outbound.get_nowait()
        if item is not None:
            frames.append(item[0])
    return frames


async def _identity(runtime: Any, account: dict[str, Any]) -> WebChatIdentity:
    conv = (await runtime.canonical_repo.list_conversations_by_account(account["id"]))[0]
    return WebChatIdentity(
        account_id=str(account["id"]),
        tenant_id=str(conv["tenant_id"]),
        conversation_id=str(conv["id"]),
        session_key=f"chat:{conv['tenant_id']}",
        chat_id=str(conv["tenant_id"]),
    )


class _FakeTelegramBot:
    """Bot API 假件：记录 pilot_deliver_final 调用，返回自增 message_id。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str]]] = []
        self._next_id = 100
        self.fail = False

    async def pilot_deliver_final(self, chat_id: str, content: str, *, media: list[str]):
        if self.fail:
            raise RuntimeError("bot api down")
        self.calls.append((chat_id, content, list(media)))
        self._next_id += 1
        return self._next_id


async def _body(sot_factory: Any, sot_pg_url: str, tmp_path: Any) -> None:
    runtime = make_runtime(sot_pg_url, tmp_path / "tg-e2e")
    try:
        account1, raw1 = await seed_user_credentials(runtime)
        account2, raw2 = await seed_user_credentials(runtime)
        identity1 = await _identity(runtime, account1)

        service = TelegramBindingService(sot_factory)
        gateway = TelegramDurableGateway(sot_factory, MessageBus())
        webchat = WebChatChannel()
        finisher = WebchatDurableTurnFinisher(
            sot_factory,
            channel_name="chat",
            channel_names=frozenset({"chat", "telegram"}),
        )
        finisher.bind_webchat_channel(webchat)
        gateway.bind_webchat_channel(webchat)
        ingress = TelegramPilotIngress(service, gateway)
        repo = TelegramBindingRepository(sot_factory)

        # ── 未绑定：文本被拒并给指引，零 durable 写入（spec「未绑定用户拒绝」）──
        reply = await ingress.handle_text(
            chat_id="9001", user_id="9001", username="u1",
            message_id="11", raw_text="你好", agent_text="你好",
        )
        assert reply is not None and "绑定码" in reply
        counts = await _canonical_count(sot_factory)
        assert counts == 0

        # ── 绑定码签发 + Telegram 侧兑换 ──
        issued = await service.issue_code(account_id=str(account1["id"]), issued_by="user:test")
        assert issued["code"].count("-") == 3  # 展示格式 xxxx-xxxx-xxxx-xxxx
        reply = await ingress.handle_text(
            chat_id="9001", user_id="9001", username="u1",
            message_id="12", raw_text=issued["code"], agent_text=issued["code"],
        )
        assert reply is not None and "绑定成功" in reply
        resolved = await service.resolve_identity("9001", "9001")
        assert resolved is not None
        assert str(resolved.account_id) == str(account1["id"])
        assert resolved.session_key == f"chat:{identity1.tenant_id}"

        # 已绑定身份的服务层兑换 → 明确拒绝（换绑须先解绑；设计 ADR-2）。
        issued_other = await _issue_for_account2(service, runtime, account2)
        try:
            await service.redeem(
                code_text=issued_other,
                telegram_user_id="9001",
                telegram_chat_id="9001",
            )
            raise AssertionError("已绑定身份兑换被接受")
        except TelegramBindingConflictError:
            pass
        assert await _active_binding_count(sot_factory) == 1  # 仅 9001→a1；a2 的码未兑换

        # ── durable 入站：accepted → WebChat 实时 accepted 帧 ──
        conn = _ws_connection(webchat, identity1)
        result = await ingress.handle_text(
            chat_id="9001", user_id="9001", username="u1",
            message_id="100", raw_text="来自 Telegram 的消息", agent_text="来自 Telegram 的消息",
        )
        assert result is None  # 已受理，回复来自 durable delivery
        await asyncio.sleep(0.05)  # accepted 帧实时推送为 fire-and-forget task
        frames = _drain(conn)
        accepted = [f for f in frames if f["type"] == "message.accepted"]
        assert accepted and accepted[0]["seq"] == 1  # 重放计数器 1 起步（C2 语义）
        assert accepted[0]["client_message_id"] == "tg:9001:100"
        accepted_seq = accepted[0]["seq"]

        # ── 执行出站（模拟 agent 回复）→ T2 → WebChat 实时终态帧 + intent ──
        pending = await _pending_turn(sot_factory, identity1.tenant_id)
        outbound = SimpleNamespace(
            channel="telegram",
            chat_id="9001",
            content="Telegram 侧回复",
            thinking=None,
            media=[],
            metadata={
                "nexus_pg_turn_id": str(pending["id"]),
                "nexus_pg_inbox_id": str(pending["inbox_record_id"]),
                "nexus_pg_conversation_id": identity1.conversation_id,
                "nexus_pg_tenant_id": identity1.tenant_id,
            },
        )
        await finisher._on_outbound(outbound)
        assert outbound.metadata["nexus_replay_seq"] == accepted_seq + 1
        await asyncio.sleep(0.05)  # 实时推送为 fire-and-forget task，让出调度
        frames = _drain(conn)
        terminal = [f for f in frames if f["type"] == "turn.completed"]
        assert terminal and terminal[0]["content"] == "Telegram 侧回复"
        assert terminal[0]["seq"] == accepted_seq + 1

        # intent：channel=telegram、target=chat_id（Bot API 投递语义）。
        intent = await _latest_intent(sot_factory, identity1.tenant_id)
        assert intent["channel"] == "telegram"
        assert intent["target_chat_id"] == "9001"

        # ── Telegram 重试幂等：同 source 三元组重复注入零新写入（验收第 3 条）──
        dup_result = await ingress.handle_text(
            chat_id="9001", user_id="9001", username="u1",
            message_id="100", raw_text="来自 Telegram 的消息", agent_text="来自 Telegram 的消息",
        )
        assert dup_result is None
        assert await _canonical_count(sot_factory) == 2  # user + assistant，无新增

        # ── delivery 路由：telegram intent → Bot API 假件 → sent（receipt）──
        bot = _FakeTelegramBot()

        async def _chat_route(envelope: Any) -> str:
            raise RuntimeError("webchat adapter 不应收到 telegram intent")

        router = ChannelRoutingDeliveryAdapter(
            {"chat": _chat_route, "telegram": TelegramDeliveryAdapter(sot_factory, bot)}
        )
        from bootstrap.delivery_worker import OutboundDeliveryWorker

        worker = OutboundDeliveryWorker(DeliveryRepository(sot_factory), router)
        assert await worker.process_once() == 1
        assert bot.calls and bot.calls[0] == ("9001", "Telegram 侧回复", [])
        claimed = await DeliveryRepository(sot_factory).claim_batch("x")
        assert claimed == []  # 已 sent，不被二次认领

        # ── 重连补拉：durable 帧连续（accepted + completed）无重复无乱序 ──
        wg = WebchatDurableGateway(sot_factory, MessageBus())
        replayed = await wg.replay_after(identity1, 0)
        assert replayed is not None
        assert [f["type"] for f in replayed] == ["message.accepted", "turn.completed"]
        assert [f["seq"] for f in replayed] == [accepted_seq, accepted_seq + 1]

        # ── REST 重建：双入口消息齐全、顺序 = canonical sequence（验收第 4 条）──
        app = create_chat_app(
            workspace=tmp_path,
            channel=WebChatChannel(),
            auth_runtime=runtime,
            static_root=tmp_path / "chat",
            durable_runtime=SimpleNamespace(session_factory=sot_factory),
        )
        client = TestClient(app, client=("127.0.0.1", 54321))
        cookie1 = await _exchange_cookie(client, raw1)
        rebuilt = client.get(
            f"/api/chat/sessions/chat:{identity1.tenant_id}/messages?sort_order=asc",
            headers={"cookie": cookie1},
        )
        assert rebuilt.status_code == 200
        items = rebuilt.json()["items"]
        assert [(i["role"], i["content"]) for i in items] == [
            ("user", "来自 Telegram 的消息"),
            ("assistant", "Telegram 侧回复"),
        ]

        # 跨账号负向：账号 2 请求账号 1 的会话 → 404（验收第 6 条）。
        cookie2 = await _exchange_cookie(client, raw2)
        cross = client.get(
            f"/api/chat/sessions/chat:{identity1.tenant_id}/messages",
            headers={"cookie": cookie2},
        )
        assert cross.status_code == 404

        # ── 解绑不删历史 + 换绑不继承（验收第 5 条）──
        binding = await repo.get_active_binding_by_identity("9001")
        assert binding is not None
        unbound = await service.unbind(binding_id=binding["id"], admin_actor="admin:t")
        assert unbound["status"] == "unbound"
        assert await _canonical_count(sot_factory) == 2  # 历史原样保留
        assert await service.resolve_identity("9001", "9001") is None
        # 未绑定再次发消息 → 拒绝 + 指引（不落默认租户）。
        reply = await ingress.handle_text(
            chat_id="9001", user_id="9001", username="u1",
            message_id="200", raw_text="还在吗", agent_text="还在吗",
        )
        assert reply is not None and "绑定码" in reply
        assert await _canonical_count(sot_factory) == 2

        # 换绑到账号 2：新会话从空历史开始。
        code2 = await _issue_for_account2(service, runtime, account2)
        reply = await ingress.handle_text(
            chat_id="9001", user_id="9001", username="u1",
            message_id="201", raw_text=code2, agent_text=code2,
        )
        assert reply is not None and "绑定成功" in reply
        resolved2 = await service.resolve_identity("9001", "9001")
        assert resolved2 is not None
        assert str(resolved2.account_id) == str(account2["id"])
        assert resolved2.conversation_id != identity1.conversation_id
        identity2 = await _identity(runtime, account2)
        assert await _conversation_message_count(
            sot_factory, identity2.tenant_id
        ) == 0  # 账号 2 会话为空：不继承账号 1 历史

        # ── 审计可追溯且无明文码（验收「绑定操作审计」）──
        audit_actions = await _audit_actions(sot_factory)
        assert {
            "telegram_binding.issue",
            "telegram_binding.redeem",
            "telegram_binding.unbind",
        } <= audit_actions
        assert issued["code"] not in await _audit_dump(sot_factory)
    finally:
        try:
            await runtime.aclose()
        except RuntimeError:
            pass


async def _issue_for_account2(service: TelegramBindingService, runtime: Any, account2: dict) -> str:
    """账号 2 签发并返回明文码（供「已绑定身份再兑换」与换绑场景）。"""
    issued = await service.issue_code(
        account_id=str(account2["id"]), issued_by="user:test2"
    )
    return issued["code"]


async def _canonical_count(factory: Any) -> int:
    from sqlalchemy import func, select

    from bootstrap.db.models.canonical import CanonicalMessageModel

    async with factory() as sess:
        return int(await sess.scalar(select(func.count()).select_from(CanonicalMessageModel)))


async def _conversation_message_count(factory: Any, tenant_id: str) -> int:
    from sqlalchemy import func, select

    from bootstrap.db.models.canonical import CanonicalMessageModel

    async with factory() as sess:
        return int(
            await sess.scalar(
                select(func.count())
                .select_from(CanonicalMessageModel)
                .where(CanonicalMessageModel.tenant_id == tenant_id)
            )
        )


async def _active_binding_count(factory: Any) -> int:
    from sqlalchemy import func, select

    from bootstrap.db.models.telegram import TelegramIdentityBindingModel

    async with factory() as sess:
        return int(
            await sess.scalar(
                select(func.count())
                .select_from(TelegramIdentityBindingModel)
                .where(TelegramIdentityBindingModel.status == "active")
            )
        )


async def _pending_turn(factory: Any, tenant_id: str) -> dict[str, Any]:
    from bootstrap.db.repository.control_plane_repo import TurnControlRepository

    pending = await TurnControlRepository(factory).list_non_terminal_turns(tenant_id)
    assert len(pending) == 1
    return pending[0]


async def _latest_intent(factory: Any, tenant_id: str) -> dict[str, Any]:
    from sqlalchemy import select

    from bootstrap.db.models.control_plane import OutboundDeliveryIntentModel

    async with factory() as sess:
        row = (
            await sess.execute(
                select(OutboundDeliveryIntentModel)
                .where(OutboundDeliveryIntentModel.tenant_id == tenant_id)
                .order_by(OutboundDeliveryIntentModel.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
        return {
            "id": str(row.id),
            "channel": row.channel,
            "target_chat_id": row.target_chat_id,
            "message_id": str(row.message_id),
            "tenant_id": row.tenant_id,
            "conversation_id": str(row.conversation_id),
        }


async def _exchange_cookie(client: TestClient, raw: str) -> str:
    resp = client.post(
        "/api/auth/exchange", json={"token": raw}, headers={"origin": DEV_ORIGIN}
    )
    assert resp.status_code == 200
    return jar(
        nexus_session=parse_cookies(resp.headers.get_list("set-cookie"))["nexus_session"]
    )


async def _audit_actions(factory: Any) -> set[str]:
    from sqlalchemy import select

    from bootstrap.db.models.auth import AdminAuditEventModel

    async with factory() as sess:
        rows = (
            await sess.execute(
                select(AdminAuditEventModel.action).where(
                    AdminAuditEventModel.action.like("telegram_binding.%")
                )
            )
        ).scalars().all()
        return set(rows)


async def _audit_dump(factory: Any) -> str:
    from sqlalchemy import select

    from bootstrap.db.models.auth import AdminAuditEventModel

    async with factory() as sess:
        rows = (
            await sess.execute(
                select(AdminAuditEventModel).where(
                    AdminAuditEventModel.action.like("telegram_binding.%")
                )
            )
        ).scalars().all()
        return repr([(r.actor, r.action, r.detail) for r in rows])


def test_full_sync_roundtrip(
    sot_factory: Any, sot_pg_url: str, sot_reset: Any, tmp_path: Any
) -> None:
    sot_reset()
    asyncio.run(_body(sot_factory, sot_pg_url, tmp_path))


async def _negatives_body(sot_factory: Any, sot_pg_url: str, tmp_path: Any) -> None:
    runtime = make_runtime(sot_pg_url, tmp_path / "tg-neg")
    try:
        account, _raw = await seed_user_credentials(runtime)
        service = TelegramBindingService(sot_factory)
        repo = TelegramBindingRepository(sot_factory)

        # 过期码：拒绝且不消耗。
        issued = await service.issue_code(
            account_id=str(account["id"]), issued_by="t"
        )
        from sqlalchemy import update

        from bootstrap.db.models.telegram import TelegramBindingCodeModel

        digest = await repo.find_code_by_digest(
            hashlib.sha256(
                "".join(
                    ch for ch in issued["code"].lower() if ch in "0123456789abcdef"
                ).encode()
            ).hexdigest()
        )
        assert digest is not None
        async with sot_factory() as sess, sess.begin():
            await sess.execute(
                update(TelegramBindingCodeModel)
                .where(TelegramBindingCodeModel.id == uuid.UUID(digest["id"]))
                .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
            )
        try:
            await service.redeem(
                code_text=issued["code"], telegram_user_id="777", telegram_chat_id="777"
            )
            raise AssertionError("过期码被接受")
        except TelegramCodeRejectedError:
            pass
        code_row = await repo.find_code_by_digest(
            hashlib.sha256(
                "".join(ch for ch in issued["code"].lower() if ch in "0123456789abcdef").encode()
            ).hexdigest()
        )
        assert code_row is not None and code_row["consumed_at"] is None  # 未被消费

        # 已消费码重放拒绝；账号已绑定后兑换签发于绑定前的同账号新码 →
        # 账号侧冲突且码不消耗。
        fresh = await service.issue_code(account_id=str(account["id"]), issued_by="t")
        another = await service.issue_code(account_id=str(account["id"]), issued_by="t")
        await service.redeem(
            code_text=fresh["code"], telegram_user_id="888", telegram_chat_id="888"
        )
        try:
            await service.redeem(
                code_text=fresh["code"], telegram_user_id="888", telegram_chat_id="888"
            )
            raise AssertionError("已消费码被接受")
        except TelegramCodeRejectedError:
            pass
        try:
            await service.redeem(
                code_text=another["code"], telegram_user_id="999", telegram_chat_id="999"
            )
            raise AssertionError("账号侧冲突被接受")
        except TelegramBindingConflictError:
            pass
        assert await repo.count_open_codes(account["id"]) == 1  # 冲突码未消耗

        # 并发双唯一竞态：同身份绑两账号，各只成功一个（失败即拒绝，验收第 1 条）。
        account_b, _ = await seed_user_credentials(runtime)
        results = await asyncio.gather(
            service.prebind(
                account_id=str(account["id"]), telegram_user_id="8080",
                telegram_chat_id="8080", admin_actor="admin",
            ),
            service.prebind(
                account_id=str(account_b["id"]), telegram_user_id="8080",
                telegram_chat_id="8080", admin_actor="admin",
            ),
            return_exceptions=True,
        )
        ok = [r for r in results if not isinstance(r, Exception)]
        conflict = [r for r in results if isinstance(r, TelegramBindingConflictError)]
        assert len(ok) == 1 and len(conflict) == 1
    finally:
        try:
            await runtime.aclose()
        except RuntimeError:
            pass


def test_binding_negatives_and_races(
    sot_factory: Any, sot_pg_url: str, sot_reset: Any, tmp_path: Any
) -> None:
    sot_reset()
    asyncio.run(_negatives_body(sot_factory, sot_pg_url, tmp_path))
