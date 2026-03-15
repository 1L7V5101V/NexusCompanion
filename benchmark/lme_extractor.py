"""LongMemEval 专用的用户事实提取器。

从 user-assistant 对话（单个 session）中提取用户的三类事实：
  event    - 用户做了什么、发生了什么、买了什么、计划了什么
  profile  - 用户的个人属性（职业、地点、健康、家庭等）
  preference - 用户的稳定偏好/厌恶

与 EpisodeExtractor 的区别：
  - 只关心"用户"这一侧（不区分多角色）
  - 输出 event / profile / preference（不含 procedure）
  - 针对 user-assistant 对话格式
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LMEFact:
    summary: str
    memory_type: str       # event | profile | preference
    happened_at: str | None


class LMEExtractor:
    def __init__(self, llm_client: Any, model: str, max_tokens: int = 1200) -> None:
        self._llm = llm_client
        self._model = model
        self._max_tokens = max_tokens

    def extract_from_session(
        self,
        turns: list[dict],
        session_date: str,
    ) -> list[LMEFact]:
        """从一个 session 的所有 turn 中提取用户事实。"""
        conversation = self._build_conversation(turns)
        if not conversation:
            return []
        prompt = self._build_prompt(conversation=conversation, session_date=session_date)
        try:
            resp = self._llm.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=self._max_tokens,
            )
            content = (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.warning("LMEExtractor LLM 调用失败: %s", exc)
            return []
        return self._parse_facts(content, session_date=session_date)

    @staticmethod
    def _build_conversation(turns: list[dict]) -> str:
        lines: list[str] = []
        for turn in turns:
            role = str(turn.get("role", "")).strip()
            content = str(turn.get("content", "")).strip()
            if not content:
                continue
            label = "USER" if role == "user" else "ASSISTANT"
            lines.append(f"{label}: {content}")
        return "\n".join(lines)

    @staticmethod
    def _build_prompt(*, conversation: str, session_date: str) -> str:
        return f"""你是长期记忆事实提取器。从以下 user-assistant 对话中提取用户（USER）的可检索事实。

session_date：{session_date}

【提取规则】
- 只提取 USER 说的内容，不提取 ASSISTANT 的陈述
- 每一件具体的事、每一个具体物品/名称/数量单独一条，绝对不要合并
  ✗ 错误："用户买了多件商品"
  ✓ 正确：每件商品单独一条，写出具体名称
- 忽略纯寒暄、打招呼、无信息量的内容
- summary 使用第三人称："用户 [做了/是/有/喜欢] [具体内容]"
- summary 必须能脱离上下文独立被检索

【分类规则】
- event：用户做了某事、发生了某事、购买了某物、完成了某项任务、状态变化
  示例："用户完成了马拉松" / "用户订购了索尼 WH-1000XM5 耳机"
- profile：用户的个人属性（职业、居住地、健康状况、家庭成员、技能等）
  示例："用户是一名软件工程师" / "用户对花生过敏"
- preference：用户的稳定喜好或厌恶，会影响未来推荐/对话
  示例："用户偏好轻量级跑步装备" / "用户不喜欢看恐怖片"

对话内容：
{conversation}

只输出 XML，facts 数量不限，有多少写多少（无事实时输出 <facts></facts>）：
<facts>
  <fact>
    <summary>第三人称描述</summary>
    <memory_type>event|profile|preference</memory_type>
    <happened_at>{session_date}</happened_at>
  </fact>
</facts>"""

    def _parse_facts(self, raw_output: str, *, session_date: str) -> list[LMEFact]:
        allowed = {"event", "profile", "preference"}
        blocks = re.findall(r"<fact>\s*(.*?)\s*</fact>", raw_output or "", re.DOTALL)
        facts: list[LMEFact] = []
        seen: set[str] = set()
        for block in blocks:
            summary = self._extract_tag(block, "summary")
            memory_type = self._extract_tag(block, "memory_type").lower()
            happened_at = self._extract_tag(block, "happened_at") or session_date
            if not summary or memory_type not in allowed:
                continue
            key = summary.lower().strip()
            if key in seen:
                continue
            seen.add(key)
            facts.append(LMEFact(
                summary=summary,
                memory_type=memory_type,
                happened_at=happened_at,
            ))
        return facts

    @staticmethod
    def _extract_tag(text: str, tag: str) -> str:
        m = re.search(
            rf"<{tag}>\s*(.*?)\s*</{tag}>",
            text or "",
            flags=re.IGNORECASE | re.DOTALL,
        )
        return m.group(1).strip() if m else ""


__all__ = ["LMEExtractor", "LMEFact"]
