from __future__ import annotations

import base64
import json
import logging
import mimetypes
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.core.llm_provider import LLMProvider
from agent.core.runtime_prompts import _PRE_FLIGHT_PROMPT, _REFLECT_PROMPT
from agent.core.types import ContextBundle, InboundMessage, ReasonerResult
from agent.procedure_hint import (
    _match_procedure_items,
    build_intercept_hint,
    build_procedure_hint,
)
from agent.tool_runtime import (
    append_assistant_tool_calls,
    append_tool_result,
    prepare_toolset,
    tool_call_signature,
)
from agent.tools.base import normalize_tool_result
from agent.tools.tool_search import _excluded_names_ctx

if TYPE_CHECKING:
    from agent.core.runtime_support import ToolDiscoveryState
    from agent.tools.registry import ToolRegistry
    from agent.tools.base import Tool
    from core.memory.port import MemoryPort

logger = logging.getLogger("agent.core.reasoner")
_TOOL_LOOP_REPEAT_LIMIT = 3
_SUMMARY_MAX_TOKENS = 512


class Reasoner(ABC):
    """
    ┌──────────────────────────────────────┐
    │ Reasoner                             │
    ├──────────────────────────────────────┤
    │ 1. 调 LLM 单步                       │
    │ 2. 执行 tool call                    │
    │ 3. 回填结果继续推理                  │
    │ 4. 汇总最终 reply                    │
    └──────────────────────────────────────┘
    """

    @abstractmethod
    async def run(
        self,
        *,
        msg: InboundMessage,
        system_prompt: str,
        context: ContextBundle,
        tools: list[Tool],
    ) -> ReasonerResult:
        """执行完整推理循环"""


