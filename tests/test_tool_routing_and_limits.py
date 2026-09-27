"""C7 task 3.3：message_push 绑定目标 + web 工具限流/SSRF 负向。"""

from __future__ import annotations

from typing import Any

import pytest

from agent.tools.message_push import MessagePushTool
from agent.tools.rate_limit import TenantRateLimiter
from agent.tools.web_fetch import WebFetchTool, _validate_url_target


# ── message_push：服务端绑定目标 ────────────────────────────────────


@pytest.mark.asyncio
async def test_push_denied_when_user_specified_arbitrary_target() -> None:
    """user 指定任意目标 → registry 打标 → 结构化拒绝（不静默改址）。"""
    tool = MessagePushTool()
    result = await tool.execute(
        _routing_overridden=True,
        channel="telegram",
        chat_id="tenant:acct-1",
        message="hi",
    )
    assert "push_target_not_allowed" in str(result)


def _push_capture() -> tuple[Any, list[tuple[str, str]]]:
    sent: list[tuple[str, str]] = []

    class _Lane:
        async def run_non_passive(self, channel: str, chat_id: str, fn: Any) -> str:
            _ = await fn()
            sent.append((channel, chat_id))
            return "已发送"

    return _Lane(), sent


@pytest.mark.asyncio
async def test_push_to_bound_target_allowed() -> None:
    """context 提供的绑定目标（注册过的渠道）正常推送（无 _routing_overridden）。"""
    tool = MessagePushTool()
    lane, sent = _push_capture()
    tool._chat_lane = lane  # noqa: SLF001 —— 测试直注依赖

    async def _text(chat_id: str, message: str) -> None:
        return None

    tool.register_channel("chat", text=_text)
    result = await tool.execute(
        channel="chat",
        chat_id="tenant:acct-1",
        message="hello",
    )
    assert "已发送" in str(result)
    assert sent == [("chat", "tenant:acct-1")]


# ── web_fetch：SSRF 负向矩阵 ────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://[::1]/x",
        "http://10.0.0.5/x",
        "http://192.168.1.1/router",
        "http://169.254.169.254/latest/meta-data/",
        "http://host.local/x",
        "http://svc.localhost/x",
    ],
)
def test_ssrf_targets_rejected(url: str) -> None:
    assert _validate_url_target(url) is not None


def test_public_url_allowed() -> None:
    assert _validate_url_target("https://example.com/x") is None


# ── 限流器 ──────────────────────────────────────────────────────────


def test_rate_limiter_window() -> None:
    limiter = TenantRateLimiter(max_calls=3, window_seconds=60.0)
    ts = 1000.0
    assert limiter.allow("tenant:a", "web_fetch", now=ts)
    assert limiter.allow("tenant:a", "web_fetch", now=ts + 1)
    assert limiter.allow("tenant:a", "web_fetch", now=ts + 2)
    assert not limiter.allow("tenant:a", "web_fetch", now=ts + 3)
    # 窗口滑出后恢复
    assert limiter.allow("tenant:a", "web_fetch", now=ts + 61)
    # 不同租户/工具互不影响
    assert limiter.allow("tenant:b", "web_fetch", now=ts + 3)
    assert limiter.allow("tenant:a", "web_search", now=ts + 3)


def test_rate_limiter_skips_empty_tenant() -> None:
    limiter = TenantRateLimiter(max_calls=1, window_seconds=60.0)
    assert limiter.allow("", "web_fetch")
    assert limiter.allow("", "web_fetch")
