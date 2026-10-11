"""c18 task 3.6：static 提示块的缓存 scope 必须带租户。

落实 spec「提示块缓存不跨租户命中」。回归形状是：同一进程、同一 workspace 下
先服务租户 A 再服务租户 B，B 命中 A 留下的 static 缓存。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent.core.prompt_block import SystemPromptBuilder, TurnContext
from agent.prompting.assembler import SectionCache

if TYPE_CHECKING:
    from agent.skills import SkillsLoader
    from core.memory.markdown import MemoryProfileApi


class _CountingStaticBlock:
    """一个最小的 static 块：渲染内容里带上租户，便于看出是谁的。"""

    priority = 10
    label = "identity"
    is_static = True

    def __init__(self) -> None:
        self.render_calls = 0

    def render(
        self,
        ctx: TurnContext,
        cached_signature: str | None = None,
    ) -> str | None:
        self.render_calls += 1
        return f"identity of {ctx.tenant_id}"

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return str(ctx.workspace.expanduser().resolve())


def _ctx(workspace: Path, tenant_id: str) -> TurnContext:
    return TurnContext(
        workspace=workspace,
        memory=cast("MemoryProfileApi", Any),
        skills=cast("SkillsLoader", Any),
        skill_names=[],
        channel=None,
        chat_id=None,
        retrieved_memory_block="",
        tenant_id=tenant_id,
    )


def test_second_tenant_does_not_hit_first_tenant_cache(tmp_path: Path) -> None:
    block = _CountingStaticBlock()
    builder = SystemPromptBuilder([block], cache=SectionCache())

    first = builder.build(_ctx(tmp_path, "tenant-a"))
    second = builder.build(_ctx(tmp_path, "tenant-b"))

    assert block.render_calls == 2, "租户 B 命中了租户 A 的 static 缓存"
    assert [item.content for item in first.system_sections] == ["identity of tenant-a"]
    assert [item.content for item in second.system_sections] == ["identity of tenant-b"]
    assert [item.cache_hit for item in second.system_sections] == [False]


def test_same_tenant_still_hits_cache(tmp_path: Path) -> None:
    """加租户维度不能把缓存本身做废——同租户重复组装仍应命中。"""
    block = _CountingStaticBlock()
    builder = SystemPromptBuilder([block], cache=SectionCache())

    builder.build(_ctx(tmp_path, "tenant-a"))
    second = builder.build(_ctx(tmp_path, "tenant-a"))

    assert block.render_calls == 1
    assert [item.cache_hit for item in second.system_sections] == [True]


def test_render_without_tenant_is_not_cached(tmp_path: Path) -> None:
    """无租户身份的渲染不得进缓存：那会让所有无租户调用共用一格（ADR-11）。"""
    block = _CountingStaticBlock()
    builder = SystemPromptBuilder([block], cache=SectionCache())

    builder.build(_ctx(tmp_path, ""))
    builder.build(_ctx(tmp_path, ""))

    assert block.render_calls == 2