class DefaultReasoner(Reasoner):
    """
    ┌──────────────────────────────────────┐
    │ DefaultReasoner                      │
    ├──────────────────────────────────────┤
    │ 1. 组装初始 messages                 │
    │ 2. 调用 LLMProvider.step()           │
    │ 3. 执行本轮 tool calls               │
    │ 4. 回填 tool result                  │
    │ 5. 直到得到最终 reply 或触发收尾     │
    └──────────────────────────────────────┘
    """

    def __init__(
        self,
        *,
        llm_provider: LLMProvider,
        max_iterations: int,
        max_tokens: int,
        tool_registry: "ToolRegistry | None" = None,
        tool_search_enabled: bool = False,
        memory_port: "MemoryPort | None" = None,
        tool_discovery: "ToolDiscoveryState | None" = None,
    ) -> None:
        self._llm_provider = llm_provider
        self._max_iterations = max_iterations
        self._max_tokens = max_tokens
        self._tool_registry = tool_registry
        self._tool_search_enabled = tool_search_enabled
        self._memory_port = memory_port
        self._tool_discovery = tool_discovery

    async def run(
        self,
        *,
        msg: InboundMessage,
        system_prompt: str,
        context: ContextBundle,
        tools: list[Tool],
    ) -> ReasonerResult:
        # 1. 准备工具集合和初始消息
        prepared = prepare_toolset(tools)
        messages = self._build_initial_messages(msg, context)
        tools_used: list[str] = []
        tool_chain: list[dict] = []
        last_signature = ""
        repeat_count = 0
        injected_proc_ids: set[str] = set()

        # 2. 初始化工具可见性
        visible_names: set[str] | None = None
        if self._tool_search_enabled and self._tool_registry is not None:
            always_on = self._tool_registry.get_always_on_names()
            preloaded = (
                self._tool_discovery.get_preloaded(msg.session_key)
                if self._tool_discovery is not None
                else set()
            )
            visible_names = always_on | preloaded

        # 3. 设置工具上下文
        if self._tool_registry is not None:
            self._tool_registry.set_context(
                channel=msg.channel,
                chat_id=_chat_id_from_session_key(msg.session_key, msg.channel),
            )

        # 4. 注入 preflight guard
        messages.append(
            {
                "role": "system",
                "content": _build_preflight_prompt(
                    request_time=msg.timestamp,
                    tool_registry=self._tool_registry,
                    tool_search_enabled=self._tool_search_enabled,
                    visible_names=visible_names,
                ),
            }
        )

        for iteration in range(self._max_iterations):
            # 5. 调用单轮 LLM
            response = await self._llm_provider.step(
                system_prompt=system_prompt,
                messages=messages,
                tools=_visible_tools(prepared.tools, visible_names),
            )

            # 6. 有工具调用时执行本轮工具
            if response.tool_calls:
                signature = tool_call_signature(response.tool_calls)
                if signature and signature == last_signature:
                    repeat_count += 1
                else:
                    last_signature = signature
                    repeat_count = 1

                if repeat_count >= _TOOL_LOOP_REPEAT_LIMIT:
                    return await self._summarize_incomplete_progress(
                        system_prompt=system_prompt,
                        messages=messages,
                        tools_used=tools_used,
                        tool_chain=tool_chain,
                        reason="tool_loop",
                        thinking=response.thinking,
                        session_key=msg.session_key,
                    )

                append_assistant_tool_calls(
                    messages,
                    content=response.reply,
                    tool_calls=response.tool_calls,
                )

                iter_calls: list[dict] = []
                pending_hints: list[str] = []
                for tool_call in response.tool_calls:
                    # 6.1 工具执行前先做 procedure/SOP 拦截判断
                    all_items = _match_procedure_items(
                        memory=self._memory_port,
                        tool_name=tool_call.name,
                        tool_arguments=dict(tool_call.arguments),
                        logger=logger,
                    )
                    intercept_items = [
                        item
                        for item in all_items
                        if bool(item.get("intercept", False))
                        and str(item.get("id", "")) not in injected_proc_ids
                    ]
                    if intercept_items:
                        result_text = build_intercept_hint(intercept_items, tool_call.name)
                        injected_proc_ids.update(
                            str(item.get("id", "")) for item in intercept_items
                        )
                        normalized = normalize_tool_result(result_text)
                        append_tool_result(
                            messages,
                            tool_call_id=tool_call.id,
                            content=normalized,
                            tool_name=tool_call.name,
                        )
                        iter_calls.append(
                            {
                                "call_id": tool_call.id,
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                                "result": normalized.preview(),
                            }
                        )
                        continue

                    # 6.2 执行实际工具
                    result_text = await self._run_tool_call(
                        tool_call=tool_call,
                        prepared_tools=prepared.tool_map,
                        visible_names=visible_names,
                        tools_used=tools_used,
                        msg=msg,
                    )
                    normalized = normalize_tool_result(result_text)
                    append_tool_result(
                        messages,
                        tool_call_id=tool_call.id,
                        content=normalized,
                        tool_name=tool_call.name,
                    )
                    iter_calls.append(
                        {
                            "call_id": tool_call.id,
                            "name": tool_call.name,
                            "arguments": tool_call.arguments,
                            "result": normalized.preview(),
                        }
                    )

                    if (
                        tool_call.name == "tool_search"
                        and visible_names is not None
                        and normalized.text
                    ):
                        _unlock_from_tool_search(normalized.text, visible_names)

                    # 6.3 工具执行后补 procedure 提示
                    hint_items = [
                        item for item in all_items if not bool(item.get("intercept", False))
                    ]
                    raw_hint, new_ids = build_procedure_hint(
                        hint_items,
                        injected_proc_ids,
                    )
                    if new_ids:
                        injected_proc_ids.update(new_ids)
                        if raw_hint:
                            pending_hints.append(raw_hint.split("\n", 1)[1])

                tool_chain.append({"text": response.reply or "", "calls": iter_calls})
                messages.append(
                    {
                        "role": "system",
                        "content": _build_reflect_prompt(
                            pending_hints=pending_hints,
                            visible_names=visible_names,
                            always_on_names=(
                                self._tool_registry.get_always_on_names()
                                if self._tool_search_enabled
                                and self._tool_registry is not None
                                else None
                            ),
                        ),
                    }
                )
                continue

            # 7. 没有工具调用时直接结束
            reply = (response.reply or "").strip() or "（无响应）"
            self._update_tool_discovery(msg.session_key, tools_used)
            return ReasonerResult(
                reply=reply,
                invocations=_flatten_invocations(tool_chain),
                thinking=response.thinking,
                metadata={
                    "tools_used": tools_used,
                    "tool_chain": tool_chain,
                    "context_retry": {},
                    "retrieval_raw": context.metadata.get("retrieval_raw"),
                },
            )

        # 8. 达到最大轮次后收尾
        return await self._summarize_incomplete_progress(
            system_prompt=system_prompt,
            messages=messages,
            tools_used=tools_used,
            tool_chain=tool_chain,
            reason="max_iterations",
            thinking=None,
            session_key=msg.session_key,
        )

    def _build_initial_messages(
        self,
        msg: InboundMessage,
        context: ContextBundle,
    ) -> list[dict[str, Any]]:
        # 1. 先放历史消息
        messages = [{"role": item.role, "content": item.content} for item in context.history]

        # 2. 再追加当前用户消息
        messages.append(
            {
                "role": "user",
                "content": _build_user_content(msg),
            }
        )
        return messages

    async def _run_tool_call(
        self,
        *,
        tool_call: Any,
        prepared_tools: dict[str, Tool],
        visible_names: set[str] | None,
        tools_used: list[str],
        msg: InboundMessage,
    ) -> str | Any:
        # 1. deferred 工具未解锁时返回 select: 引导
        if visible_names is not None and tool_call.name not in visible_names:
            return (
                f"工具 '{tool_call.name}' 当前未加载（schema 不可见）。"
                f"请先调用 tool_search(query=\"select:{tool_call.name}\") 加载，"
                "然后再调用该工具。不要放弃当前任务。"
            )

        # 2. 工具不存在时返回错误
        tool = prepared_tools.get(tool_call.name)
        if tool is None:
            return (
                f"工具 '{tool_call.name}' 不存在。"
                f"请调用 tool_search(query=\"select:{tool_call.name}\") 或使用关键词搜索。"
            )

        # 3. 执行工具
        tools_used.append(tool_call.name)
        if self._tool_registry is not None:
            if tool_call.name == "tool_search" and visible_names is not None:
                token = _excluded_names_ctx.set(visible_names)
            else:
                token = None
            try:
                return await self._tool_registry.execute(
                    tool_call.name,
                    dict(tool_call.arguments),
                )
            finally:
                if token is not None:
                    _excluded_names_ctx.reset(token)

        return await tool.execute(
            channel=msg.channel,
            chat_id=_chat_id_from_session_key(msg.session_key, msg.channel),
            **dict(tool_call.arguments),
        )

    async def _summarize_incomplete_progress(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools_used: list[str],
        tool_chain: list[dict],
        reason: str,
        thinking: str | None,
        session_key: str,
    ) -> ReasonerResult:
        # 1. 先尝试让模型基于当前上下文生成收尾摘要
        summary_messages = [
            *messages,
            {
                "role": "system",
                "content": (
                    f"[收尾原因] {reason}\n"
                    f"[已调用工具] {', '.join(tools_used[-8:]) if tools_used else '无'}\n"
                    "请用简短中文总结已完成进度、未完成部分和建议下一步。"
                ),
            },
        ]
        try:
            response = await self._llm_provider.step(
                system_prompt=system_prompt,
                messages=summary_messages,
                tools=[],
            )
            reply = (response.reply or "").strip()
            if reply:
                self._update_tool_discovery(session_key, tools_used)
                return ReasonerResult(
                    reply=reply,
                    invocations=_flatten_invocations(tool_chain),
                    thinking=thinking or response.thinking,
                    metadata={
                        "tools_used": tools_used,
                        "tool_chain": tool_chain,
                        "context_retry": {},
                        "retrieval_raw": None,
                    },
                )
        except Exception as exc:
            logger.warning("reasoner summarize failed: %s", exc)

        # 2. 模型收尾失败时返回固定兜底
        self._update_tool_discovery(session_key, tools_used)
        return ReasonerResult(
            reply=(
                f"这次任务还没完全收束，原因是 {reason}。"
                "我已经保留当前进度，下一步可以继续补齐缺失信息。"
            ),
            invocations=_flatten_invocations(tool_chain),
            thinking=thinking,
            metadata={
                "tools_used": tools_used,
                "tool_chain": tool_chain,
                "context_retry": {},
                "retrieval_raw": None,
            },
        )

    def _update_tool_discovery(self, session_key: str, tools_used: list[str]) -> None:
        # 1. 仅在 tool_search 模式下维护 LRU
        if (
            not self._tool_search_enabled
            or self._tool_registry is None
            or self._tool_discovery is None
            or not tools_used
        ):
            return

        # 2. 写回本轮已使用工具
        self._tool_discovery.update(
            session_key,
            tools_used,
            self._tool_registry.get_always_on_names(),
        )


