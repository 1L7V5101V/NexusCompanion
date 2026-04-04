from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent.llm_json import load_json_object_loose

logger = logging.getLogger("agent.core.consolidation")

if TYPE_CHECKING:
    from agent.provider import LLMProvider
    from core.memory.port import MemoryPort
    from memory2.profile_extractor import ProfileFactExtractor

_ALLOWED_PENDING_TAGS = frozenset(
    {
        "identity",
        "preference",
        "key_info",
        "health_long_term",
        "requested_memory",
        "correction",
    }
)


def _format_pending_items(raw_items) -> str:
    if not isinstance(raw_items, list):
        return ""

    lines = []
    seen = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag", "")).strip().lower()
        content = str(item.get("content", "")).strip()
        if tag not in _ALLOWED_PENDING_TAGS or not content:
            continue
        line = f"- [{tag}] {content}"
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines)


def _parse_consolidation_payload(text: str) -> dict | None:
    return load_json_object_loose(text)


@dataclass(frozen=True)
class ConsolidationWindow:
    old_messages: list[dict]
    keep_count: int
    consolidate_up_to: int


def _select_consolidation_window(
    session,
    *,
    memory_window: int,
    archive_all: bool,
) -> ConsolidationWindow | None:
    total_messages = len(session.messages)
    if archive_all:
        return ConsolidationWindow(
            old_messages=list(session.messages),
            keep_count=0,
            consolidate_up_to=total_messages,
        )

    keep_count = memory_window // 2
    if total_messages <= keep_count:
        return None
    if total_messages - session.last_consolidated <= 0:
        return None

    consolidate_up_to = total_messages - keep_count
    old_messages = session.messages[session.last_consolidated : consolidate_up_to]
    if not old_messages:
        return None
    return ConsolidationWindow(
        old_messages=old_messages,
        keep_count=keep_count,
        consolidate_up_to=consolidate_up_to,
    )


def _build_consolidation_source_ref(window: ConsolidationWindow) -> str:
    ids = [str(msg["id"]) for msg in window.old_messages if msg.get("id")]
    return json.dumps(ids, ensure_ascii=False)


def _build_entry_source_ref(base_source_ref: str, entry: str) -> str:
    text = (entry or "").strip()
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12] if text else "empty"
    return f"{base_source_ref}#h:{digest}"


def _format_conversation_for_consolidation(old_messages: list[dict]) -> str:
    lines = []
    for message in old_messages:
        if not message.get("content") or message.get("role") == "tool":
            continue
        if message.get("role") == "assistant" and message.get("proactive"):
            continue
        role = str(message.get("role", "")).upper()
        ts = str(message.get("timestamp", "?"))[:16]
        lines.append(f"[{ts}] {role}: {message['content']}")
    return "\n".join(lines)


def _select_recent_history_entries(history_text: str, *, limit: int = 3) -> list[str]:
    if not history_text.strip() or limit <= 0:
        return []
    chunks = re.split(r"\n\s*\n+", history_text.strip())
    entries = [chunk.strip() for chunk in chunks if chunk.strip()]
    return entries[-limit:]


