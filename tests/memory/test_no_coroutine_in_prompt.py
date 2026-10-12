"""c18 task 2.5 / 4.5：主动推送的画像段经共用渲染器进帧，且不可能出现未 await 的协程。

原来这条测试是红的，因为 `plugins/proactive_flow/prompt.py` 用同步方式调一个声明为 async
的画像存储：协程对象是真值，`str(x() or "")` 里的 `or ""` 兜不住，`<coroutine object ...>`
的字符串表示于是直接进提示正文，而全程不抛异常。4.5 之后这条路径上根本没有对存储的读取
调用——插件只消费 `render_sections` 交回的段，所以那个失效形状是**结构上不可能**，不是
被修好了。

因此本文件的两条断言：

1. 真跑 `build_runtime_context_message`，画像三段确实经真 `ContextBuilder` 渲染进帧，
   正文里没有 `coroutine` 字样。
2. 结构守卫：`ProactivePromptBuilder` 不再收任何画像存储句柄（构造参数里没有 `memory`），
   防止有人把老形状接回来——那正是「装了但静默失效」最容易复发的方式。

覆盖范围如实交代：drift（`plugins/drift_flow/runtime.py:600`）那行嵌在需要完整 tick 上下文
的 async 方法里，4.6 接上之前只能由 2.4 的静态扫描覆盖；唤醒（`plugins/wake_proactive/`）
今天导不进来（缺 `session/embedding_store`，加载器把 `ImportError` 吞成一条 warning），
同样只有静态覆盖。「快照缺失时不回退共享目录」那条行为断言属于 6.9（`SelfModelPromptBlock`
的回退分支还在）。

`-W error` 本身不足以抓住协程问题：「coroutine was never awaited」是在协程对象被垃圾回收
时才发出的，不在调用点，所以断言里要显式 `gc.collect()`。
"""

from __future__ import annotations

import gc
import inspect
import warnings
from pathlib import Path
from typing import cast

import pytest

from agent.context import ContextBuilder
from agent.core.types import MemoryProfileSnapshot, PersonaSnapshot
from plugins.default_proactive.context import AgentTickContext
from plugins.default_proactive.gateway import GatewayResult
from plugins.proactive_flow.prompt import ProactivePromptBuilder
from proactive_v2.config import ProactiveConfig

_TENANT = "c18-bypass-a"


def _tick_with_snapshots() -> AgentTickContext:
    ctx = AgentTickContext(session_key="telegram:42", tenant_id=_TENANT)
    ctx.persona_snapshot = PersonaSnapshot(
        tenant_id=_TENANT,
        source="custom",
        identity="该租户的身份",
        personality_rules="该租户的规则",
        relationship_state="该租户的自我认知正文",
    )
    ctx.profile_snapshot = MemoryProfileSnapshot(
        tenant_id=_TENANT,
        long_term="该租户的长期画像正文",
        pending="该租户的待合并正文",
        recent_context="该租户的近期摘要正文",
        history="",
    )
    return ctx


def test_proactive_frame_carries_sections_from_the_shared_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 替身从 3.8 那个文件复用（_Skills / _Memory），不再抄第三份。
    from tests.memory.test_render_sections_parity import _Memory, _Skills

    monkeypatch.setattr("agent.context.SkillsLoader", _Skills)
    monkeypatch.setattr(
        "agent.context.build_agent_static_identity_prompt", lambda **_: "identity"
    )

    builder = ProactivePromptBuilder(
        cfg=cast(ProactiveConfig, None),
        workspace_context_fn=lambda: "",
        # memory 参数在 4.8 之前还是必填；本轮有 persona 快照，块不会走那个回退分支。
        # _Memory 是刻意不满足协议的局部替身（与 3.8 那个文件同一个处理）。
        profile_renderer=ContextBuilder(tmp_path, _Memory()),  # type: ignore[arg-type]
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        message = builder.build_runtime_context_message(
            _tick_with_snapshots(), GatewayResult()
        )
        gc.collect()

    body = message["content"]
    bad = [str(w.message) for w in caught if "never awaited" in str(w.message)]
    assert not bad, f"主动推送留下了未 await 的协程：{bad}"
    assert "coroutine" not in body, f"协程字符串进了帧：{body!r}"
    for expected in ("该租户的自我认知正文", "该租户的长期画像正文", "该租户的近期摘要正文"):
        assert expected in body, f"画像段没经渲染器进帧，缺 {expected!r}"


def test_prompt_builder_takes_no_profile_storage_handle() -> None:
    """构造签名里没有存储句柄——老形状是「同步调 async 存储」，静默失效且不报错。"""
    params = inspect.signature(ProactivePromptBuilder.__init__).parameters
    assert "memory" not in params, (
        f"主动推送又拿到了画像存储句柄 {list(params)}：同步调用 async 存储会把"
        "<coroutine object ...> 写进提示，且不抛异常"
    )
