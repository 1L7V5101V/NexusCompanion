"""Responses 协议纯转换函数。

从 transports/responses.py 抽出,保持无 auth/fcntl 依赖(Windows 可导入),
供 CodexResponsesTransport 与 agent.provider 的 Codex 协议路径共用。
"""

from __future__ import annotations

import json
from typing import Any, cast

from agent.model_runtime.errors import TransportError
from agent.model_runtime.types import ModelUsage, ToolCall, UsageCoverage


def _responses_input(
    messages: list[dict],
    system_prompt: str,
    *,
    runtime_id: str = "",
    model: str = "",
) -> tuple[list[dict], str]:
    """转换 Chat 历史,并原样重放同 transport 的 opaque item。"""
    result: list[dict] = []
    instructions = system_prompt
    for message in messages:
        role = message.get("role")
        if role == "system":
            instructions = f"{instructions}\n\n{message.get('content', '')}".strip()
            continue
        state = message.get("model_state")
        if _matches_continuation(state, runtime_id=runtime_id, model=model):
            items = state.get("items")
            if isinstance(items, list):
                result.extend(
                    _sanitize_replay_item(item)
                    for item in items
                    if isinstance(item, dict)
                )
        if role == "tool":
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        tool_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list):
            for call in tool_calls:
                function = call.get("function") or {}
                result.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or "{}"),
                    }
                )
        content = message.get("content")
        if content not in (None, ""):
            result.append({"role": role, "content": _responses_content(role, content)})
    return result, instructions


def _matches_continuation(value: object, *, runtime_id: str, model: str) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        value.get("schema_version") == 1
        and value.get("runtime_id") == runtime_id
        and value.get("transport") == "responses"
        and value.get("model") == model
    )


def _responses_content(role: object, content: object) -> object:
    """按消息角色把 Chat content blocks 转为 Responses blocks。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise TransportError("消息 content 必须是字符串或数组")
    converted: list[dict[str, Any]] = []
    for raw in content:
        if not isinstance(raw, dict):
            raise TransportError("消息 content block 必须是对象")
        block_type = raw.get("type")
        if block_type in {"input_text", "output_text", "input_image"}:
            converted.append(raw)
        elif block_type == "text":
            target = "output_text" if role == "assistant" else "input_text"
            converted.append({"type": target, "text": str(raw.get("text") or "")})
        elif block_type == "image_url" and role == "user":
            image = raw.get("image_url")
            image_url = image.get("url") if isinstance(image, dict) else image
            if not isinstance(image_url, str) or not image_url:
                raise TransportError("image_url block 缺少 URL")
            item: dict[str, Any] = {"type": "input_image", "image_url": image_url}
            if isinstance(image, dict) and image.get("detail"):
                item["detail"] = image["detail"]
            converted.append(item)
        else:
            raise TransportError(f"Responses 不支持的 content block: {block_type}")
    return converted


def _responses_tools(tools: list[dict]) -> list[dict]:
    result: list[dict] = []
    for tool in tools:
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, dict) or not function.get("name"):
            raise TransportError("工具 schema 缺少函数名")
        result.append(
            {
                "type": "function",
                "name": function["name"],
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {"type": "object", "properties": {}}),
                "strict": bool(function.get("strict", False)),
            }
        )
    return result


def _normalize_tool_choice(
    tool_choice: str | dict[str, Any], tools: list[dict]
) -> tuple[str, list[dict]]:
    """把 Chat Completions 工具选择收敛为 Codex Responses 字符串契约。"""
    if isinstance(tool_choice, str):
        if tool_choice not in {"auto", "none", "required"}:
            raise TransportError(f"Responses 不支持的 tool_choice: {tool_choice}")
        return tool_choice, tools
    function = tool_choice.get("function")
    name = function.get("name") if isinstance(function, dict) else tool_choice.get("name")
    if tool_choice.get("type") != "function" or not isinstance(name, str) or not name:
        raise TransportError("Responses 命名 tool_choice 结构无效")
    selected = [tool for tool in tools if tool.get("name") == name]
    if not selected:
        raise TransportError(f"Responses 命名 tool_choice 引用了未知工具: {name}")
    return "required", selected


def _responses_lite_input(
    messages: list[dict], instructions: str, tools: list[dict]
) -> list[dict]:
    """按 Codex Responses Lite 契约内嵌工具和开发者指令。"""
    prefix: list[dict] = [
        {"type": "additional_tools", "role": "developer", "tools": tools}
    ]
    if instructions:
        prefix.append(
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": instructions}],
            }
        )
    return [*prefix, *_strip_image_details(messages)]


def _strip_image_details(items: list[dict]) -> list[dict]:
    """复制 Lite input,并移除后端不接受的图片 detail。"""
    copied = json.loads(json.dumps(items, ensure_ascii=False))
    for item in copied:
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "input_image":
                _ = block.pop("detail", None)
    return cast(list[dict], copied)


def _sanitize_replay_item(item: dict[str, Any]) -> dict[str, Any]:
    """只保留 reasoning 重放契约允许的字段。"""
    if item.get("type") != "reasoning":
        raise TransportError(f"Responses continuation 包含不支持的 item: {item.get('type')}")
    allowed = {"type", "summary", "content", "encrypted_content"}
    return {key: value for key, value in item.items() if key in allowed}


def _normalize_effort(value: str) -> str:
    return {"ultra": "max"}.get(value, value)


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _dump(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    dumped = value.model_dump(mode="json")
    return cast(dict[str, Any], dumped)


def _tool_call(raw: dict[str, str]) -> ToolCall:
    try:
        arguments = json.loads(raw.get("arguments") or "{}")
    except json.JSONDecodeError as exc:
        raise TransportError("Codex 工具调用参数不是有效 JSON") from exc
    if not isinstance(arguments, dict):
        raise TransportError("Codex 工具调用参数必须是 JSON 对象")
    return ToolCall(id=raw.get("id", ""), name=raw["name"], arguments=arguments)


def _parse_usage(raw: Any) -> ModelUsage | None:
    if raw is None:
        return None
    input_tokens = _field(raw, "input_tokens")
    output_tokens = _field(raw, "output_tokens")
    input_details = _field(raw, "input_tokens_details")
    output_details = _field(raw, "output_tokens_details")
    return ModelUsage(
        input_tokens=int(input_tokens) if input_tokens is not None else None,
        cached_input_tokens=_optional_int(_field(input_details, "cached_tokens")),
        output_tokens=int(output_tokens) if output_tokens is not None else None,
        reasoning_output_tokens=_optional_int(_field(output_details, "reasoning_tokens")),
        covered_request_count=1 if input_tokens is not None and output_tokens is not None else 0,
        coverage=(
            UsageCoverage.EXACT
            if input_tokens is not None and output_tokens is not None
            else UsageCoverage.PARTIAL
            if input_tokens is not None or output_tokens is not None
            else UsageCoverage.UNAVAILABLE
        ),
    )


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None
