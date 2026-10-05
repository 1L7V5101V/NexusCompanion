from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agent.core.types import PersonaSnapshot
from agent.prompting import PromptSectionMeta, PromptSectionRender, SectionCache
from prompts.agent import (
    build_agent_behavior_rules_prompt,
    build_agent_session_context_prompt,
    build_agent_static_identity_prompt,
    build_skills_catalog_prompt,
)

if TYPE_CHECKING:
    from agent.skills import SkillsLoader
    from core.memory.markdown import MemoryProfileApi

logger = logging.getLogger("agent.core.prompt_block")


@dataclass
class TurnContext:
    workspace: Path
    memory: "MemoryProfileApi"
    skills: "SkillsLoader"
    skill_names: list[str]
    channel: str | None
    chat_id: str | None
    retrieved_memory_block: str
    persona_snapshot: PersonaSnapshot | None = None


class PromptBlock(Protocol):
    priority: int
    label: str
    is_static: bool

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None: ...

    def cache_signature(self, ctx: TurnContext) -> str | None: ...


# ─── Prompt Block 渲染顺序（priority 升序 = system prompt 拼接顺序）────────────
#  10 IdentityPromptBlock      → build_agent_static_identity_prompt(workspace)
#                              来源：工作区路径、memory/* 文件索引
#                              时机：仅 workspace 变化时才变，最稳定
#  15 BehaviorRulesPromptBlock → build_agent_behavior_rules_prompt(workspace)
#                              来源：prompts/agent.py 里的固定行为规范
#                              时机：仅代码或 workspace 变化时才变，最稳定
#  20 SkillsCatalogPromptBlock → skills.build_skills_summary()
#                              来源：skills/ 目录扫描结果、技能描述、依赖可用性
#                              时机：技能文件或环境依赖变化时才变，低频
#  30 SelfModelPromptBlock     → memory/SELF.md
#                              来源：memory.read_self()
#                              时机：自我认知被写回时才变，低频
#  35 LongTermMemoryPromptBlock→ memory/MEMORY.md
#                              来源：memory.read_profile() / get_memory_context()
#                              时机：长期记忆 consolidate 或人工更新时才变，低频
#  40 SessionContextPromptBlock→ 环境 + 当前 session
#                              来源：platform.machine() + channel + chat_id
#                              时机：切换机器架构、channel、chat_id 时才变；同 session 基本稳定
#  45 RecentContextPromptBlock → memory/RECENT_CONTEXT.md（裁掉 Recent Turns）
#                              来源：memory.read_recent_context()
#                              时机：近期语境压缩摘要更新时变化；每轮 Recent Turns 刷新不会直接进入这里
#  50 ActiveSkillsPromptBlock  → active skill 内容
#                              来源：always skills + 本轮命中的 skill_names
#                              时机：本轮技能命中集合变化时就会变，中频
#  55 MemoryBlockPromptBlock   → 本轮语义检索注入
#                              来源：retrieved_memory_block
#                              时机：每轮 retrieval 结果都可能不同，最高频
# ─────────────────────────────────────────────────────────────────────────────
def _render_persona_identity(ctx: "TurnContext") -> str | None:
    """tenant PersonaProfile 快照 → 身份块（identity + personality_rules）。

    沿用单体 build_agent_static_identity_prompt 的段落形状，正文换成 tenant
    快照；来源标签 persona_profile 供 source breakdown 识别（ADR-4/ADR-6）。
    """
    snap = ctx.persona_snapshot
    if snap is None:
        return None
    name = snap.identity.split("\n", 1)[0][:32] or "Nexus"
    return (
        f"# {name}\n\n"
        f"{snap.identity}\n\n"
        f"## Personality\n\n{snap.personality_rules}"
    )


class IdentityPromptBlock:
    priority = 10
    label = "identity"
    is_static = True

    def __init__(
        self,
        render_fn=build_agent_static_identity_prompt,
        persona_render_fn=None,
    ) -> None:
        # persona_render_fn: tenant 快照下的身份块渲染（c9-persona-relationship
        # ADR-4）；缺省延后解析以避免硬依赖。
        self._persona_render_fn = persona_render_fn or _render_persona_identity
        self._render_fn = render_fn

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        if ctx.persona_snapshot is not None:
            return self._persona_render_fn(ctx)
        return self._render_fn(workspace=ctx.workspace)

    def cache_signature(self, ctx: TurnContext) -> str | None:
        # 快照存在时禁用 static 缓存：不同 tenant 的快照内容不同，而缓存
        # scope 是进程级 workspace 路径——缓存会跨 tenant 泄漏（fail-closed）。
        if ctx.persona_snapshot is not None:
            return None
        return str(ctx.workspace.expanduser().resolve())


class BehaviorRulesPromptBlock:
    priority = 15
    label = "behavior_rules"
    is_static = True

    def __init__(self, render_fn=build_agent_behavior_rules_prompt) -> None:
        self._render_fn = render_fn

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        return self._render_fn(workspace=ctx.workspace)

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return str(ctx.workspace.expanduser().resolve())


