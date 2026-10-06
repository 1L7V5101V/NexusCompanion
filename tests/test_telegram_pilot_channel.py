"""C10 TelegramChannel Pilot 分支 / 配置 fail-fast / delivery 分发路由单元测试。

通道测试沿用 ``tests/test_channel_clients.py`` 的 telegram 模块桩注入模式：
不联网、不依赖 PG（PG 全链路见 tests/pg_sot/test_telegram_binding_e2e.py）。
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any

import pytest

from agent.config import _load_channels_config
from agent.config_models import TelegramChannelConfig
from bootstrap.telegram_durable import (
    ChannelRoutingDeliveryAdapter,
    telegram_pilot_prereq_error,
)
from tests.test_channel_clients import _import_telegram_channel

# asyncio_mode=auto（pytest.ini）：async 用例自动收集；本模块无模块级标记，
# 同步用例（配置解析/fail-fast 矩阵）保持普通函数。


class _StubIngress:
    """记录调用的桩：脚本化 resolve/handle_text 等返回值。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.resolve_result: Any = None
        self.handle_text_result: Any = None

    async def resolve(self, telegram_user_id: str, telegram_chat_id: str) -> Any:
        self.calls.append(("resolve", {"u": telegram_user_id, "c": telegram_chat_id}))
        return self.resolve_result

    async def handle_text(self, **kwargs: Any) -> str | None:
        self.calls.append(("handle_text", kwargs))
        return self.handle_text_result

    async def handle_command_start(self, **kwargs: Any) -> str | None:
        self.calls.append(("handle_command_start", kwargs))
        return "指引"

    async def media_unsupported(self, **kwargs: Any) -> str | None:
        self.calls.append(("media_unsupported", kwargs))
        return "首版暂不支持图片/文件消息，请发送文字。"


def _make_channel(tg: types.ModuleType, pilot_ingress: Any) -> Any:
    return tg.TelegramChannel(
        token="123:abc",
        bus=types.SimpleNamespace(subscribe_outbound=lambda *a, **k: None),
        session_manager=types.SimpleNamespace(workspace=None),
        pilot_ingress=pilot_ingress,
    )


def _update(
    *,
    text: str = "hi",
    chat_id: int = 9001,
    chat_type: str = "private",
    user_id: int = 42,
    message_id: int = 7,
    username: str = "tester",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        effective_message=types.SimpleNamespace(
            text=text,
            message_id=message_id,
            reply_to_message=None,
            caption=None,
            photo=[types.SimpleNamespace(file_id="f1")],
            document=types.SimpleNamespace(file_id="d1", file_name="a.txt", mime_type="text/plain"),
        ),
        effective_chat=types.SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=types.SimpleNamespace(id=user_id, username=username),
    )


@pytest.fixture()
def wired(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[tuple[str, str]]]:
    """注入 telegram 桩并 monkeypatch 发送函数，返回 (module, sent 记录)。"""
    tg = _import_telegram_channel(monkeypatch)
    sent: list[tuple[str, str]] = []

    async def _fake_send_markdown(bot: Any, chat_id: str, message: str, limiter: Any) -> None:
        sent.append((str(chat_id), message))

    async def _fake_typing(self: Any, context: Any, chat_id: int) -> None:
        sent.append((str(chat_id), "<typing>"))

    monkeypatch.setattr(tg, "send_markdown", _fake_send_markdown)
    monkeypatch.setattr(tg.TelegramChannel, "_safe_send_typing", _fake_typing)
    return tg, sent


async def test_pilot_text_bound_accepts_silently(wired: tuple[Any, list]) -> None:
    tg, sent = wired
    stub = _StubIngress()
    stub.resolve_result = types.SimpleNamespace(session_key="chat:t1")
    stub.handle_text_result = None  # accepted：回复来自 durable delivery
    channel = _make_channel(tg, stub)
    await channel._on_message(_update(), types.SimpleNamespace())
    assert stub.calls[0][0] == "handle_text"
    kwargs = stub.calls[0][1]
    assert kwargs["chat_id"] == "9001" and kwargs["message_id"] == "7"
    assert sent == [("9001", "<typing>")]  # 仅 typing 活性提示


async def test_pilot_text_unbound_gets_reply(wired: tuple[Any, list]) -> None:
    tg, sent = wired
    stub = _StubIngress()
    stub.resolve_result = None
    stub.handle_text_result = "请发送绑定码完成绑定。"
    channel = _make_channel(tg, stub)
    await channel._on_message(_update(), types.SimpleNamespace())
    assert sent == [("9001", "请发送绑定码完成绑定。")]


