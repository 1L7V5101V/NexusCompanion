"""prompt 四层组装与回退语义（tasks 3.1 / 3.2 / ADR-4）——纯单元，无 PG。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent.core.prompt_block import (
    IdentityPromptBlock,
    SelfModelPromptBlock,
    SystemPromptBuilder,
    TurnContext,
)
from agent.core.types import PersonaSnapshot
from agent.prompting.assembler import SectionCache

if TYPE_CHECKING:
    from agent.skills import SkillsLoader
    from core.memory.markdown import MemoryProfileApi


def _ctx(
    tmp_path: Path,
    snapshot: PersonaSnapshot | None,
    *,
    self_file_content: str = "单体 SELF 内容",
) -> TurnContext:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / "SELF.md").write_text(self_file_content, encoding="utf-8")

    class _Mem:
        def read_self(self) -> str:
            return (memory_dir / "SELF.md").read_text(encoding="utf-8")

    return TurnContext(
        workspace=tmp_path,
        memory=cast("MemoryProfileApi", _Mem()),
        skills=cast("SkillsLoader", Any),
        skill_names=[],
        channel="chat",
        chat_id="c1",
        retrieved_memory_block="",
        tenant_id=(snapshot.tenant_id if snapshot is not None else "tenant-test"),
        persona_snapshot=snapshot,
    )


SNAP_A = PersonaSnapshot(
    tenant_id="tenant-a",
    source="custom",
    identity="A 的身份文本",
    personality_rules="A 的规则文本",
    relationship_state="A 的关系状态",
)
SNAP_B = PersonaSnapshot(
    tenant_id="tenant-b",
    source="template",
    identity="B 的身份文本",
    personality_rules="B 的规则文本",
    relationship_state="B 的关系状态",
)


def test_identity_block_uses_snapshot_when_present(tmp_path: Path) -> None:
    block = IdentityPromptBlock()
    rendered = block.render(_ctx(tmp_path, SNAP_A))
    assert rendered is not None
    assert "A 的身份文本" in rendered
    assert "A 的规则文本" in rendered
    # 单体全局内容不得混入（跨 tenant 污染负向）。
    assert "B 的身份文本" not in rendered


def test_identity_block_falls_back_without_snapshot(tmp_path: Path) -> None:
    block = IdentityPromptBlock(render_fn=lambda workspace: "单体身份块")
    ctx = _ctx(tmp_path, None)
    assert block.render(ctx) == "单体身份块"


def test_identity_block_cached_per_tenant_not_per_workspace(tmp_path: Path) -> None:
    """跨 tenant 缓存泄漏防线。

    c18 ADR-11 之前的做法是"有快照就禁 static 缓存"——那是逐块手写防御，把任何
    一个渲染租户内容的块改成 static 就会重新泄漏。现在隔离由 cache_scope 的租户
    维度结构性保证，所以身份块可以照常被缓存，前提是不同租户不命中同一格。
    """
    builder = SystemPromptBuilder([IdentityPromptBlock()], cache=SectionCache())

    first = builder.build(_ctx(tmp_path, SNAP_A))
    second = builder.build(_ctx(tmp_path, SNAP_B))

    assert [item.cache_hit for item in second.system_sections] == [False]
    a_text = first.system_prompt
    b_text = second.system_prompt
    assert "A 的身份文本" in a_text and "A 的身份文本" not in b_text
    assert "B 的身份文本" in b_text and "B 的身份文本" not in a_text


def test_self_block_uses_snapshot_relationship_state(tmp_path: Path) -> None:
    """当前值只有 persona 快照一个来源（c18 task 6.9 / ADR-9）。

    原来这里是一对用例：有快照读快照、没快照读文件。回退那条合并成一条来源断言 +
    一条「不回退」断言——回退分支删掉之后它测的机制就不存在了。
    """
    block = SelfModelPromptBlock()
    rendered = block.render(_ctx(tmp_path, SNAP_A, self_file_content="单体 SELF 内容"))
    assert rendered is not None
    assert "A 的关系状态" in rendered
    assert "单体 SELF 内容" not in rendered  # 快照优先，不读文件


def test_self_block_without_snapshot_injects_nothing(tmp_path: Path) -> None:
    """快照为 None 时什么都不注入，不回退共享 workspace 的 SELF.md。

    这就是 spec「画像缺失不回退共享位置」在那一行上的落点：`ctx.memory` 的全仓库最后一处
    读取此前正是这条回退。
    """
    rendered = SelfModelPromptBlock().render(
        _ctx(tmp_path, None, self_file_content="单体 SELF 内容")
    )
    assert rendered is None


def test_two_tenants_render_differently(tmp_path: Path) -> None:
    """隔离验收（prompt 层）：同一段组装代码，A/B 快照产出互斥内容。"""
    block_a = IdentityPromptBlock().render(_ctx(tmp_path, SNAP_A))
    block_b = IdentityPromptBlock().render(_ctx(tmp_path, SNAP_B))
    self_a = SelfModelPromptBlock().render(_ctx(tmp_path, SNAP_A))
    self_b = SelfModelPromptBlock().render(_ctx(tmp_path, SNAP_B))
    assert block_a != block_b and self_a != self_b
    assert "B 的" not in (block_a or "") and "A 的" not in (block_b or "")


def test_snapshot_is_immutable_per_turn(tmp_path: Path) -> None:
    """生效时机（ADR-4/ADR-5）：组装期持有的快照对象在组装后不被改写——
    frozen dataclass 语义即「进行中 turn 用原 snapshot」的代码表达。"""
    import dataclasses

    assert dataclasses.is_dataclass(PersonaSnapshot)
    assert PersonaSnapshot.__dataclass_params__.frozen is True
