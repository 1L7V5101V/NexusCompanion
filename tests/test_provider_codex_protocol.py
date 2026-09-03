"""
LLMProvider Codex（Responses API）协议路径单元测试。

覆盖：
  1. 消息/工具/tool_choice 转换（responses_converters 接线）
  2. SSE 事件流解析（content/thinking/tool_calls/usage）
  3. 错误映射（context length / policy / 断流 / 流 idle 超时）
  4. _create_with_retry 泛化后默认 chat completions 路径行为不变
  5. 配置层 protocol 字段解析与构造点接线
"""

import asyncio
import tomllib
from types import SimpleNamespace
from typing import Any

import pytest

from agent.config import load_config
from agent.provider import (
    ContextLengthError,
    ContentSafetyError,
    LLMNetworkTimeoutError,
    LLMProvider,
)
from agent.model_runtime.transports.responses_converters import (
    _normalize_tool_choice,
    _responses_input,
    _responses_tools,
)


# ── 测试工具 ──────────────────────────────────────────────────────────────────


class _FakeStream:
    def __init__(self, events: list[dict]):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _HangingStream:
    """模拟流卡死（idle 超时场景）。"""

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)
        raise StopAsyncIteration  # pragma: no cover


def _make_provider(**kwargs: Any) -> LLMProvider:
    return LLMProvider(
        api_key="test-key",
        protocol="codex",
        payload_snapshot_enabled=False,
        **kwargs,
    )


def _install_fake_create(
    provider: LLMProvider,
    fake_create: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # conftest 的 openai stub 客户端没有 responses 资源，直接注入 fake
    responses = SimpleNamespace(create=fake_create)
    monkeypatch.setattr(provider._client, "responses", responses, raising=False)


# ── 转换函数 ──────────────────────────────────────────────────────────────────


def test_responses_input_tool_history_roundtrip():
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "查天气"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "北京"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "晴 25 度"},
    ]
    items, instructions = _responses_input(messages, "")
    assert "你是助手" in instructions
    # user 消息以 role 形式存在，工具轮次是 function_call → function_call_output
    assert items[0]["role"] == "user"
    assert items[1]["type"] == "function_call"
    assert items[2]["type"] == "function_call_output"
    assert items[1]["call_id"] == "call_1"
    assert items[1]["name"] == "get_weather"
    assert items[2]["output"] == "晴 25 度"


def test_responses_input_no_placeholder_for_empty_assistant_content():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "t", "arguments": "{}"},
                }
            ],
        },
    ]
    items, _ = _responses_input(messages, "")
    # 空content的assistant消息不应产生占位文本item
    assert [item["type"] for item in items] == ["function_call"]


def test_responses_tools_flatten():
    chat_tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查天气",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    converted = _responses_tools(chat_tools)
    assert converted[0]["type"] == "function"
    assert converted[0]["name"] == "get_weather"
    assert converted[0]["description"] == "查天气"
    assert converted[0]["strict"] is False


def test_normalize_tool_choice_variants():
    tools = [{"type": "function", "name": "a"}, {"type": "function", "name": "b"}]
    assert _normalize_tool_choice("auto", tools) == ("auto", tools)
    assert _normalize_tool_choice("none", tools) == ("none", tools)
    choice, selected = _normalize_tool_choice(
        {"type": "function", "function": {"name": "b"}}, tools
    )
    assert choice == "required"
    assert selected == [{"type": "function", "name": "b"}]


# ── Codex 路径 payload 与流消费 ───────────────────────────────────────────────


def test_chat_responses_payload_and_stream(monkeypatch):
    provider = _make_provider(system_prompt="sys")
    captured: dict = {}

    async def fake_create(**payload):
        captured.update(payload)
        return _FakeStream(
            [
                {"type": "response.output_text.delta", "delta": "你好"},
                {"type": "response.output_text.done", "text": "你好，世界"},
                {
                    "type": "response.completed",
                    "response": {
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 10,
                            "input_tokens_details": {"cached_tokens": 40},
                        }
                    },
                },
            ]
        )

    _install_fake_create(provider, fake_create, monkeypatch)
    deltas: list[dict] = []

    async def on_delta(delta):
        deltas.append(delta)

    resp = asyncio.run(
        provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            model="grok-4.6",
            max_tokens=1234,
            on_content_delta=on_delta,
        )
    )
    assert captured["model"] == "grok-4.6"
    assert captured["max_output_tokens"] == 1234
    assert captured["stream"] is True
    assert captured["store"] is False
    assert "max_tokens" not in captured
    assert resp.content == "你好，世界"
    # done 事件会补齐 delta 未覆盖的后缀（"，世界"）
    assert [d["content_delta"] for d in deltas] == ["你好", "，世界"]
    assert resp.cache_prompt_tokens == 100
    assert resp.cache_hit_tokens == 40