async def test_pilot_group_messages_ignored(wired: tuple[Any, list]) -> None:
    tg, sent = wired
    stub = _StubIngress()
    channel = _make_channel(tg, stub)
    await channel._on_message(_update(chat_type="supergroup"), types.SimpleNamespace())
    await channel._on_command(
        _update(chat_type="group", text="/start"), types.SimpleNamespace()
    )
    await channel._on_photo(_update(chat_type="group"), types.SimpleNamespace())
    await channel._on_document(_update(chat_type="group"), types.SimpleNamespace())
    assert stub.calls == [] and sent == []  # 群聊一律忽略（首版仅私聊身份）


async def test_pilot_start_command_redeem(wired: tuple[Any, list]) -> None:
    tg, sent = wired
    stub = _StubIngress()
    channel = _make_channel(tg, stub)
    await channel._on_command(_update(text="/start abcd-1234"), types.SimpleNamespace())
    assert stub.calls[0][0] == "handle_command_start"
    assert stub.calls[0][1]["code_argument"] == "abcd-1234"
    assert sent == [("9001", "指引")]


async def test_pilot_media_unsupported(wired: tuple[Any, list]) -> None:
    tg, sent = wired
    stub = _StubIngress()
    channel = _make_channel(tg, stub)
    await channel._on_photo(_update(), types.SimpleNamespace())
    assert stub.calls[0][0] == "media_unsupported"
    assert sent == [("9001", "首版暂不支持图片/文件消息，请发送文字。")]


async def test_pilot_stop_uses_bound_session_key(wired: tuple[Any, list]) -> None:
    tg, sent = wired
    stub = _StubIngress()
    stub.resolve_result = types.SimpleNamespace(session_key="chat:t1")
    captured: dict[str, Any] = {}

    class _FakeInterrupt:
        def request_interrupt(self, *, session_key: str, sender: str, command: str) -> Any:
            captured.update(session_key=session_key, sender=sender, command=command)
            return types.SimpleNamespace(message="已中断")

    channel = _make_channel(tg, stub)
    channel._interrupt_controller = _FakeInterrupt()
    await channel._on_stop_command(_update(), types.SimpleNamespace())
    assert captured == {"session_key": "chat:t1", "sender": "42", "command": "/stop"}
    assert sent == [("9001", "已中断")]


async def test_pilot_mode_skips_outbound_and_event_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tg = _import_telegram_channel(monkeypatch)
    bound: list[str] = []

    class _FakeEventBus:
        def __init__(self) -> None:
            self.subs: list[str] = []

        def on(self, evt: Any, cb: Any) -> Any:
            self.subs.append(getattr(evt, "__name__", str(evt)))
            return types.SimpleNamespace(close=lambda: None)

    eb = _FakeEventBus()
    pilot = _make_channel(tg, _StubIngress())
    pilot._bus = types.SimpleNamespace(  # type: ignore[assignment]
        subscribe_outbound=lambda name, cb: bound.append(f"out:{name}")
    )
    pilot._event_bus = eb  # type: ignore[assignment]
    pilot._bind_runtime()
    assert bound == [] and eb.subs == []  # Pilot：不订阅 outbound / 流式事件

    legacy = _make_channel(tg, None)
    legacy._bus = pilot._bus  # type: ignore[assignment]
    legacy._event_bus = eb  # type: ignore[assignment]
    legacy._bind_runtime()
    assert "out:telegram" in bound and len(eb.subs) == 4  # 旧路径行为不变


# ── 配置解析与 fail-fast 前置矩阵 ──────────────────────────────


def test_config_parses_pilot_flag() -> None:
    cfg = _load_channels_config(
        {"channels": {"telegram": {"token": "t", "pilot_identity_binding": True}}}
    )
    assert cfg.telegram is not None
    assert cfg.telegram.pilot_identity_binding is True
    default = _load_channels_config({"channels": {"telegram": {"token": "t"}}})
    assert default.telegram is not None
    assert default.telegram.pilot_identity_binding is False


def test_prereq_matrix_fail_fast() -> None:
    assert telegram_pilot_prereq_error(
        storage_backend="postgres", auth_enabled=True
    ) is None
    assert "postgres" in (
        telegram_pilot_prereq_error(storage_backend="sqlite", auth_enabled=True) or ""
    )
    assert "auth" in (
        telegram_pilot_prereq_error(storage_backend="postgres", auth_enabled=False) or ""
    )


async def test_delivery_router_unknown_channel_fails_loudly() -> None:
    class _Ok:
        async def __call__(self, envelope: Any) -> str:
            return "ok"

    router = ChannelRoutingDeliveryAdapter({"chat": _Ok()})
    with pytest.raises(RuntimeError, match="未注册 channel"):
        await router(types.SimpleNamespace(channel="qq", id="x"))


def test_config_model_default_off() -> None:
    assert TelegramChannelConfig(token="t").pilot_identity_binding is False