class ConsolidationService:
    _EXTRACT_MAX_TOKENS: int = 1024

    def __init__(
        self,
        *,
        memory_port: "MemoryPort",
        provider: "LLMProvider",
        model: str,
        memory_window: int,
        profile_extractor: "ProfileFactExtractor | None" = None,
    ) -> None:
        self._memory_port = memory_port
        self._provider = provider
        self._model = model
        self._memory_window = memory_window
        self._profile_extractor = profile_extractor

    async def _extract_and_save_profile_facts(
        self,
        *,
        extractor,
        conversation: str,
        existing_profile: str,
        source_ref: str,
        scope_channel: str,
        scope_chat_id: str,
    ) -> None:
        try:
            facts = await extractor.extract(
                conversation,
                existing_profile=existing_profile,
            )
            if not facts:
                return

            for fact in facts:
                await self._memory_port.save_item(
                    summary=fact.summary,
                    memory_type="profile",
                    extra={
                        "category": fact.category,
                        "scope_channel": scope_channel,
                        "scope_chat_id": scope_chat_id,
                    },
                    source_ref=f"{source_ref}#profile",
                    happened_at=fact.happened_at,
                )
                logger.info(
                    "memory2 profile fact saved: category=%s %r",
                    fact.category,
                    fact.summary[:60],
                )
        except Exception as e:
            logger.warning("profile fact extraction failed: %s", e)

    async def consolidate(
        self,
        session,
        archive_all: bool = False,
        await_vector_store: bool = False,
    ) -> None:
        memory = self._memory_port

        # 1. 先决定本次归档窗口，没有窗口就直接结束。
        window = _select_consolidation_window(
            session,
            memory_window=self._memory_window,
            archive_all=archive_all,
        )
        if window is None:
            return

        # 2. 准备 prompt 所需的对话、最近 history 和现有长期记忆。
        source_ref = _build_consolidation_source_ref(window)
        conversation = _format_conversation_for_consolidation(window.old_messages)
        current_memory = await asyncio.to_thread(memory.read_long_term)
        recent_history_raw = await asyncio.to_thread(memory.read_history, 16000)
        recent_history_entries = _select_recent_history_entries(
            recent_history_raw if isinstance(recent_history_raw, str) else "",
            limit=3,
        )
        recent_history_block = "\n".join(
            f"- {entry}" for entry in recent_history_entries
        )

        # 3. 让模型抽取 history_entries / pending_items。
        prompt = f"""你是记忆提取代理（Memory Extraction Agent）。从对话中精确提取结构化信息，返回 JSON。

## 字段说明

### 1. "history_entries" → HISTORY.md（数组，每条对应一个独立主题）
按主题拆分，每个独立话题写一条，1-2 句，以 [YYYY-MM-DD HH:MM] 开头，保留足够细节便于未来 grep 检索。
不同主题必须拆成独立条目，不得合并。若整段对话只有一个主题，返回只含一条的数组。

**history_entries 提取规则（严格遵守）**：
1. 只提取 USER 明确表达的行动、经历、计划和状态；ASSISTANT 的建议、推荐、解释一律不写入，即使其中提到了地名、店名或活动。
2. 每条必须是简洁的第三人称摘要句，绝对不能包含 "USER:" 或 "ASSISTANT:" 等原始对话标记，不得复制粘贴原始对话文本。
3. 商家名称、地点、人名、数量、价格、型号等具体细节必须保留，不得用"某商店""某地方"概括。
4. 先判断当前 USER 内容的材料类型：是“用户此刻直接自述”，还是“用户正在展示一段外部聊天记录、截图 OCR、转贴 transcript 给助手看”。
5. 若 USER 内容属于外部聊天记录 / transcript，必须先做层级理解：
   - 外层：当前 USER 正在把一段材料发给助手看。
   - 内层：材料中可能有多个 speaker；这些 speaker 不自动等于当前 USER。
   - 只有当材料中某个 speaker 与当前 USER 的映射在当前会话里被明确确认时，才允许把该 speaker 的事实写入摘要。
6. 对 transcript 场景，默认认为 speaker 映射不明确；除非当前会话中有非常明确的显式说明，否则不要尝试判断材料里的某个昵称/说话人就是用户或对方。
7. 截图 OCR、聊天记录、论坛转贴、邮件内容、工单内容等，若未明确 speaker 身份映射，只能概括为“用户展示了一段关于 X 的聊天/记录/材料”，绝不能把其中内容直接当作用户自己的经历、身份、联系人关系、健康情况、住址等事实写入。
8. 特别是：群聊昵称、备注名、称呼（如“老婆”“妈”“医生”“客户”）在 transcript 中都不能直接视为真实关系标签；它们只能作为材料中的原文称呼，不能作为人物关系事实写入长期记忆。
9. 若材料的主要价值在于“用户当前关心/正在处理/正在审阅某件事”，可以写外层事实，例如“[时间] 用户展示并询问一段关于退款争议的聊天记录”；但不要把内层具体事实归属到用户本人。
10. 若某条信息无法判断是否属于用户本人，宁可不写，也不要猜。

### 2. "pending_items" → PENDING.md（数组）
只记录适合短期待办/待确认的信息。每项格式：
{{"tag":"preference|identity|key_info|health_long_term|requested_memory|correction","content":"..."}}

## 最近三次 consolidation event
{recent_history_block or "（空）"}

## 已有长期记忆
{current_memory or "（空）"}

## 本次对话
{conversation or "（空）"}

提醒：外部聊天记录 / OCR / transcript 中的说话内容，不能作为人物身份、说话人归属、关系判断或具体事实归属的直接证据。
请只返回 JSON。"""
        response = await self._provider.chat(
            messages=[
                {"role": "system", "content": "你是记忆提取代理。"},
                {"role": "user", "content": prompt},
            ],
            tools=[],
            model=self._model,
            max_tokens=self._EXTRACT_MAX_TOKENS,
        )
        payload = _parse_consolidation_payload(getattr(response, "content", "") or "") or {}

        # 4. 写 HISTORY/PENDING，并按需要落向量记忆。
        history_entries = payload.get("history_entries") or payload.get("history_entry") or []
        if isinstance(history_entries, str):
            history_entries = [history_entries]
        pending_text = _format_pending_items(payload.get("pending_items") or [])

        normalized_entries = [
            str(entry).strip() for entry in history_entries if str(entry).strip()
        ]
        if normalized_entries:
            memory.append_history_once("\n\n".join(normalized_entries))
        if pending_text:
            memory.append_pending_once(pending_text)

        save_tasks = [
            memory.save_from_consolidation(
                history_entry=entry,
                source_ref=_build_entry_source_ref(source_ref, entry),
                session_key=session.key,
            )
            for entry in normalized_entries
        ]
        if await_vector_store:
            for task in save_tasks:
                await task
        else:
            for task in save_tasks:
                asyncio.create_task(task)

        if self._profile_extractor is not None:
            existing_profile = await asyncio.to_thread(memory.read_profile)
            await self._extract_and_save_profile_facts(
                extractor=self._profile_extractor,
                conversation=conversation,
                existing_profile=existing_profile if isinstance(existing_profile, str) else "",
                source_ref=source_ref,
                scope_channel=str(getattr(session, "_channel", "")),
                scope_chat_id=str(getattr(session, "_chat_id", "")),
            )

        if not archive_all:
            session.last_consolidated = window.consolidate_up_to