class SkillsCatalogPromptBlock:
    priority = 20
    label = "skills_catalog"
    is_static = True

    def __init__(self, render_fn=build_skills_catalog_prompt) -> None:
        self._render_fn = render_fn

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        summary = cached_signature or ""
        if not summary:
            return None
        return self._render_fn(summary)

    def cache_signature(self, ctx: TurnContext) -> str | None:
        summary = ctx.skills.build_skills_summary()
        return summary or None


class SelfModelPromptBlock:
    priority = 30
    label = "self_model"
    is_static = False

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        # C9：tenant 快照优先（RelationshipState 当前值来自 PG seam）；无快照
        # 回退单体文件语义（行为不变）。
        if ctx.persona_snapshot is not None:
            self_content = ctx.persona_snapshot.relationship_state
            name = ctx.persona_snapshot.identity.split("\n", 1)[0][:32] or "Nexus"
            if not self_content:
                return None
            return f"## {name} Self-Perception\n\n{self_content}"
        self_content = ctx.memory.read_self()
        if not self_content:
            return None
        from agent.persona import get_identity_name
        name = get_identity_name()
        return f"## {name} Self-Perception\n\n{self_content}"

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


class LongTermMemoryPromptBlock:
    priority = 35
    label = "long_term_memory"
    is_static = False

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        memory = ctx.memory.get_memory_context()
        return str(memory).strip() if memory else None

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


class SessionContextPromptBlock:
    priority = 40
    label = "session_context"
    is_static = False

    def __init__(self, render_fn=build_agent_session_context_prompt) -> None:
        self._render_fn = render_fn

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        return self._render_fn(
            channel=ctx.channel,
            chat_id=ctx.chat_id,
        )

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


class RecentContextPromptBlock:
    priority = 45
    label = "recent_context"
    is_static = False

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        content = ctx.memory.read_recent_context()
        if not content:
            return None
        # Strip ## Recent Turns section — it mirrors the sliding window and causes overlap.
        marker = "\n## Recent Turns"
        cut = content.find(marker)
        trimmed = content[:cut].strip() if cut != -1 else content.strip()
        return trimmed if trimmed else None

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


class ActiveSkillsPromptBlock:
    priority = 50
    label = "active_skills"
    is_static = False

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        always_skills = ctx.skills.get_always_skills()
        names: list[str] = []
        seen: set[str] = set()
        for name in [*always_skills, *ctx.skill_names]:
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
        if not names:
            return None
        content = ctx.skills.load_skills_for_context(names)
        if not content:
            return None
        return f"# Active Skills\n\n{content}"

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


class MemoryBlockPromptBlock:
    priority = 55
    label = "retrieved_memory"
    is_static = False

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        block = (ctx.retrieved_memory_block or "").strip()
        return block or None

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


@dataclass
class SystemPromptBuildResult:
    system_sections: list[PromptSectionRender]
    system_prompt: str
    debug_breakdown: list[PromptSectionMeta]


class SystemPromptBuilder:
    """
    ┌──────────────────────────────────────┐
    │ SystemPromptBuilder                  │
    ├──────────────────────────────────────┤
    │ 1. 按 priority 遍历 prompt blocks    │
    │ 2. 读取 static block cache           │
    │ 3. 渲染启用的 blocks                 │
    │ 4. 汇总 system prompt               │
    └──────────────────────────────────────┘
    """

    def __init__(
        self,
        blocks: list[PromptBlock],
        cache: SectionCache | None = None,
    ) -> None:
        self._blocks = sorted(blocks, key=lambda block: block.priority)
        self._cache = cache or SectionCache()

    def build(
        self,
        ctx: TurnContext,
        *,
        disabled_sections: set[str] | None = None,
    ) -> SystemPromptBuildResult:
        # 1. 先准备输出容器和禁用集合。
        renders: list[PromptSectionRender] = []
        breakdown: list[PromptSectionMeta] = []
        disabled = disabled_sections or set()
        cache_scope = str(ctx.workspace.expanduser().resolve())

        # 2. 再逐个渲染 prompt block。
        for block in self._blocks:
            if block.label in disabled:
                continue
            cache_hit = False
            rendered: str | None = None
            signature = block.cache_signature(ctx) if block.is_static else None

            # 3. static block 先查缓存，避免重复读文件或重复构造。
            if signature:
                rendered = self._cache.get(cache_scope, block.label, signature)
                cache_hit = rendered is not None
            if rendered is None:
                rendered = block.render(ctx, cached_signature=signature)
                if rendered and signature:
                    self._cache.set(cache_scope, block.label, signature, rendered)

            # 4. 最后只收录真正有内容的 block。
            if rendered:
                renders.append(
                    PromptSectionRender(
                        name=block.label,
                        content=rendered,
                        is_static=block.is_static,
                        cache_hit=cache_hit,
                    )
                )
                breakdown.append(
                    PromptSectionMeta(
                        name=block.label,
                        chars=len(rendered),
                        est_tokens=max(1, len(rendered) // 3),
                        is_static=block.is_static,
                        cache_hit=cache_hit,
                    )
                )

        return SystemPromptBuildResult(
            system_sections=renders,
            system_prompt="\n\n---\n\n".join(item.content for item in renders),
            debug_breakdown=breakdown,
        )
