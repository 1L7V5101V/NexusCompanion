"""
LongMemEval 生产链路 Ingestor

对每个 session 并行跑三条真实生产提取链路：

  1. Consolidation 风格提取  →  memory_type=event
     来源：agent/looping/consolidation.py _consolidate_memory()
     同一个 prompt，每 session 1 次 LLM 调用

  2. ProfileFactExtractor.extract()  →  memory_type=profile
     来源：memory2/profile_extractor.py
     直接复用生产代码，每 session 1 次 LLM 调用

  3. _extract_implicit 风格提取  →  memory_type=preference / event
     来源：memory2/post_response_worker.py _extract_implicit()
     同一个 prompt，整个 session 作为单次交换传入，每 session 1 次 LLM 调用

三条链路并行，总计 3 个 LLM 调用/session。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import json_repair

import openai

from memory2.profile_extractor import ProfileFactExtractor, ProfileFact

logger = logging.getLogger(__name__)


# ─── Provider Adapter ────────────────────────────────────────────────
# ProfileFactExtractor 使用生产 LLMProvider.chat() 接口（返回带 .content 的对象）
# 这里将 openai.OpenAI 包装成相同接口供生产代码直接复用。

@dataclass
class _ChatResponse:
    content: str


class _ProviderAdapter:
    """将 openai.OpenAI 同步客户端包装为生产 LLMProvider 异步接口。"""

    def __init__(self, client: openai.OpenAI, default_model: str) -> None:
        self._client = client
        self._default_model = default_model

    async def chat(
        self,
        messages: list[dict],
        tools: list | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        **_kwargs: Any,
    ) -> _ChatResponse:
        m = model or self._default_model
        mt = max_tokens or 500
        resp = await asyncio.to_thread(
            self._client.chat.completions.create,
            model=m,
            messages=messages,
            max_tokens=mt,
        )
        content = resp.choices[0].message.content or ""
        return _ChatResponse(content=content)


# ─── IngestFact（统一输出格式）────────────────────────────────────────

@dataclass
class IngestFact:
    summary: str
    memory_type: str       # event | profile | preference
    happened_at: str | None
    source: str            # consolidation | profile | implicit


# ─── 辅助：格式化对话 ─────────────────────────────────────────────────

def _format_conversation(turns: list[dict], session_date: str = "") -> str:
    """将 session turns 格式化为 [date HH:MM] ROLE: content 形式（与 consolidation 一致）。"""
    lines: list[str] = []
    for turn in turns:
        role = str(turn.get("role", "")).strip()
        content = str(turn.get("content", "")).strip()
        if not content:
            continue
        label = "USER" if role == "user" else "ASSISTANT"
        lines.append(f"[{session_date}] {label}: {content}")
    return "\n".join(lines)


def _format_user_messages(turns: list[dict]) -> str:
    """只取 USER 消息，供 implicit 提取使用。"""
    lines = [
        str(t.get("content", "")).strip()
        for t in turns
        if t.get("role") == "user" and t.get("content", "").strip()
    ]
    return "\n".join(lines)


# ─── 三条提取链路 ─────────────────────────────────────────────────────

class LMEProductionIngestor:
    """
    对 LME haystack session 运行三条生产提取链路，返回 IngestFact 列表。
    同步入口 extract_from_session_sync()，内部异步并行执行三条 pipeline。
    """

    def __init__(self, light_client: openai.OpenAI, light_model: str) -> None:
        self._adapter = _ProviderAdapter(light_client, light_model)
        self._model = light_model
        self._client = light_client
        # 直接复用生产 ProfileFactExtractor
        self._profile_extractor = ProfileFactExtractor(
            llm_client=self._adapter,
            model=light_model,
            max_tokens=600,
            timeout_ms=5000,
        )

    def extract_all_sessions_sync(
        self,
        sessions: list[list[dict]],
        dates: list[str],
        concurrency: int = 12,
    ) -> list[list[IngestFact]]:
        """
        同步入口：对所有 session 并发提取，返回与 sessions 等长的结果列表。
        concurrency 控制同时进行的 LLM 调用组数（每组 3 个并行调用）。
        """
        return asyncio.run(self._extract_all_sessions(sessions, dates, concurrency))

    async def _extract_all_sessions(
        self,
        sessions: list[list[dict]],
        dates: list[str],
        concurrency: int,
    ) -> list[list[IngestFact]]:
        sem = asyncio.Semaphore(concurrency)

        async def _bounded(turns, date):
            async with sem:
                return await self._extract_all(turns, date)

        tasks = [_bounded(turns, str(date or "")) for turns, date in zip(sessions, dates)]
        return await asyncio.gather(*tasks, return_exceptions=False)

    async def _extract_all(
        self,
        turns: list[dict],
        session_date: str,
    ) -> list[IngestFact]:
        conversation = _format_conversation(turns, session_date)
        if not conversation.strip():
            return []

        events_task = self._extract_consolidation_events(conversation, session_date)
        profile_task = self._extract_profile_facts(conversation)
        implicit_task = self._extract_implicit(turns)

        results = await asyncio.gather(events_task, profile_task, implicit_task, return_exceptions=True)

        facts: list[IngestFact] = []
        for r in results:
            if isinstance(r, list):
                facts.extend(r)
            elif isinstance(r, Exception):
                logger.warning("extraction pipeline failed: %s", r)
        return facts

    # ── 1. Consolidation → event ──────────────────────────────────────

    async def _extract_consolidation_events(
        self, conversation: str, session_date: str
    ) -> list[IngestFact]:
        """
        来源：agent/looping/consolidation.py _consolidate_memory() 的主 LLM 提取。
        提取 history_entries（按主题拆分的带时间戳事件摘要）→ event 类型。
        """
        prompt = f"""你是记忆提取代理（Memory Extraction Agent）。从对话中精确提取结构化信息，返回 JSON。

