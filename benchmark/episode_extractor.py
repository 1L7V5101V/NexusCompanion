from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import openai

logger = logging.getLogger(__name__)


@dataclass
class ExtractedFact:
    character: str
    summary: str
    memory_type: str
    category: str
    happened_at: str | None


class EpisodeExtractor:
    def __init__(self, llm_client: openai.OpenAI, model: str) -> None:
        self._llm_client = llm_client
        self._model = model

    def extract_from_session(
        self,
        session_utterances: list[dict],
        session_date: str,
        characters: list[str],
    ) -> list[ExtractedFact]:
        # 1. 先把 session 压成稳定文本，空输入直接返回。
        conversation = self._build_conversation(session_utterances)
        if not conversation:
            return []

        # 2. 再调用 LLM 提取 XML，异常时返回空列表。
        prompt = self._build_prompt(
            conversation=conversation,
            session_date=session_date,
            characters=characters,
        )
        try:
            response = self._llm_client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=3000,
            )
        except Exception as exc:
            logger.warning("EpisodeExtractor LLM 调用失败: %s", exc)
            return []

        # 3. 最后解析 XML、去重并做类型映射。
        content = (response.choices[0].message.content or "").strip()
        return self._parse_facts(content, session_date=session_date)

    @staticmethod
    def _build_conversation(session_utterances: list[dict]) -> str:
        lines: list[str] = []
        for utterance in session_utterances:
            speaker = str(utterance.get("speaker", "")).strip() or "Unknown"
            text = str(utterance.get("text", "")).strip()
            dia_id = str(utterance.get("dia_id", "")).strip()
            if not text:
                continue
            prefix = f"[{dia_id}] " if dia_id else ""
            lines.append(f"{prefix}{speaker}: {text}")
        return "\n".join(lines)

    @staticmethod
    def _build_prompt(
        *,
        conversation: str,
        session_date: str,
        characters: list[str],
    ) -> str:
        joined_characters = ", ".join(characters)
        return f"""你是长期记忆事实提取器。从下面的对话中提取所有值得记住的原子事实，写入记忆库。

角色列表：{joined_characters}
session_date：{session_date}

【核心原则：宁多勿少，宁具体勿笼统】

提取规则：
- 只提取对话中明确说出的内容，不推断
- 每一件具体的事、每一个具体的物品/名称/数量单独一条 fact，绝对不要合并
  ✗ 错误："Melanie 画了风景和静物"
  ✓ 正确：分别写 "Melanie 画了一匹马" / "Melanie 画了日落" / "Melanie 画了日出"
- 涉及列举（多本书、多项活动、多个物品）时，每项单独一条 fact
  ✗ 错误："Melanie 读了几本书"
  ✓ 正确：每本书单独一条，写出书名
- 涉及人物属性/喜好时，写出具体内容而非概括
  ✗ 错误："Melanie 的孩子们喜欢自然相关的东西"
  ✓ 正确："Melanie 的孩子们喜欢恐龙和大自然"
- summary 使用第三人称，格式："[角色名] [做了/是/有/喜欢] [具体内容]"
- summary 必须能脱离上下文独立被检索到
- 忽略纯寒暄、无信息量的聊天

分类规则：
- category 只能是：event / personal_fact / preference / status / decision
- memory_type 只允许：event / profile
- category=event|status|decision → memory_type=event
- category=personal_fact|preference → memory_type=profile
- happened_at 统一写 {session_date}

对话内容：
{conversation}

只输出 XML，facts 数量不限，有多少写多少：
<facts>
  <fact>
    <character>角色名</character>
    <summary>一句话具体事实</summary>
    <memory_type>event|profile</memory_type>
    <category>event|personal_fact|preference|status|decision</category>
    <happened_at>{session_date}</happened_at>
  </fact>
</facts>"""

    def _parse_facts(self, raw_output: str, *, session_date: str) -> list[ExtractedFact]:
        allowed_categories = {
            "event": "event",
            "status": "event",
            "decision": "event",
            "personal_fact": "profile",
            "preference": "profile",
        }
        blocks = re.findall(r"<fact>\s*(.*?)\s*</fact>", raw_output or "", re.DOTALL)
        facts: list[ExtractedFact] = []
        seen: set[tuple[str, str, str]] = set()
        for block in blocks:
            character = self._extract_tag(block, "character")
            summary = self._extract_tag(block, "summary")
            category = self._extract_tag(block, "category").lower()
            memory_type = self._extract_tag(block, "memory_type").lower()
            happened_at = self._extract_tag(block, "happened_at") or session_date
            if not character or not summary or category not in allowed_categories:
                continue
            resolved_memory_type = allowed_categories[category]
            if memory_type and memory_type not in {"event", "profile"}:
                continue
            if memory_type and memory_type != resolved_memory_type:
                memory_type = resolved_memory_type
            else:
                memory_type = resolved_memory_type
            key = (character, summary, category)
            if key in seen:
                continue
            seen.add(key)
            facts.append(
                ExtractedFact(
                    character=character,
                    summary=summary,
                    memory_type=memory_type,
                    category=category,
                    happened_at=happened_at,
                )
            )
        return facts

    @staticmethod
    def _extract_tag(raw_output: str, tag: str) -> str:
        match = re.search(
            rf"<{tag}>\s*(.*?)\s*</{tag}>",
            raw_output or "",
            flags=re.IGNORECASE | re.DOTALL,
        )
        return match.group(1).strip() if match else ""


__all__ = ["EpisodeExtractor", "ExtractedFact"]
