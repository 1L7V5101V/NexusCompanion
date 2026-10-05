"""prompt 四层组装与回退语义（tasks 3.1 / 3.2 / ADR-4）——纯单元，无 PG。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.core.prompt_block import (
    IdentityPromptBlock,
    SelfModelPromptBlock,
    TurnContext,
)
from agent.core.types import PersonaSnapshot


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
        memory=_Mem(),
        skills=Any,
        skill_names=[],
        channel="chat",
        chat_id="c1",
        retrieved_memory_block="",
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


def test_identity_block_cache_disabled_with_snapshot(tmp_path: Path) -> None:
    """跨 tenant 缓存泄漏防线：有快照 → cache_signature None（禁 static 缓存）。"""
    block = IdentityPromptBlock()
    assert block.cache_signature(_ctx(tmp_path, SNAP_A)) is None
    assert block.cache_signature(_ctx(tmp_path, None)) is not None


def test_self_block_uses_snapshot_relationship_state(tmp_path: Path) -> None:
    block = SelfModelPromptBlock()
    rendered = block.render(_ctx(tmp_path, SNAP_A, self_file_content="单体 SELF 内容"))
    assert "A 的关系状态" in rendered
    assert "单体 SELF 内容" not in rendered  # 快照优先，不读文件


def test_self_block_falls_back_to_file_without_snapshot(tmp_path: Path) -> None:
    block = SelfModelPromptBlock()
    rendered = block.render(_ctx(tmp_path, None, self_file_content="单体 SELF 内容"))
    assert "单体 SELF 内容" in rendered


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