## 字段说明

### 1. "history_entries" → 事件记录（数组，每条对应一个独立主题）
按主题拆分，每个独立话题写一条，1-2 句，以 [{session_date}] 开头，保留足够细节便于未来检索。
不同主题必须拆成独立条目，不得合并。若整段对话只有一个主题，返回只含一条的数组。

## 提取规则（严格遵守）

1. **只提取 USER 明确表达的行动、经历、计划和状态**：
   ASSISTANT 的建议、推荐、解释一律不作为 history_entry。
   即使 ASSISTANT 提到了地名、店名、活动，若 USER 未确认执行，不得提取。

2. **必须是第三人称摘要句**：
   每条 history_entry 绝对不能包含 "USER:"、"ASSISTANT:" 等原始对话标记，
   不得复制粘贴原始对话文本。示例：
   ✗ 错误：[2023-05-10] USER: I got a stand mixer as a birthday gift from my sister
   ✓ 正确：[2023-05-10] 用户收到姐姐送的搅拌机作为生日礼物

3. **保留关键细节**：
   商家名称、地点、人名、数量、价格、型号等具体信息必须保留，不得用"某商店""某地方"概括。

## 待处理对话
{conversation}

只返回合法 JSON，格式：{{"history_entries": ["[{session_date}] ..."]}}
不要 markdown 代码块。"""

        try:
            resp = await asyncio.to_thread(
                self._client.chat.completions.create,
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500,
                temperature=0.1,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            data = json_repair.loads(text)
            entries = data.get("history_entries", []) if isinstance(data, dict) else []
            return [
                IngestFact(
                    summary=str(e).strip(),
                    memory_type="event",
                    happened_at=session_date or None,
                    source="consolidation",
                )
                for e in entries
                if isinstance(e, str) and e.strip()
            ]
        except Exception as exc:
            logger.warning("consolidation extraction failed: %s", exc)
            return []

    # ── 2. ProfileFactExtractor → profile ────────────────────────────

    async def _extract_profile_facts(self, conversation: str) -> list[IngestFact]:
        """
        来源：memory2/profile_extractor.py ProfileFactExtractor.extract()
        直接复用生产代码，提取 purchase / decision / preference / status / personal_fact。
        """
        try:
            facts: list[ProfileFact] = await self._profile_extractor.extract(conversation)
            return [
                IngestFact(
                    summary=f.summary,
                    memory_type="profile",
                    happened_at=f.happened_at,
                    source="profile_extractor",
                )
                for f in facts
            ]
        except Exception as exc:
            logger.warning("profile extraction failed: %s", exc)
            return []

    # ── 3. _extract_implicit → preference / event ────────────────────

    async def _extract_implicit(self, turns: list[dict]) -> list[IngestFact]:
        """
        来源：memory2/post_response_worker.py _extract_implicit()
        同一个 prompt，将整个 session 的 USER 消息合并后作为 user_msg 传入。
        只保留 preference 和 event（过滤掉 procedure，LME 不测 agent 行为规范）。
        """
        user_text = _format_user_messages(turns)
        if not user_text.strip():
            return []

        # 直接使用生产 prompt，仅替换对话内容部分
        prompt = f"""你是记忆提取专家。从以下对话中提取两类长期有效的信息：
