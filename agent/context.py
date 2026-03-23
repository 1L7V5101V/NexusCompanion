import base64
import mimetypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from agent.skills import SkillsLoader
from prompts.agent import (
    build_agent_identity_prompt,
    build_current_session_prompt,
    build_skills_catalog_prompt,
    build_sop_index_prompt,
    build_telegram_rendering_prompt,
)

if TYPE_CHECKING:
    from core.memory.port import MemoryPort


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

    def render(self, ctx: TurnContext) -> str | None: ...


class IdentitySection:
    priority = 10
    label = "identity"

    def render(self, ctx: TurnContext) -> str | None:
        return build_agent_identity_prompt(
            workspace=ctx.workspace,
            message_timestamp=ctx.message_timestamp,
        )


class MemoryBlockSection:
    priority = 20
    label = "retrieved_memory"

    def render(self, ctx: TurnContext) -> str | None:
        block = (ctx.retrieved_memory_block or "").strip()
        return block or None


class LongTermMemorySection:
    priority = 30
    label = "long_term_memory"

    def render(self, ctx: TurnContext) -> str | None:
        memory = ctx.memory.get_memory_context()
        return str(memory).strip() if memory else None


class SelfModelSection:
    priority = 40
    label = "self_model"

    def render(self, ctx: TurnContext) -> str | None:
        self_content = ctx.memory.read_self()
        if not self_content:
            return None
        return f"## Nexus 自我认知\n\n{self_content}"


class SOPIndexSection:
    priority = 50
    label = "sop_index"

    def render(self, ctx: TurnContext) -> str | None:
        sop_readme = ctx.workspace / "sop" / "README.md"
        if not sop_readme.exists():
            return None
        try:
            sop_index = sop_readme.read_text(encoding="utf-8").strip()
        except Exception:
            return None
        if not sop_index:
            return None
        return build_sop_index_prompt(sop_index)


class SkillsSection:
    priority = 60
    label = "active_skills"

    def render(self, ctx: TurnContext) -> str | None:
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


class SkillsCatalogSection:
    priority = 70
    label = "skills_catalog"

    def render(self, ctx: TurnContext) -> str | None:
        summary = ctx.skills.build_skills_summary()
        if not summary:
            return None
        return build_skills_catalog_prompt(summary)


class SystemPromptBuilder:
    def __init__(self, sections: list[ContextSection]):
        self._sections = sorted(sections, key=lambda s: s.priority)

    def build(self, ctx: TurnContext) -> str:
        parts: list[str] = []
        for section in self._sections:
            rendered = section.render(ctx)
            if rendered:
                parts.append(rendered)
        return "\n\n---\n\n".join(parts)


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
        channel: str | None,
        chat_id: str | None,
        media: list[str] | None,
    ) -> list[dict[str, Any]]:
        prompt = system_prompt
        if channel and chat_id:
            prompt += build_current_session_prompt(channel=channel, chat_id=chat_id)
        if channel:
            policy = self._policies.get(channel)
            if policy is not None:
                prompt = policy.augment_system_prompt(prompt)

        messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
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
    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md"]

    def __init__(self, workspace: Path, memory: "MemoryPort"):
        self.workspace = workspace
        self.skills = SkillsLoader(workspace)
        self.memory = memory
        self._system_prompt_builder = SystemPromptBuilder(
            [
                IdentitySection(),
                MemoryBlockSection(),
                LongTermMemorySection(),
                SelfModelSection(),
                SOPIndexSection(),
                SkillsSection(),
                SkillsCatalogSection(),
            ]
        )
        self._envelope_builder = MessageEnvelopeBuilder(
            policies={TelegramChannelPolicy.channel: TelegramChannelPolicy()}
        )

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        message_timestamp: "datetime | None" = None,
        retrieved_memory_block: str = "",
    ) -> str:
        ctx = TurnContext(
            workspace=self.workspace,
            memory=self.memory,
            skills=self.skills,
            skill_names=skill_names or [],
            message_timestamp=message_timestamp,
            retrieved_memory_block=retrieved_memory_block,
        )
        return self._system_prompt_builder.build(ctx)

    def _get_identity(self, message_timestamp: "datetime | None" = None) -> str:
        return build_agent_identity_prompt(
            workspace=self.workspace,
            message_timestamp=message_timestamp,
        )

    def _load_bootstrap_files(self) -> str:
        parts = []
        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
        return "\n\n".join(parts) if parts else ""

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
    ) -> list[dict[str, Any]]:
        system_prompt = self.build_system_prompt(
            skill_names=skill_names,
            message_timestamp=message_timestamp,
            retrieved_memory_block=retrieved_memory_block,
        )
        return self._envelope_builder.build(
            history=history,
            current_message=current_message,
            system_prompt=system_prompt,
            channel=channel,
            chat_id=chat_id,
            media=media,
        )

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
