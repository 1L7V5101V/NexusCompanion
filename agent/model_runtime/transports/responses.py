from __future__ import annotations

import asyncio
import uuid
from typing import Any, cast

import httpx
import openai
from openai import AsyncOpenAI

from agent.model_runtime.auth.codex import (
    CODEX_API_BASE,
    CODEX_CLIENT_VERSION,
    CodexAuthDriver,
)
from agent.model_runtime.errors import (
    AuthenticationError,
    ContextWindowError,
    QuotaError,
    RateLimitError,
    RetryableTransportError,
    TransportError,
)
from agent.model_runtime.transports.responses_converters import (
    _dump,
    _field,
    _normalize_effort,
    _normalize_tool_choice,
    _parse_usage,
    _responses_input,
    _responses_lite_input,
    _responses_tools,
    _sanitize_replay_item,
    _tool_call,
)
from agent.model_runtime.types import LLMResponse, ModelRequest


class CodexResponsesTransport:
    """把规范化模型请求映射到 Codex Responses 流。"""

    def __init__(
        self,
        auth: CodexAuthDriver,
        *,
        runtime_id: str,
        base_url: str = CODEX_API_BASE,
        read_timeout_s: float = 120,
        use_responses_lite: bool = False,
        supports_parallel_tool_calls: bool = True,
        reasoning_summary: str = "none",
    ) -> None:
        self.auth = auth
        self.runtime_id = runtime_id
        self.base_url = base_url
        self.use_responses_lite = use_responses_lite
        self.supports_parallel_tool_calls = supports_parallel_tool_calls
        self.reasoning_summary = reasoning_summary
        self.installation_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.thread_id = str(uuid.uuid4())
        self.window_id = str(uuid.uuid4())
        self.network_timeout = httpx.Timeout(
            connect=30,
            read=read_timeout_s,
            write=30,
            pool=30,
        )

    async def send(self, request: ModelRequest) -> LLMResponse:
        try:
            return await self._send_once(request, force_refresh=False)
        except AuthenticationError:
            return await self._send_once(request, force_refresh=True)

    async def _send_once(
        self, request: ModelRequest, *, force_refresh: bool
    ) -> LLMResponse:
        headers = await asyncio.to_thread(self.auth.headers, force_refresh=force_refresh)
        default_headers = {
            "ChatGPT-Account-ID": headers.get("ChatGPT-Account-ID", ""),
            "originator": "codex_cli_rs",
            "User-Agent": f"codex_cli_rs/{CODEX_CLIENT_VERSION}",
            "x-codex-installation-id": self.installation_id,
            "session-id": self.session_id,
            "thread-id": self.thread_id,
            "x-codex-window-id": self.window_id,
        }
        if self.use_responses_lite:
            default_headers["x-openai-internal-codex-responses-lite"] = "true"
        client = AsyncOpenAI(
            api_key=headers["Authorization"].removeprefix("Bearer "),
            base_url=self.base_url,
            default_headers=default_headers,
            timeout=self.network_timeout,
            max_retries=0,
        )
        try:
            stream = await client.responses.create(**self._build_payload(request))
            return await self._consume_stream(cast(Any, stream), request)
        except openai.APIStatusError as exc:
            status_code = exc.status_code
            error_text = str(exc).lower()
            if status_code == 401:
                raise AuthenticationError("Codex 请求认证失败") from exc
            if status_code == 429:
                if any(
                    marker in error_text
                    for marker in ("insufficient_quota", "quota exceeded", "billing")
                ):
                    raise QuotaError("Codex 账号额度不足") from exc
                raise RateLimitError("Codex 请求被限流") from exc
            if status_code == 400 and any(
                marker in error_text
                for marker in ("context_length", "context window", "too many tokens")
            ):
                raise ContextWindowError("Codex 请求超过上下文窗口") from exc
            raise
        except (openai.APIConnectionError, openai.APITimeoutError) as exc:
            raise RetryableTransportError("Codex Responses 连接失败") from exc
        finally:
            await client.close()

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        messages, instructions = _responses_input(
            request.messages,
            request.system_prompt,
            runtime_id=self.runtime_id,
            model=request.model,
        )
        tools = _responses_tools(request.tools)
        tool_choice, tools = _normalize_tool_choice(request.tool_choice, tools)
        if self.use_responses_lite:
            messages = _responses_lite_input(messages, instructions, tools)
            instructions = ""
        payload: dict[str, Any] = {
            "model": request.model,
            "instructions": instructions,
            "input": messages,
            "extra_body": {
                "client_metadata": {
                    "x-codex-installation-id": self.installation_id,
                    "session-id": self.session_id,
                    "thread-id": self.thread_id,
                    "x-codex-window-id": self.window_id,
                }
            },
            "tool_choice": tool_choice,
            "parallel_tool_calls": (
                self.supports_parallel_tool_calls and not self.use_responses_lite
            ),
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        reasoning: dict[str, str] = {}
        if request.reasoning_effort:
            reasoning["effort"] = _normalize_effort(request.reasoning_effort)
        if self.reasoning_summary != "none":
            reasoning["summary"] = self.reasoning_summary
        if self.use_responses_lite:
            reasoning["context"] = "all_turns"
        if reasoning:
            payload["reasoning"] = reasoning
        if tools and not self.use_responses_lite:
            payload["tools"] = tools
        cache_key = request.prompt_cache_key or self.thread_id
        if cache_key:
            payload["prompt_cache_key"] = cache_key
        return payload

    async def _consume_stream(self, stream: Any, request: ModelRequest) -> LLMResponse:
        """消费 SSE 事件并保留后续重放必需的 output item。"""
        content: list[str] = []
        thinking: list[str] = []
        tool_args: dict[str, dict[str, str]] = {}
        output_items: list[dict[str, Any]] = []
        usage: ModelUsage | None = None
        completed = False
        iterator = aiter(stream)
        while True:
            try:
                event = await anext(iterator)
            except StopAsyncIteration:
                break
            event_type = str(_field(event, "type") or "")
            delta = _field(event, "delta")
            if event_type == "response.output_text.delta" and isinstance(delta, str):
                content.append(delta)
                if request.on_delta:
                    await request.on_delta({"content_delta": delta})
            elif event_type == "response.output_text.done":
                done_text = _field(event, "text")
                current = "".join(content)
                if isinstance(done_text, str) and done_text.startswith(current):
                    suffix = done_text[len(current) :]
                    if suffix:
                        content.append(suffix)
                        if request.on_delta:
                            await request.on_delta({"content_delta": suffix})
            elif event_type == "response.reasoning_summary_text.delta" and isinstance(delta, str):
                thinking.append(delta)
                if request.on_delta:
                    await request.on_delta({"thinking_delta": delta})
            elif event_type == "response.reasoning_text.delta" and isinstance(delta, str):
                thinking.append(delta)
                if request.on_delta:
                    await request.on_delta({"thinking_delta": delta})
            elif event_type == "response.reasoning_summary_text.done":
                done_text = _field(event, "text")
                current = "".join(thinking)
                if isinstance(done_text, str) and done_text.startswith(current):
                    suffix = done_text[len(current) :]
                    if suffix:
                        thinking.append(suffix)
                        if request.on_delta:
                            await request.on_delta({"thinking_delta": suffix})
            elif event_type == "response.function_call_arguments.delta":
                item_id = str(_field(event, "item_id") or _field(event, "output_index") or "")
                slot = tool_args.setdefault(item_id, {"arguments": ""})
                slot["arguments"] += str(delta or "")
            elif event_type == "response.output_item.done":
                item = _dump(_field(event, "item"))
                if item:
                    if item.get("type") == "reasoning":
                        output_items.append(_sanitize_replay_item(item))
                    if item.get("type") == "function_call":
                        item_id = str(item.get("id") or item.get("call_id") or "")
                        tool_args[item_id] = {
                            "id": str(item.get("call_id") or item_id),
                            "name": str(item.get("name") or ""),
                            "arguments": str(item.get("arguments") or "{}"),
                        }
            elif event_type == "response.completed":
                response = _field(event, "response")
                usage = _parse_usage(_field(response, "usage"))
                completed = True
                break
            elif event_type in {"response.failed", "response.incomplete"}:
                response = _field(event, "response")
                error = _field(response, "error") or _field(response, "incomplete_details")
                _raise_stream_error(error)
        if not completed:
            raise RetryableTransportError("Codex Responses 在 completed 事件前断流")
        calls = [_tool_call(value) for value in tool_args.values() if value.get("name")]
        model_state = {
            "schema_version": 1,
            "runtime_id": self.runtime_id,
            "transport": "responses",
            "model": request.model,
            "items": output_items,
        }
        return LLMResponse(
            content="".join(content).strip() or None,
            tool_calls=calls,
            thinking="".join(thinking).strip() or None,
            provider_fields={"model_state": model_state},
            cache_prompt_tokens=usage.input_tokens if usage else None,
            cache_hit_tokens=usage.cached_input_tokens if usage else None,
            usage=usage,
        )


def _raise_stream_error(error: Any) -> None:
    code = str(_field(error, "code") or "").lower()
    message = str(_field(error, "message") or error or "未知错误")
    if code in {"context_length_exceeded", "context_window_exceeded"}:
        raise ContextWindowError(f"Codex 请求超过上下文窗口: {message}")
    if code in {"insufficient_quota", "usage_not_included"}:
        raise QuotaError(f"Codex 账号额度不足: {message}")
    if code in {"rate_limit_exceeded", "rate_limit_error"}:
        raise RateLimitError(f"Codex 请求受限: {message}")
    if code in {"invalid_prompt", "bio_policy", "cyber_policy", "policy_violation"}:
        raise TransportError(f"Codex Responses 请求被拒绝 code={code}: {message}")
    raise RetryableTransportError(
        f"Codex Responses 暂时失败 code={code or '-'}: {message}"
    )