def test_chat_responses_tool_call_assembly(monkeypatch):
    provider = _make_provider()

    async def fake_create(**payload):
        assert payload["tool_choice"] == "required"
        assert payload["tools"][0]["name"] == "get_weather"
        assert payload["parallel_tool_calls"] is True
        return _FakeStream(
            [
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc1",
                    "delta": '{"city": ',
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc1",
                    "delta": '"北京"}',
                },
                {
                    "type": "response.output_item.done",
                    "item": {
                        "id": "fc1",
                        "type": "function_call",
                        "call_id": "call_9",
                        "name": "get_weather",
                        "arguments": '{"city": "北京"}',
                    },
                },
                {"type": "response.completed", "response": {}},
            ]
        )

    _install_fake_create(provider, fake_create, monkeypatch)
    resp = asyncio.run(
        provider.chat(
            messages=[{"role": "user", "content": "查天气"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            model="grok-4.6",
            max_tokens=1024,
            tool_choice={"type": "function", "function": {"name": "get_weather"}},
        )
    )
    assert len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert call.id == "call_9"
    assert call.name == "get_weather"
    assert call.arguments == {"city": "北京"}


def test_chat_responses_thinking_deltas(monkeypatch):
    provider = _make_provider()

    async def fake_create(**payload):
        return _FakeStream(
            [
                {"type": "response.reasoning_summary_text.delta", "delta": "思考中"},
                {"type": "response.reasoning_summary_text.done", "text": "思考中..."},
                {"type": "response.output_text.delta", "delta": "答案"},
                {"type": "response.completed", "response": {}},
            ]
        )

    _install_fake_create(provider, fake_create, monkeypatch)
    deltas: list[dict] = []

    async def on_delta(delta):
        deltas.append(delta)

    resp = asyncio.run(
        provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            model="m",
            max_tokens=100,
            on_content_delta=on_delta,
        )
    )
    assert resp.thinking == "思考中..."
    assert resp.content == "答案"
    assert [d["thinking_delta"] for d in deltas if "thinking_delta" in d] == [
        "思考中",
        "...",
    ]


def test_chat_responses_reasoning_effort_and_extra_body(monkeypatch):
    provider = _make_provider(extra_body={"reasoning_effort": "ultra", "enable_thinking": True})
    captured: dict = {}

    async def fake_create(**payload):
        captured.update(payload)
        return _FakeStream([{"type": "response.completed", "response": {}}])

    _install_fake_create(provider, fake_create, monkeypatch)
    resp = asyncio.run(
        provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            model="grok-4.6",
            max_tokens=100,
        )
    )
    assert resp.content is None
    assert captured["reasoning"]["effort"] == "max"  # ultra 归一化为 max
    assert "enable_thinking" not in captured
    assert "extra_body" not in captured


def test_chat_responses_tool_content_list_flattened(monkeypatch):
    provider = _make_provider()
    captured: dict = {}

    async def fake_create(**payload):
        captured.update(payload)
        return _FakeStream([{"type": "response.completed", "response": {}}])

    _install_fake_create(provider, fake_create, monkeypatch)
    asyncio.run(
        provider.chat(
            messages=[
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": [{"type": "text", "text": "结果A"}, {"type": "text", "text": "结果B"}],
                }
            ],
            tools=[],
            model="m",
            max_tokens=100,
        )
    )
    outputs = [
        item
        for item in captured["input"]
        if item.get("type") == "function_call_output"
    ]
    assert outputs[0]["output"] == "结果A\n结果B"


# ── 错误映射 ──────────────────────────────────────────────────────────────────


def _failed_event(code: str, message: str = "boom") -> dict:
    return {
        "type": "response.failed",
        "response": {"error": {"code": code, "message": message}},
    }


def test_stream_error_context_length_maps(monkeypatch):
    provider = _make_provider()

    async def fake_create(**payload):
        return _FakeStream([_failed_event("context_length_exceeded")])

    _install_fake_create(provider, fake_create, monkeypatch)
    with pytest.raises(ContextLengthError):
        asyncio.run(
            provider.chat(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                model="m",
                max_tokens=100,
            )
        )


def test_stream_error_policy_maps(monkeypatch):
    provider = _make_provider()

    async def fake_create(**payload):
        return _FakeStream([_failed_event("policy_violation")])

    _install_fake_create(provider, fake_create, monkeypatch)
    with pytest.raises(ContentSafetyError):
        asyncio.run(
            provider.chat(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                model="m",
                max_tokens=100,
            )
        )


def test_stream_error_generic_maps(monkeypatch):
    provider = _make_provider()

    async def fake_create(**payload):
        return _FakeStream([_failed_event("server_error")])

    _install_fake_create(provider, fake_create, monkeypatch)
    with pytest.raises(RuntimeError, match="server_error"):
        asyncio.run(
            provider.chat(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                model="m",
                max_tokens=100,
            )
        )


def test_stream_break_before_completed(monkeypatch):
    provider = _make_provider()

    async def fake_create(**payload):
        return _FakeStream([{"type": "response.output_text.delta", "delta": "partial"}])

    _install_fake_create(provider, fake_create, monkeypatch)
    with pytest.raises(RuntimeError, match="断流"):
        asyncio.run(
            provider.chat(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                model="m",
                max_tokens=100,
            )
        )