1. 用户对 agent 行为的隐式偏好或操作规范
2. 用户对内容题材/作品/游戏/作者/来源的稳定喜欢或厌恶，尤其是会影响未来主动推送的偏好

【唯一有效依据来源：USER 说的话】
ASSISTANT 的回复只是对话背景，不能作为提取依据。
即使 ASSISTANT 描述了某个流程/规则，若 USER 没有明确表达纠正/要求/不满，也不能提取。

【提取标准——必须同时满足以下全部条件】
1. 能从 USER 的原话中直接引用出明确的纠正/不满/要求/偏好信号
   - 行为规范信号示例："你应该""你不能""你之前错了""下次要"
   - 内容偏好信号示例："我就讨厌《X》""以后别给我推 X""我很喜欢《Y》"
   无法引用 USER 原话 → 直接返回 []
2. 该信号必须具有跨对话持久意义
3. 若是内容偏好，必须足够明确，能够影响未来推荐/主动推送/举例方式

【明确不写】
✗ ASSISTANT 回复中描述的任何流程、规则、步骤
✗ USER 只是在查询/确认信息
✗ 无法在 USER 原话中找到纠正/不满/强调句的条目
✗ 一次性操作记录
✗ 带明确时间锚点的短期计划或近期打算
【preference vs event 判断标准】6 个月后是否仍然适用？
  → 是（稳定偏好/厌恶/规避意图）→ preference
  → 否（当前计划/近期打算/一次性感受）→ event（若有记忆价值）或不写

【重要】大多数对话不包含可提取的偏好，返回 [] 是正常且正确的结果。
【数量限制】最多 3 条，只写有 USER 原文依据的。

【对话内容（USER 消息）】
{user_text}

只返回合法 JSON 数组，无内容时返回 []。
每项格式：{{"summary": "...", "memory_type": "preference|event"}}"""

        try:
            resp = await asyncio.to_thread(
                self._client.chat.completions.create,
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0.1,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            items = json_repair.loads(text)
            if not isinstance(items, list):
                return []
            allowed = {"preference", "event"}
            return [
                IngestFact(
                    summary=str(item.get("summary", "")).strip(),
                    memory_type=item.get("memory_type", "preference"),
                    happened_at=None,
                    source="implicit",
                )
                for item in items
                if isinstance(item, dict)
                and item.get("summary", "").strip()
                and item.get("memory_type") in allowed
            ]
        except Exception as exc:
            logger.warning("implicit extraction failed: %s", exc)
            return []


__all__ = ["LMEProductionIngestor", "IngestFact"]
