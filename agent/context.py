import base64
import logging
import mimetypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from agent.memes.catalog import MemeCatalog
from agent.prompting import (
    PromptAssembler,
    PromptSectionMeta,
    PromptSectionRender,
    SectionCache,
    build_runtime_guard_message,
    build_system_context_message,
)
from agent.skills import SkillsLoader
from prompts.agent import (
    build_agent_environment_prompt,
    build_agent_request_time_prompt,
    build_agent_static_identity_prompt,
    build_current_session_prompt,
    build_skills_catalog_prompt,
    build_sop_index_prompt,
    build_telegram_rendering_prompt,
)

if TYPE_CHECKING:
    from core.memory.port import MemoryPort

logger = logging.getLogger("agent.context")


@dataclass
class TurnContext:
    workspace: Path
    memory: "MemoryPort"
    skills: SkillsLoader
    skill_names: list[str]
    message_timestamp: datetime | None
    retrieved_memory_block: str


class ContextSection(Protocol):
    priority: int
    label: str
    is_static: bool

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None: ...

    def cache_signature(self, ctx: TurnContext) -> str | None: ...


class StaticIdentitySection:
    priority = 10
    label = "identity"
    is_static = True

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        return build_agent_static_identity_prompt(workspace=ctx.workspace)

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return str(ctx.workspace.expanduser().resolve())


class MemoryBlockSection:
    priority = 20
    label = "retrieved_memory"
    is_static = False

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        block = (ctx.retrieved_memory_block or "").strip()
        return block or None

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


class LongTermMemorySection:
    priority = 30
    label = "long_term_memory"
    is_static = False

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        memory = ctx.memory.get_memory_context()
        return str(memory).strip() if memory else None

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


class SelfModelSection:
    priority = 40
    label = "self_model"
    is_static = False

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        self_content = ctx.memory.read_self()
        if not self_content:
            return None
        return f"## Nexus 自我认知\n\n{self_content}"

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


class SOPIndexSection:
    priority = 50
    label = "sop_index"
    is_static = True

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        sop_index = (cached_signature or "").strip()
        if not sop_index:
            return None
        return build_sop_index_prompt(sop_index)

    def cache_signature(self, ctx: TurnContext) -> str | None:
        sop_readme = ctx.workspace / "sop" / "README.md"
        if not sop_readme.exists():
            return None
        try:
            return sop_readme.read_text(encoding="utf-8")
        except Exception:
            return None


class SkillsSection:
    priority = 60
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


class MemesSection:
    priority = 65
    label = "memes"
    is_static = False

    def __init__(self, catalog: MemeCatalog) -> None:
        self._catalog = catalog

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        block = self._catalog.build_prompt_block()
        if not block:
            return None
        return f"# Memes\n\n{block}"

    def cache_signature(self, ctx: TurnContext) -> str | None:
        return None


class SkillsCatalogSection:
    priority = 70
    label = "skills_catalog"
    is_static = True

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        summary = cached_signature or ""
        if not summary:
            return None
        return build_skills_catalog_prompt(summary)

    def cache_signature(self, ctx: TurnContext) -> str | None:
        summary = ctx.skills.build_skills_summary()
        return summary or None


@dataclass
class SystemPromptBuildResult:
    system_sections: list[PromptSectionRender]
    system_prompt: str
    debug_breakdown: list[PromptSectionMeta]


