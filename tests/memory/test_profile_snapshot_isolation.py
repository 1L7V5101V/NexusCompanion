"""c18 task 2.3：画像块只读本轮租户快照，不得回退共享目录。

落实 spec「新租户开场白不引用他人话题」与「画像缺失不回退共享位置」。纯单元，无 PG。

接线前预期为红，且红在两处该红的地方：`agent.core.types` 还没有
`MemoryProfileSnapshot`（task 3.1），`TurnContext` 还没有 `tenant_id` 与
`profile_snapshot` 字段（task 3.3）。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent.core.prompt_block import (
    LongTermMemoryPromptBlock,
    RecentContextPromptBlock,
    TurnContext,
)
from agent.core.types import MemoryProfileSnapshot

if TYPE_CHECKING:
    from agent.skills import SkillsLoader
    from core.memory.markdown import MemoryProfileApi

# 别的身份留在共享目录里的内容片段。
CANARY = "硅的光子挠曲电效应"
OTHER_TENANT = "tenant-owner"
NEW_TENANT = "tenant-newcomer"


def _shared_store_dir(tmp_path: Path) -> Path:
    """造出泄漏面：进程级共享 workspace 的 memory 目录里躺着别人的内容。"""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / "MEMORY.md").write_text(
        f"## 用户事实\n- {CANARY}", encoding="utf-8"
    )
    (memory_dir / "RECENT_CONTEXT.md").write_text(
        f"## Compression\n- 待延续话题：{CANARY}\n\n## Recent Turns\n- 无关内容",
        encoding="utf-8",
    )
    return memory_dir


def _ctx(tmp_path: Path, profile: MemoryProfileSnapshot) -> TurnContext:
    memory_dir = _shared_store_dir(tmp_path)

    class _Mem:
        """全局 store 视角：它看见的就是上面那些别人的内容。"""

        def get_memory_context(self) -> str:
            return (memory_dir / "MEMORY.md").read_text(encoding="utf-8")

        def read_recent_context(self) -> str:
            return (memory_dir / "RECENT_CONTEXT.md").read_text(encoding="utf-8")

    return TurnContext(
        workspace=tmp_path,
        skills=cast("SkillsLoader", Any),
        skill_names=[],
        channel="chat",
        chat_id="c1",
        retrieved_memory_block="",
        tenant_id=profile.tenant_id,
        profile_snapshot=profile,
    )


def test_newcomer_prompt_blocks_do_not_leak_shared_owner_content(
    tmp_path: Path,
) -> None:
    """新租户画像为空 → 两个画像块都不得把共享目录里别人的内容注入。"""
    ctx = _ctx(tmp_path, MemoryProfileSnapshot.empty(NEW_TENANT))

    rendered = [
        block.render(ctx)
        for block in (LongTermMemoryPromptBlock(), RecentContextPromptBlock())
    ]
    leaked = [text for text in rendered if text and CANARY in text]
    assert not leaked, f"共享目录内容进入了新租户的提示：{leaked}"


def test_empty_profile_renders_nothing_rather_than_falling_back(
    tmp_path: Path,
) -> None:
    """画像缺失是空语义，不是「查不到就改读共享目录」。"""
    ctx = _ctx(tmp_path, MemoryProfileSnapshot.empty(NEW_TENANT))

    assert LongTermMemoryPromptBlock().render(ctx) is None
    assert RecentContextPromptBlock().render(ctx) is None


def test_turn_context_requires_tenant_id(tmp_path: Path) -> None:
    """缺租户身份必须构造失败，不回退默认租户（task 3.3）。"""
    import dataclasses

    import pytest

    from agent.core.prompt_block import TurnContext as _TC

    fields = dataclasses.fields(_TC)
    names = [field.name for field in fields]
    assert "tenant_id" in names
    idx = names.index("tenant_id")
    assert all(
        field.default is dataclasses.MISSING
        for field in fields[: idx + 1]
    ), "tenant_id 之前（含自身）出现带默认值的字段，缺失即可静默兜底"

    with pytest.raises(TypeError):
        _TC(  # pyright: ignore[reportCallIssue] 本用例断言的就是缺 tenant_id 时构造失败
            workspace=tmp_path,
            skills=cast("SkillsLoader", Any),
            skill_names=[],
            channel="chat",
            chat_id="c1",
            retrieved_memory_block="",
        )


def test_tenant_snapshot_renders_only_its_own_content(tmp_path: Path) -> None:
    """有快照时注入的是该租户自己的画像，且与共享目录内容无关。"""
    own = MemoryProfileSnapshot(
        tenant_id=OTHER_TENANT,
        long_term="属于 owner 的长期画像",
        pending="",
        recent_context="属于 owner 的近期摘要",
        history="",
    )
    ctx = _ctx(tmp_path, own)

    assert LongTermMemoryPromptBlock().render(ctx) == (
        "## Long-term Memory\n属于 owner 的长期画像"
    )
    recent = RecentContextPromptBlock().render(ctx)
    assert recent is not None
    assert "属于 owner 的近期摘要" in recent
    assert CANARY not in recent