def test_stream_idle_timeout(monkeypatch):
    provider = _make_provider(stream_idle_timeout_s=0.05)

    async def fake_create(**payload):
        return _HangingStream()

    _install_fake_create(provider, fake_create, monkeypatch)
    with pytest.raises(LLMNetworkTimeoutError):
        asyncio.run(
            provider.chat(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                model="m",
                max_tokens=100,
            )
        )


def test_create_stage_error_classification(monkeypatch):
    provider = _make_provider(max_retries=0)

    async def fake_create(**payload):
        raise RuntimeError("Error code: 400 - context_length_exceeded")

    _install_fake_create(provider, fake_create, monkeypatch)
    with pytest.raises(ContextLengthError):
        asyncio.run(
            provider.chat(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                model="m",
                max_tokens=100,
            )
        )


# ── 默认 chat completions 路径回归 ────────────────────────────────────────────


def test_default_chat_path_unchanged(monkeypatch):
    provider = LLMProvider(api_key="test-key", payload_snapshot_enabled=False, max_retries=1)
    calls: list[int] = []

    async def fake_create(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("503 server error")
        resp = type("Resp", (), {})()
        msg = type(
            "Msg",
            (),
            {"content": "ok", "tool_calls": None, "reasoning_content": None},
        )()
        resp.choices = [type("Choice", (), {"message": msg})()]
        resp.usage = None
        return resp

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)
    resp = asyncio.run(
        provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            model="m",
            max_tokens=100,
        )
    )
    assert resp.content == "ok"
    assert len(calls) == 2  # 第一次 503 重试后成功


# ── 配置层 ────────────────────────────────────────────────────────────────────

_MINIMAL_TOML = """
provider = "openai"
model = "m"
api_key = "k"
system_prompt = "s"

[llm]
{llm_protocol}

[llm.main]
model = "grok-4.6"
{main_protocol}

[llm.fast]
model = "fast-m"
{fast_protocol}
"""


def _write_config(tmp_path, llm_protocol="", main_protocol="", fast_protocol=""):
    text = _MINIMAL_TOML.format(
        llm_protocol=llm_protocol,
        main_protocol=main_protocol,
        fast_protocol=fast_protocol,
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_config_protocol_slot_inherits_llm_level(tmp_path):
    config = load_config(_write_config(tmp_path, llm_protocol='protocol = "codex"'))
    assert config.protocol == "codex"
    assert config.light_protocol == "codex"


def test_config_protocol_slot_overrides(tmp_path):
    config = load_config(
        _write_config(
            tmp_path,
            llm_protocol='protocol = "codex"',
            fast_protocol='protocol = "openai"',
        )
    )
    assert config.protocol == "codex"
    assert config.light_protocol == "openai"


def test_config_protocol_default_openai(tmp_path):
    config = load_config(_write_config(tmp_path))
    assert config.protocol == "openai"
    assert config.light_protocol == "openai"


def test_config_protocol_invalid_raises(tmp_path):
    with pytest.raises(ValueError, match="protocol"):
        load_config(_write_config(tmp_path, llm_protocol='protocol = "grpc"'))


def test_config_protocol_responses_alias(tmp_path):
    config = load_config(_write_config(tmp_path, main_protocol='protocol = "responses"'))
    assert config.protocol == "responses"


# ── 构造点接线 ────────────────────────────────────────────────────────────────


def test_build_providers_wires_protocol(monkeypatch):
    import bootstrap.providers as bp

    captured: list[dict] = []
    real = bp.LLMProvider

    def spy(**kwargs):
        captured.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(bp, "LLMProvider", spy)

    class Cfg:
        provider = "openai"
        model = "grok-4.6"
        api_key = "k"
        system_prompt = "s"
        base_url = "https://gw.example.com/v1"
        extra_body = {}
        dev_mode = False
        protocol = "codex"
        light_model = ""
        light_api_key = ""
        light_base_url = ""
        agent_model = ""
        agent_api_key = ""
        agent_base_url = ""
        multimodal = True
        vl_model = ""

    bp.build_providers(Cfg())
    assert captured[0]["protocol"] == "codex"


def test_build_proactive_provider_follows_main_protocol():
    from bootstrap.proactive import _build_proactive_provider

    class Cfg:
        api_key = "k"
        system_prompt = "s"
        base_url = "https://gw.example.com/v1"
        extra_body = {}
        provider = "openai"
        protocol = "codex"

    sentinel = object()
    provider = _build_proactive_provider(Cfg(), sentinel)
    assert provider is not sentinel
    assert provider._protocol == "codex"


def test_tomllib_parses_protocol_under_llm(tmp_path):
    """确保 [llm] protocol 与既有 [llm.main] 嵌套表在 TOML 解析中共存。"""
    path = _write_config(tmp_path, llm_protocol='protocol = "codex"')
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["llm"]["protocol"] == "codex"
    assert raw["llm"]["main"]["model"] == "grok-4.6"