class SystemPromptBuilder:
    def __init__(self, sections: list[ContextSection], cache: SectionCache | None = None):
        self._sections = sorted(sections, key=lambda s: s.priority)
        self._cache = cache or SectionCache()

    def build(
        self, ctx: TurnContext, *, disabled_sections: set[str] | None = None
    ) -> SystemPromptBuildResult:
        renders: list[PromptSectionRender] = []
        breakdown: list[PromptSectionMeta] = []
        disabled = disabled_sections or set()
        cache_scope = str(ctx.workspace.expanduser().resolve())
        for section in self._sections:
            if section.label in disabled:
                continue
            cache_hit = False
            rendered: str | None = None
            signature = section.cache_signature(ctx) if section.is_static else None
            # static section 先按 scope + signature 查缓存，避免重复读文件或重复算摘要。
            if signature:
                rendered = self._cache.get(cache_scope, section.label, signature)
                cache_hit = rendered is not None
            if rendered is None:
                rendered = section.render(ctx, cached_signature=signature)
                if rendered and signature:
                    self._cache.set(cache_scope, section.label, signature, rendered)
            if rendered:
                renders.append(
                    PromptSectionRender(
                        name=section.label,
                        content=rendered,
                        is_static=section.is_static,
                        cache_hit=cache_hit,
                    )
                )
                breakdown.append(
                    PromptSectionMeta(
                        name=section.label,
                        chars=len(rendered),
                        est_tokens=max(1, len(rendered) // 3),
                        is_static=section.is_static,
                        cache_hit=cache_hit,
                    )
                )
        return SystemPromptBuildResult(
            system_sections=renders,
            system_prompt="\n\n---\n\n".join(item.content for item in renders),
            debug_breakdown=breakdown,
        )


class ChannelPolicy(Protocol):
    channel: str

    def augment_system_prompt(self, prompt: str) -> str: ...


class TelegramChannelPolicy:
    channel = "telegram"

    def augment_system_prompt(self, prompt: str) -> str:
        return prompt + build_telegram_rendering_prompt()


class MessageEnvelopeBuilder:
    def __init__(self, policies: dict[str, ChannelPolicy] | None = None):
        self._policies = policies or {}

    def build(
        self,
        *,
        history: list[dict[str, Any]],
        current_message: str,
        system_prompt: str,
        system_context: dict[str, str] | None,
        runtime_guard_context: dict[str, str] | None,
        channel: str | None,
        media: list[str] | None,
    ) -> list[dict[str, Any]]:
        prompt = system_prompt
        if channel:
            policy = self._policies.get(channel)
            if policy is not None:
                prompt = policy.augment_system_prompt(prompt)

        # 顺序是有意设计的：system prompt -> side context -> runtime guard -> history -> 当前用户消息。
        messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
        for text in (system_context or {}).values():
            if text.strip():
                messages.append(build_system_context_message(text))
        for text in (runtime_guard_context or {}).values():
            if text.strip():
                messages.append(build_runtime_guard_message(text))
        messages.extend(history)
        messages.append(
            {
                "role": "user",
                "content": self._build_user_content(current_message, media),
            }
        )
        return messages

    def _build_user_content(
        self, text: str, media: list[str] | None
    ) -> str | list[dict[str, Any]]:
        if not media:
            return text

        images = []
        for item in media:
            item = str(item)
            if item.startswith(("http://", "https://")):
                images.append({"type": "image_url", "image_url": {"url": item}})
                continue

            p = Path(item)
            mime, _ = mimetypes.guess_type(p)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue
            with p.open("rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            images.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                }
            )

        if not images:
            return text
        return images + [{"type": "text", "text": text}]


class ContextBuilder:
    def __init__(self, workspace: Path, memory: "MemoryPort"):
        self.workspace = workspace
        self.skills = SkillsLoader(workspace)
        self.memory = memory
        self._system_prompt_builder = SystemPromptBuilder(
            [
                StaticIdentitySection(),
                MemoryBlockSection(),
                LongTermMemorySection(),
                SelfModelSection(),
                SOPIndexSection(),
                SkillsSection(),
                MemesSection(MemeCatalog(workspace / "memes")),
                SkillsCatalogSection(),
            ]
        )
        self._envelope_builder = MessageEnvelopeBuilder(
            policies={TelegramChannelPolicy.channel: TelegramChannelPolicy()}
        )
        self._assembler = PromptAssembler(self)
        self._last_debug_breakdown: list[PromptSectionMeta] = []
        self._last_assembled_contexts: dict[str, dict[str, str]] = {
            "system_context": {},
            "runtime_guard_context": {},
        }

    @property
    def last_debug_breakdown(self) -> list[PromptSectionMeta]:
        return list(self._last_debug_breakdown)

    @property
    def last_assembled_contexts(self) -> dict[str, dict[str, str]]:
        return {
            "system_context": dict(self._last_assembled_contexts["system_context"]),
            "runtime_guard_context": dict(
                self._last_assembled_contexts["runtime_guard_context"]
            ),
        }

    def build_system_context(
        self,
        *,
        channel: str | None = None,
        chat_id: str | None = None,
        message_timestamp: "datetime | None" = None,
    ) -> dict[str, str]:
        # 这里只放“本轮系统事实”，避免把 request_time / session 之类的易变信息塞回主 prompt。
        context = {
            "request_time": build_agent_request_time_prompt(
                message_timestamp=message_timestamp
            ),
            "environment": build_agent_environment_prompt(),
        }
        if channel and chat_id:
            context["current_session"] = build_current_session_prompt(
                channel=channel,
                chat_id=chat_id,
            ).strip()
        return context

    def build_runtime_guard_context(
        self,
        *,
        preflight_prompt: str | None = None,
    ) -> dict[str, str]:
        # runtime guard 和 system_context 同样用 system role，
        # 但语义上是“本轮约束”，由调用方按 turn 动态注入。
        if not preflight_prompt:
            return {}
        return {"preflight": preflight_prompt}

    def _build_system_prompt_result(
        self,
        skill_names: list[str] | None = None,
        message_timestamp: "datetime | None" = None,
        retrieved_memory_block: str = "",
        disabled_sections: set[str] | None = None,
    ) -> SystemPromptBuildResult:
        ctx = TurnContext(
            workspace=self.workspace,
            memory=self.memory,
            skills=self.skills,
            skill_names=skill_names or [],
            message_timestamp=message_timestamp,
            retrieved_memory_block=retrieved_memory_block,
        )
        built = self._system_prompt_builder.build(
            ctx,
            disabled_sections=disabled_sections,
        )
        self._last_debug_breakdown = built.debug_breakdown
        if built.debug_breakdown:
            logger.info(
                "prompt breakdown: %s",
                ", ".join(
                    f"{item.name}[chars={item.chars},tokens~={item.est_tokens},static={int(item.is_static)},cache={int(item.cache_hit)}]"
                    for item in built.debug_breakdown
                ),
            )
        return built

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        message_timestamp: "datetime | None" = None,
        retrieved_memory_block: str = "",
        disabled_sections: set[str] | None = None,
    ) -> str:
        return self._build_system_prompt_result(
            skill_names=skill_names,
            message_timestamp=message_timestamp,
            retrieved_memory_block=retrieved_memory_block,
            disabled_sections=disabled_sections,
        ).system_prompt

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        media: list[str] | None = None,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        message_timestamp: "datetime | None" = None,
        retrieved_memory_block: str = "",
        disabled_sections: set[str] | None = None,
        runtime_guard_context: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        assembled = self._assembler.assemble(
            history=history,
            current_message=current_message,
            media=media,
            skill_names=skill_names,
            channel=channel,
            chat_id=chat_id,
            message_timestamp=message_timestamp,
            retrieved_memory_block=retrieved_memory_block,
            disabled_sections=disabled_sections,
            runtime_guard_context=runtime_guard_context,
        )
        self._last_debug_breakdown = assembled.debug_breakdown
        self._last_assembled_contexts = {
            "system_context": dict(assembled.system_context),
            "runtime_guard_context": dict(assembled.runtime_guard_context),
        }
        return assembled.messages

    def _build_user_content(
        self, text: str, media: list[str] | None
    ) -> str | list[dict[str, Any]]:
        return self._envelope_builder._build_user_content(text, media)

    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> list[dict[str, Any]]:
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "content": result,
            }
        )
        return messages

    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        msg: dict[str, Any] = {"role": "assistant"}
        if content:
            msg["content"] = content
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        messages.append(msg)
        return messages
