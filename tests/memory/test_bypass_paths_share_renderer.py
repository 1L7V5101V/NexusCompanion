"""c18 task 4.4：两条旁路与主对话共用同一个段渲染器，因此共用同一份租户维度缓存。

ADR-15 的装配选择是「渲染器只有一个实例，从主 agent 传给 proactive scope」，理由有两条：
一是画像段的顺序、命名与 breakdown 只有一份（ADR-15 不选「只抽一个 helper 函数」的那条），
二是 `SectionCache` 跟着实例走，共用才谈得上 ADR-11 的租户维度缓存在两条路径上都生效。

本文件不测 4.5 / 4.6 的消费（那时两条路径才会真的调 `render_sections`），测的是装配本身：
渲染器从 `AgentLoop` 出来是它自己在用的那一个实例、`ProactiveLoop` 把它转发进两条旁路共用
的那个 scope。「同一个渲染器 ⇒ 同租户命中缓存、跨租户不命中」这半条已经在
`test_static_cache_tenant_scoped.py`（3.6）里钉过，这里不重复断言。

`ProactiveLoop` 与 `AgentLoop` 的真实构造要拉十几样的依赖（session manager、provider、
push tool、kernel、snapshot store…），全仓库今天没有那样的夹具，所以这里用绑定方法 +
替身对象走最小必要面：`_build_runtime_scope` 只读 self 的字段，把字段交齐就能验它有没有
把渲染器带进 scope。这不是绕开测试，是把断言精确落在「转发发生与否」这一件事上。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agent.looping.core import AgentLoop
from proactive_v2.loop import ProactiveLoop
from proactive_v2.runtime_scope import ProactiveRuntimeScope


def _build_scope(loop_stub: Any) -> ProactiveRuntimeScope:
    """直接调 `_build_runtime_scope` 这个未绑定方法，绕开 ProactiveLoop 的构造。"""
    return ProactiveLoop._build_runtime_scope(loop_stub)  # type: ignore[arg-type]


def test_agent_loop_exposes_its_own_builder() -> None:
    """渲染器必须是主 agent 在用的那一个实例，不是又建一个。"""
    builder = object()
    stub = SimpleNamespace(_context=builder)

    assert AgentLoop.profile_renderer.fget(stub) is builder  # type: ignore[arg-type]


def test_scope_fields_default_to_none() -> None:
    """没装配它们（dev 路径 / 测试）时是 None，不兜一个假渲染器或假解析器。

    查字段默认值而不是构造整个 scope：`mcp_gateway` 的类型不是 Optional，为这条断言造一个
    假网关没有意义。
    """
    fields = ProactiveRuntimeScope.__dataclass_fields__
    assert fields["profile_renderer"].default is None
    assert fields["persona_resolver"].default is None


def test_build_runtime_scope_forwards_the_renderer() -> None:
    """`ProactiveLoop._build_runtime_scope` 把渲染器与解析器都带进两条旁路共用的那个 scope。"""
    builder = object()
    resolver = object()
    loop_stub = SimpleNamespace(
        _cfg=object(),
        _sense=object(),
        _presence=None,
        _provider=object(),
        _model="m",
        _max_tokens=8,
        _memory=None,
        _state=object(),
        _passive_busy_fn=None,
        _turn_orchestrator=None,
        _rng=None,
        _read_workspace_proactive_context=lambda: "",
        _shared_tools=None,
        _event_bus=None,
        _mcp_gateway=object(),
        _plugin_proactive_sources=[],
        _tool_hooks=[],
        _scheduler=SimpleNamespace(next_interval=lambda _s: 1),
        _turn_logger=None,
        _profile_renderer=builder,
        _persona_resolver=resolver,
    )

    scope = _build_scope(loop_stub)

    assert scope.profile_renderer is builder
    assert scope.persona_resolver is resolver
