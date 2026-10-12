"""c18 tasks 3.8 / 3.9：`render_sections` 与主链同源，窄协议可用单方法替身满足。

`ContextBuilder.render_sections` 是从 `PromptAssembler.assemble` 里提出来的那一步
（design ADR-15）。提出来之后主链必须**逐字不变**，否则这就是一个改了模型输入的
重构，而 C18 的意图只是让旁路复用同一段渲染。所以这里断言的是「两条路得到同
一样的东西」，不是「render_sections 输出长这样」。

顺带钉住分组的位置：`CONTEXT_FRAME_SECTIONS` 里的段名进上下文帧，其余进系统提示，
且调用方追加的 top/bottom 段走的是同一套判据——旁路正是靠这一点保持它们现有的落点
不被这次重构悄悄挪动（主动推送今天把那三段放帧里，主链放系统提示里，差异是已知的，
本 change 不统一）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.context import ContextBuilder
from agent.core.prompt_block import (
    ProfileSectionRenderer,
    SystemPromptBuildResult,
)
from agent.core.types import ContextRequest
from agent.prompting import (
    SYSTEM_CONTEXT_FRAME_MARKER,
    PromptSectionRender,
    build_context_frame_content,
    is_context_frame,
)


class _Memory:
    """够用的文件后端替身：本测试只关心段的位置与两条路一致，不关心画像内容。"""

    def read_profile(self) -> str:
        return "memory block"

    def read_self(self) -> str:
        return "self note"

    def read_recent_context(self) -> str:
        return ""

    def get_memory_context(self) -> str:
        return "memory block"


class _Skills:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def build_skills_summary(self) -> str:
        return "skill summary"

    def get_always_skills(self) -> list[str]:
        return []

    def load_skills_for_context(self, names: list[str]) -> str:
        return ""


@pytest.fixture()
def builder(monkeypatch, tmp_path: Path) -> ContextBuilder:
    monkeypatch.setattr("agent.context.SkillsLoader", _Skills)
    monkeypatch.setattr(
        "agent.context.build_agent_static_identity_prompt", lambda **_: "identity"
    )
    instance = ContextBuilder(tmp_path, _Memory())  # type: ignore[arg-type]
    return instance


def _request(**overrides: object) -> ContextRequest:
    base: dict[str, object] = {
        "history": [{"role": "user", "content": "earlier"}],
        "current_message": "now",
        "tenant_id": "tenant-a",
        # 恒带一个会落进上下文帧的段，这样每条用例里都确实存在一条帧消息可比。
        "retrieved_memory_block": "RETRIEVED",
        "message_timestamp": datetime(2026, 10, 12, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return ContextRequest(**base)  # type: ignore[arg-type]


def _frame_messages(messages: list[dict[str, object]]) -> list[str]:
    """按标记定位帧消息，不靠下标——没有帧内容时封装里压根不会有这条消息。"""
    return [
        str(message["content"])
        for message in messages
        if message.get("role") == "user" and is_context_frame(str(message["content"]))
    ]


def test_render_sections_matches_render_for_the_plain_case(builder: ContextBuilder) -> None:
    request = _request()
    sections = builder.render_sections(request)
    rendered = builder.render(request)

    assert sections.system_prompt == rendered.system_prompt
    assert [item.name for item in sections.debug_breakdown] == [
        item.name for item in rendered.debug_breakdown
    ]
    # 帧内容必须和主链那条 context frame 消息逐字一致，且只有一条。
    frame = build_context_frame_content(sections.frame_sections)
    assert _frame_messages(rendered.messages) == [frame]
    assert frame.startswith(SYSTEM_CONTEXT_FRAME_MARKER)


def test_top_and_bottom_sections_land_identically(builder: ContextBuilder) -> None:
    top = [
        PromptSectionRender(name="extra_top", content="TOP", is_static=False),
        # 帧组里的段名：调用方追加也必须被分到帧那一侧，不能因为来自 top 就进系统提示
        PromptSectionRender(name="recent_context", content="AS FRAME", is_static=False),
    ]
    bottom = [PromptSectionRender(name="extra_bottom", content="BOTTOM", is_static=False)]
    request = _request()

    sections = builder.render_sections(
        request, system_sections_top=top, system_sections_bottom=bottom
    )
    rendered = builder.render(
        request, system_sections_top=top, system_sections_bottom=bottom
    )

    assert sections.system_prompt == rendered.system_prompt
    assert "TOP" in sections.system_prompt
    assert "BOTTOM" in sections.system_prompt
    assert "AS FRAME" not in sections.system_prompt
    frame_names = {item.name for item in sections.frame_sections}
    assert "recent_context" in frame_names
    assert "AS FRAME" in build_context_frame_content(sections.frame_sections)
    # 顺序：top 在块之前、bottom 在块之后，与重构前一致。
    assert [item.name for item in sections.debug_breakdown][:1] == ["extra_top"]
    assert [item.name for item in sections.debug_breakdown][-1:] == ["extra_bottom"]


def test_disabled_sections_reach_caller_supplied_sections(builder: ContextBuilder) -> None:
    top = [
        PromptSectionRender(name="extra_top", content="TOP", is_static=False),
        PromptSectionRender(name="blocked_top", content="NOPE", is_static=False),
    ]
    request = _request(disabled_sections={"extra_top", "retrieved_memory"})
    sections = builder.render_sections(request, system_sections_top=top)

    assert "TOP" not in sections.system_prompt
    assert "NOPE" in sections.system_prompt
    assert sections.system_prompt == builder.render(request, system_sections_top=top).system_prompt


def test_render_sections_does_not_build_the_envelope(builder: ContextBuilder) -> None:
    """旁路不该被迫传 history / current_message 再扔掉封装。

    这里断言的是返回类型里没有 messages 这一项——那是 `render` 的职责。
    """
    sections = builder.render_sections(_request())
    assert not hasattr(sections, "messages")
    assert isinstance(sections, SystemPromptBuildResult)


def test_profile_section_renderer_is_runtime_checkable() -> None:
    class _Stub:
        def render_sections(
            self,
            request: ContextRequest,
            *,
            system_sections_top: list[PromptSectionRender] | None = None,
            system_sections_bottom: list[PromptSectionRender] | None = None,
        ) -> SystemPromptBuildResult:
            return SystemPromptBuildResult(
                system_sections=[], system_prompt="", debug_breakdown=[]
            )

    # 只有一个方法就能当替身：插件的单测不必为了渲染画像而搭完整 ContextBuilder。
    assert isinstance(_Stub(), ProfileSectionRenderer)


def test_context_builder_satisfies_the_protocol(builder: ContextBuilder) -> None:
    assert isinstance(builder, ProfileSectionRenderer)


def test_frame_placement_comes_from_one_declaration(builder: ContextBuilder) -> None:
    """`CONTEXT_FRAME_SECTIONS` 是唯一一处「哪些段进帧」的声明，主链与旁路共用它。

    重构前旁路各自决定放哪儿：主动推送把 self_model / long_term_memory /
    recent_context 三段都塞进帧（`plugins/proactive_flow/prompt.py:159-161`），
    而主链只把帧名单里的那几个放进去。本测试钉住主链这一侧的判据只有一份。
    """
    from agent.prompting import CONTEXT_FRAME_SECTIONS

    sections = builder.render_sections(_request(retrieved_memory_block="RETRIEVED"))
    frame_names = {item.name for item in sections.frame_sections}
    system_names = {item.name for item in sections.system_sections}

    assert "retrieved_memory" in frame_names
    assert "retrieved_memory" not in system_names
    assert frame_names <= CONTEXT_FRAME_SECTIONS
    assert not (system_names & CONTEXT_FRAME_SECTIONS)
    assert "RETRIEVED" not in sections.system_prompt
