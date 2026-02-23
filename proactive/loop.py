"""
ProactiveLoop — 主动触达核心循环。

独立于 AgentLoop，定期：
  1. 拉取所有订阅信息流的最新内容
  2. 获取用户最近聊天上下文
  3. 调用 LLM 反思：有没有值得主动说的
  4. 高于阈值时通过 MessagePushTool 发送消息
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent.provider import LLMProvider
from agent.tools.message_push import MessagePushTool
from feeds.base import FeedItem
from feeds.registry import FeedRegistry
from session.manager import SessionManager

logger = logging.getLogger(__name__)


@dataclass
class ProactiveConfig:
    enabled: bool = False
    interval_seconds: int = 1800    # 两次 tick 间隔（秒）
    threshold: float = 0.70         # score 高于此值才发送
    items_per_source: int = 3       # 每个信息源取几条
    recent_chat_messages: int = 20  # 回顾最近 N 条对话
    model: str = ""                 # 留空则继承全局 model
    default_channel: str = "telegram"
    default_chat_id: str = ""


@dataclass
class _Decision:
    score: float
    should_send: bool
    message: str
    reasoning: str


class ProactiveLoop:
    def __init__(
        self,
        feed_registry: FeedRegistry,
        session_manager: SessionManager,
        provider: LLMProvider,
        push_tool: MessagePushTool,
        config: ProactiveConfig,
        model: str,
        max_tokens: int = 1024,
    ) -> None:
        self._feeds = feed_registry
        self._sessions = session_manager
        self._provider = provider
        self._push = push_tool
        self._cfg = config
        self._model = config.model or model
        self._max_tokens = max_tokens
        self._running = False

    async def run(self) -> None:
        self._running = True
        logger.info(
            f"ProactiveLoop 已启动  间隔={self._cfg.interval_seconds}s  "
            f"阈值={self._cfg.threshold}  "
            f"目标={self._cfg.default_channel}:{self._cfg.default_chat_id}"
        )
        while self._running:
            await asyncio.sleep(self._cfg.interval_seconds)
            try:
                await self._tick()
            except Exception:
                logger.exception("ProactiveLoop tick 异常")

    def stop(self) -> None:
        self._running = False

    # ── internal ──────────────────────────────────────────────────

    async def _tick(self) -> None:
        logger.info("[proactive] tick 开始")

        # 1. 并发拉取信息流
        items = await self._feeds.fetch_all(self._cfg.items_per_source)
        logger.info(f"[proactive] 拉取到 {len(items)} 条信息")

        # 2. 最近聊天上下文
        recent = self._collect_recent()

        # 3. LLM 反思
        decision = await self._reflect(items, recent)
        logger.info(
            f"[proactive] score={decision.score:.2f}  "
            f"send={decision.should_send}  "
            f"reasoning={decision.reasoning[:80]!r}"
        )

        # 4. 阈值判断
        if decision.should_send and decision.score >= self._cfg.threshold:
            await self._send(decision.message)
        else:
            logger.info("[proactive] 决定不主动发送")

    def _collect_recent(self) -> list[dict]:
        """取最近活跃 session 的最近 N 条消息（只取 user/assistant 文本）。"""
        try:
            sessions_meta = self._sessions.list_sessions()
        except Exception as e:
            logger.warning(f"[proactive] list_sessions 失败: {e}")
            return []

        if not sessions_meta:
            return []

        # list_sessions 按 updated_at 倒序，取最近一个
        latest = sessions_meta[0]
        key = latest.get("key", "")
        if not key:
            return []

        try:
            session = self._sessions.get_or_create(key)
            msgs = session.messages[-self._cfg.recent_chat_messages:]
            return [
                {"role": m["role"], "content": str(m.get("content", ""))[:200]}
                for m in msgs
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]
        except Exception as e:
            logger.warning(f"[proactive] 加载 session {key!r} 失败: {e}")
            return []

    async def _reflect(self, items: list[FeedItem], recent: list[dict]) -> _Decision:
        now_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        feed_text = _format_items(items) or "（暂无订阅内容）"
        chat_text = _format_recent(recent) or "（无近期对话记录）"

        system_msg = (
            "你是一个陪伴型 AI 助手，正在决定是否主动联系用户。"
            "你了解用户订阅的信息流和最近的对话内容。"
            "你的目标是在恰当的时机分享有价值的信息，而不是频繁打扰用户。"
        )

        user_msg = f"""当前时间：{now_str}