def _build_user_content(msg: InboundMessage) -> str | list[dict[str, Any]]:
    # 1. 无附件时直接返回纯文本
    if not msg.media:
        return msg.content

    # 2. 将图片附件转成 image_url blocks
    images: list[dict[str, Any]] = []
    for item in msg.media:
        raw = str(item)
        if raw.startswith(("http://", "https://")):
            images.append({"type": "image_url", "image_url": {"url": raw}})
            continue

        path = Path(raw)
        mime, _ = mimetypes.guess_type(path)
        if not path.is_file() or not mime or not mime.startswith("image/"):
            continue
        with path.open("rb") as fp:
            b64 = base64.b64encode(fp.read()).decode()
        images.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        )

    # 3. 有图片时按 OpenAI 多模态格式返回
    if images:
        return [*images, {"type": "text", "text": msg.content}]

    # 4. 非图片附件按旧逻辑退回纯文本
    return msg.content


def _visible_tools(tools: list[Tool], visible_names: set[str] | None) -> list[Tool]:
    if visible_names is None:
        return tools
    return [tool for tool in tools if tool.name in visible_names]


def _unlock_from_tool_search(result: str, visible_names: set[str]) -> None:
    try:
        data = json.loads(result)
        for item in data.get("matched", []):
            name = item.get("name")
            if isinstance(name, str) and name:
                visible_names.add(name)
    except Exception:
        return


