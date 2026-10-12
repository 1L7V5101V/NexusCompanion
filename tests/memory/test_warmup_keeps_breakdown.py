"""c18 task 3.7 / ADR-12：预热渲染必须吃本轮真实租户的画像快照。

`before_reasoning` 里那个 `_PromptWarmupModule` 的产物是被丢掉的（`_ = render(...)`），
它唯一的可见后果是 `ContextBuilder.last_debug_breakdown` 与租户作用域的段缓存被它写了一遍。
所以「预热有没有按本轮归属渲染」只能从这两处后果断，不能去断它返回了什么。

回归的形状：长时段有内容时 `chars > 0`，交空快照时 `chars == 0`。第二条是判据的敏感性
对照——没有它，第一条可能只是碰巧不为零。

`ContextRequest` 现在把 `tenant_id` 与 `profile_snapshot` 收成必填（task 3.7），所以
「以无租户形状预热」在类型上就写不出来：漏交归属会在构造时抛，而不是静默渲染出一份
空的画像段并把租户作用域的缓存填成空。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from agent.context import ContextBuilder
from agent.core.types import MemoryProfileSnapshot
from agent.lifecycle.phase import Phase
from agent.lifecycle.phases.before_reasoning import (
    BeforeReasoningFrame,
    default_before_reasoning_modules,
)
from agent.lifecycle.types import (
    BeforeReasoningCtx,
    BeforeReasoningInput,
    BeforeTurnCtx,
    TurnState,
)
from bus.event_bus import EventBus
from bus.events import InboundMessage

TENANT = "c18-warmup-tenant"
_LONG_TERM = "面试安排：周四下午 3 点。" * 6


def _input(profile_snapshot: MemoryProfileSnapshot) -> BeforeReasoningInput:
    msg = InboundMessage(
        channel="telegram",
        sender="user",
        chat_id="7",
        content="hello",
        timestamp=datetime(2026, 10, 12, tzinfo=timezone.utc),
        tenant_id=TENANT,
    )
    state = TurnState(msg=msg, session_key="telegram:7", dispatch_outbound=False)
    before_turn = BeforeTurnCtx(
        session_key="telegram:7",
        channel=msg.channel,
        chat_id=msg.chat_id,
        content=msg.content,
        timestamp=msg.timestamp,
        retrieved_memory_block="",
        retrieval_trace_raw=None,
        history_messages=(),
        skill_names=[],
    )
    return BeforeReasoningInput(
        state=state, before_turn=before_turn, profile_snapshot=profile_snapshot
    )


def _long_term_section(builder: ContextBuilder):
    return next(
        (
            item
            for item in builder.last_debug_breakdown
            if item.name == "long_term_memory"
        ),
        None,
    )


@pytest.mark.asyncio
async def test_warmup_breakdown_belongs_to_the_turn_tenant(tmp_path: Path) -> None:
    """预热后读到的 source breakdown 属于该轮真实租户（长时段带着快照里的字数）。"""
    builder = ContextBuilder(tmp_path)
    bus = EventBus()
    phase = Phase(
        default_before_reasoning_modules(bus, builder),
        frame_factory=BeforeReasoningFrame,
    )
    snapshot = MemoryProfileSnapshot(
        tenant_id=TENANT,
        long_term=_LONG_TERM,
        pending="",
        recent_context="",
        history="",
    )

    ctx = cast(
        BeforeReasoningCtx,
        await phase.run(_input(snapshot)),
    )

    assert ctx.tenant_id == TENANT, "归属要随 ctx 走到预热，否则预热渲染的是无租户形状"
    section = _long_term_section(builder)
    assert section is not None, "预热后根本没长出长时段"
    # 只断到「本轮快照的正文确实进了这一段」：标题文字属渲染细节，不该被钉死
    assert section.chars >= len(_LONG_TERM), (
        f"预热的长时段没吃到本轮快照：chars={section.chars}"
    )
    await bus.aclose()


@pytest.mark.asyncio
async def test_warmup_with_empty_snapshot_renders_no_long_term(tmp_path: Path) -> None:
    """判据的敏感性对照：交空快照时长时段的字数就是 0。"""
    builder = ContextBuilder(tmp_path)
    bus = EventBus()
    phase = Phase(
        default_before_reasoning_modules(bus, builder),
        frame_factory=BeforeReasoningFrame,
    )

    await phase.run(_input(MemoryProfileSnapshot.empty(TENANT)))

    # 空画像时长时段根本不进 breakdown——这条对照证明上一条断言不是碰巧不为零
    assert _long_term_section(builder) is None
    await bus.aclose()


def test_context_request_rejects_a_missing_attribution() -> None:
    """漏交归属或快照要在构造时就炸，不能静默按无租户形状渲染。"""
    from agent.core.types import ContextRequest

    with pytest.raises(ValueError, match="tenant_id"):
        ContextRequest(
            history=[],
            current_message="hi",
            tenant_id="",
            profile_snapshot=MemoryProfileSnapshot.empty(TENANT),
        )
    with pytest.raises(ValueError, match="profile_snapshot"):
        ContextRequest(
            history=[],
            current_message="hi",
            tenant_id=TENANT,
            profile_snapshot=cast(Any, None),
        )