## 订阅信息流（最新内容）

{feed_text}

## 近期对话

{chat_text}

## 任务

综合以上信息，判断是否值得主动联系用户。考虑：
- 信息流里有没有用户可能感兴趣的内容
- 现在说点什么是否自然、不唐突
- 与近期对话有无关联或延伸

只输出 JSON，不要其他内容：
{{
  "reasoning": "内心独白（不会显示给用户，说清楚你的判断依据）",
  "score": 0.0,
  "should_send": false,
  "message": ""
}}

score 说明：0.0=完全没必要  0.5=有点想说  0.7=比较值得  1.0=非常值得立刻说
message 若 should_send=true，写要发给用户的话（口语化，不要像系统通知）"""

        try:
            resp = await self._provider.chat(
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                tools=[],
                model=self._model,
                max_tokens=self._max_tokens,
            )
            return _parse_decision(resp.content or "")
        except Exception as e:
            logger.error(f"[proactive] LLM 反思失败: {e}")
            return _Decision(score=0.0, should_send=False, message="", reasoning=str(e))

    async def _send(self, message: str) -> None:
        channel = self._cfg.default_channel
        chat_id = self._cfg.default_chat_id
        if not channel or not chat_id:
            logger.warning("[proactive] default_channel/chat_id 未配置，跳过发送")
            return
        try:
            await self._push.execute(channel=channel, chat_id=chat_id, message=message)
            logger.info(f"[proactive] 已发送主动消息 → {channel}:{chat_id}")
        except Exception as e:
            logger.error(f"[proactive] 发送失败: {e}")


# ── helpers ──────────────────────────────────────────────────────

def _format_items(items: list[FeedItem]) -> str:
    if not items:
        return ""
    lines = []
    for item in items:
        pub = ""
        if item.published_at:
            try:
                pub = " (" + item.published_at.astimezone().strftime("%m-%d %H:%M") + ")"
            except Exception:
                pass
        title = item.title or "(无标题)"
        lines.append(f"[{item.source_name}]{pub} {title}")
        if item.content:
            lines.append(f"  {item.content[:200]}")
        if item.url:
            lines.append(f"  {item.url}")
    return "\n".join(lines)


def _format_recent(msgs: list[dict]) -> str:
    if not msgs:
        return ""
    lines = []
    for m in msgs[-10:]:   # 最多展示最近 10 条
        role = "用户" if m["role"] == "user" else "助手"
        content = str(m.get("content", ""))[:150]
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _parse_decision(text: str) -> _Decision:
    """从 LLM 输出中提取 JSON 决策。"""
    # 先尝试提取 ```json ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    raw = match.group(1) if match else text

    # 找第一个完整的 { ... }
    brace_match = re.search(r"\{[\s\S]*\}", raw)
    if not brace_match:
        logger.warning(f"[proactive] 无法提取 JSON: {text[:200]!r}")
        return _Decision(score=0.0, should_send=False, message="", reasoning="parse error")

    try:
        d = json.loads(brace_match.group())
        return _Decision(
            score=float(d.get("score", 0.0)),
            should_send=bool(d.get("should_send", False)),
            message=str(d.get("message", "")),
            reasoning=str(d.get("reasoning", "")),
        )
    except Exception as e:
        logger.warning(f"[proactive] JSON 解析失败: {e}  raw={raw[:200]!r}")
        return _Decision(score=0.0, should_send=False, message="", reasoning=str(e))