def _build_preflight_prompt(
    *,
    request_time: datetime | None,
    tool_registry: "ToolRegistry | None",
    tool_search_enabled: bool,
    visible_names: set[str] | None,
) -> str:
    anchor = _format_request_time_anchor(request_time)
    if not tool_search_enabled or tool_registry is None:
        return f"【本轮时间锚点】{anchor}\n" + _PRE_FLIGHT_PROMPT

    deferred = tool_registry.get_deferred_names(visible=visible_names)
    builtin = deferred.get("builtin", [])
    mcp = deferred.get("mcp", {})
    lines = [
        f"【本轮时间锚点】{anchor}",
        "所有时间相关判断必须与该锚点一致；无法验证时必须明确不确定。",
        "",
    ]
    if builtin or mcp:
        lines.append("【未加载工具目录（知道名字但 schema 未暴露）】")
        if builtin:
            lines.append(f"内置: {', '.join(builtin)}")
        for server, names in mcp.items():
            lines.append(f"MCP ({server}): {', '.join(names)}")
        lines.append(
            '已知工具名但不可见时，请先调用 tool_search(query="select:工具名")。'
        )
        lines.append("")
    lines.append(_PRE_FLIGHT_PROMPT)
    return "\n".join(lines)


def _build_reflect_prompt(
    *,
    pending_hints: list[str],
    visible_names: set[str] | None,
    always_on_names: set[str] | None,
) -> str:
    tool_state_hint = ""
    if visible_names is not None and always_on_names is not None:
        unlocked_extra = visible_names - always_on_names - {"tool_search"}
        if unlocked_extra:
            tool_state_hint = (
                f"【当前会话已额外解锁工具: {', '.join(sorted(unlocked_extra))}】\n"
            )
        else:
            tool_state_hint = (
                "【当前仅 always-on 工具可见】\n"
                "若需其他工具：已知工具名 → tool_search(query=\"select:工具名\") 加载；"
                "不知道工具名 → tool_search(query=\"关键词\") 搜索。\n"
            )

    if not pending_hints:
        return tool_state_hint + _REFLECT_PROMPT

    combined = "\n".join(h for h in pending_hints if h.strip())
    if not combined.strip():
        return tool_state_hint + _REFLECT_PROMPT
    return (
        "【⚠️ 操作规范提醒 | 适用于本轮工具调用】\n"
        f"{combined}\n\n---\n\n"
        + tool_state_hint
        + _REFLECT_PROMPT
    )


def _format_request_time_anchor(ts: datetime | None) -> str:
    if ts is None:
        ts = datetime.now().astimezone()
    elif ts.tzinfo is None:
        ts = ts.astimezone()
    return f"request_time={ts.isoformat()} ({ts.strftime('%Y-%m-%d %H:%M:%S %Z')})"


def _flatten_invocations(tool_chain: list[dict]) -> list[Any]:
    from agent.core.types import ToolCall

    invocations: list[ToolCall] = []
    for group in tool_chain:
        for call in group.get("calls") or []:
            if not isinstance(call, dict):
                continue
            invocations.append(
                ToolCall(
                    id=str(call.get("call_id", "") or ""),
                    name=str(call.get("name", "") or ""),
                    arguments=call.get("arguments")
                    if isinstance(call.get("arguments"), dict)
                    else {},
                )
            )
    return invocations


def _chat_id_from_session_key(session_key: str, channel: str) -> str:
    prefix = f"{channel}:"
    if session_key.startswith(prefix):
        return session_key[len(prefix) :]
    if ":" in session_key:
        return session_key.split(":", 1)[1]
    return session_key
