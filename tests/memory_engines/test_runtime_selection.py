"""C14 运行时接线验收（task 2.x）。

生效时机（work 内冻结 / 下个 work 重解析）、检索管线 binding 优先选择规则、
多引擎同名工具分发器（双引擎启动不崩溃 + 按租户引擎分发）、C7 目录按 active
engine 过滤、ingest 独占门控（非 active 引擎不处理 TurnCommitted）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent.looping.ports import MemoryServices
from agent.retrieval.default_pipeline import AgenticRAGPipeline
from agent.retrieval.protocol import RetrievalRequest
from agent.tools.catalog import tenant_visible_names
from agent.tools.context import ToolExecutionContext
from agent.tools.meta.register import register_memory_tools_for_engines
from agent.tools.registry import ToolRegistry
from agent.work_binding import (
    bind_work_engine,
    current_work_engine,
    reset_work_engine,
)
from bootstrap.memory_binding import EngineIngestGate
from core.memory.engine import MemoryToolProfile, MemoryToolSpec
from infra.storage.interfaces import TenantContext

pytestmark = pytest.mark.postgres


# ── Fake engines ─────────────────────────────────────────────────────────────


class _FakeEngine:
    """最小 engine 替身：记录 query 调用；tool_profile 可声明工具面。"""

    def __init__(
        self,
        name: str,
        *,
        with_recall: bool = True,
        with_memorize: bool = False,
        extra_tools: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.queries: list[Any] = []
        self._with_recall = with_recall
        self._with_memorize = with_memorize
        self._extra_tools = extra_tools

    async def query(self, request: Any) -> Any:
        self.queries.append(request)
        return SimpleNamespace(
            text_block=f"[{self.name}] block", trace=None, records=[], raw={}
        )

    def tool_profile(self) -> MemoryToolProfile:
        recall = (
            MemoryToolSpec(
                name="recall_memory",
                description=f"recall via {self.name}",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            )
            if self._with_recall
            else None
        )
        memorize = (
            MemoryToolSpec(
                name="memorize",
                description=f"memorize via {self.name}",
                parameters={"type": "object", "properties": {}},
                risk="write",
            )
            if self._with_memorize
            else None
        )
        extras = tuple(
            MemoryToolSpec(
                name=tool_name,
                description=f"{tool_name} via {self.name}",
                parameters={"type": "object", "properties": {}},
            )
            for tool_name in self._extra_tools
        )
        return MemoryToolProfile(recall=recall, memorize=memorize, tools=extras)


def _request(engine_binding: str = "") -> RetrievalRequest:
    return RetrievalRequest(
        message="hello",
        tenant=TenantContext(tenant_id="t1"),
        session_key="chat:t1",
        channel="chat",
        chat_id="t1",
        history=[],
        session_metadata={},
        engine_binding=engine_binding,
    )


# ── 验收 4：生效时机（work 内冻结 / 下个 work 生效）──────────────────────────


async def test_work_binding_frozen_within_work(
    me_factory: Any, me_pg_url: str
) -> None:
    from bootstrap.memory_binding import TenantMemoryEngineBindingService

    service = TenantMemoryEngineBindingService(
        me_factory, ready_engines=("default", "rachael")
    )
    await service.ensure_binding("t1")

    async def resolver(tenant_id: str) -> str:
        return await service.resolve_active_engine(tenant_id)

    # work 1：lease 内解析一次并绑定 → work 期间即使 binding 被切换也不换引擎。
    token = bind_work_engine(await resolver("t1"))
    try:
        assert current_work_engine() == "default"
        await service.switch("t1", "rachael")
        assert await resolver("t1") == "rachael"  # 新解析已是新引擎
        assert current_work_engine() == "default"  # 进行中 work 仍冻结旧引擎
    finally:
        reset_work_engine(token)

    # work 2：下个 work 重新解析 → 取新引擎。
    token2 = bind_work_engine(await resolver("t1"))
    try:
        assert current_work_engine() == "rachael"
    finally:
        reset_work_engine(token2)
    assert current_work_engine() == ""


# ── 验收 4/9：检索管线 binding 优先（不碰 C13 召回内部）──────────────────────


async def test_pipeline_routes_by_engine_binding() -> None:
    default_engine = _FakeEngine("default")
    rachael_engine = _FakeEngine("rachael")
    pipeline = AgenticRAGPipeline(
        memory=MemoryServices(
            engines={"default": default_engine, "rachael": rachael_engine}
        ),
    )
    result = await pipeline.retrieve(_request("rachael"))
    assert result.block == "[rachael] block"
    assert default_engine.queries == [] and len(rachael_engine.queries) == 1

    result = await pipeline.retrieve(_request("default"))
    assert result.block == "[default] block"
    assert len(default_engine.queries) == 1


async def test_pipeline_binding_fallback_to_primary() -> None:
    """绑定引擎未构建 → 回退 primary 并直查（不进融合路径、不阻断检索）。"""
    primary = _FakeEngine("default")
    pipeline = AgenticRAGPipeline(
        memory=MemoryServices(engines={"default": primary}),
    )
    result = await pipeline.retrieve(_request("ghost-engine"))
    assert result.block == "[default] block"
    assert len(primary.queries) == 1


async def test_pipeline_no_binding_single_engine() -> None:
    """无绑定（dev 路径）单引擎 → 旧行为直查 primary。"""
    primary = _FakeEngine("default")
    pipeline = AgenticRAGPipeline(memory=MemoryServices(engines={"default": primary}))
    result = await pipeline.retrieve(_request())
    assert result.block == "[default] block"


# ── 验收 2/6：多引擎工具注册与租户分发 ───────────────────────────────────────


def _make_tools(engines: dict[str, _FakeEngine]) -> ToolRegistry:
    tools = ToolRegistry()
    register_memory_tools_for_engines(
        tools,
        {name: engine for name, engine in engines.items()},  # type: ignore[arg-type]
    )
    return tools


def test_dual_engine_registration_no_crash_and_dispatcher() -> None:
    """双引擎并存注册不崩溃（修 recall_memory 重复注册）；同名工具为分发器。"""
    default_engine = _FakeEngine("default", with_memorize=True)
    rachael_engine = _FakeEngine("rachael", extra_tools=("reinforce_memory",))
    tools = _make_tools({"default": default_engine, "rachael": rachael_engine})
    registered = tools.get_registered_names()
    # recall_memory 只注册一次；两侧独有工具齐全。
    assert sum(1 for name in registered if name == "recall_memory") == 1
    assert {"recall_memory", "memorize", "reinforce_memory"} <= registered
    docs = {doc.name: doc for doc in tools.get_documents()}
    # 独有工具标注归属引擎；同名分发器 source_name 留空（多归属）。
    assert docs["memorize"].source_name == "default"
    assert docs["reinforce_memory"].source_name == "rachael"
    assert docs["recall_memory"].source_name == ""
    assert docs["recall_memory"].source_type == "memory_engine"


async def test_dispatcher_routes_by_work_engine() -> None:
    default_engine = _FakeEngine("default", with_memorize=True)
    rachael_engine = _FakeEngine("rachael", extra_tools=("reinforce_memory",))
    tools = _make_tools({"default": default_engine, "rachael": rachael_engine})

    recall = tools.get_tool("recall_memory")
    assert recall is not None
    await recall.execute(query="q1", memory_engine="rachael", tenant_id="t1")
    assert len(rachael_engine.queries) == 1 and default_engine.queries == []
    await recall.execute(query="q2", memory_engine="default", tenant_id="t1")
    assert len(default_engine.queries) == 1
    # 缺省（dev/未注入）→ primary。
    await recall.execute(query="q3", tenant_id="t1")
    assert len(default_engine.queries) == 2
    # 未知引擎 → primary 回退。
    await recall.execute(query="q4", memory_engine="ghost", tenant_id="t1")
    assert len(default_engine.queries) == 3


def test_tenant_catalog_filters_by_active_engine() -> None:
    """C7 目录：engine 工具按租户 active engine 过滤（未启用引擎不可见）。"""
    default_engine = _FakeEngine("default", with_memorize=True)
    rachael_engine = _FakeEngine("rachael", extra_tools=("reinforce_memory",))
    tools = _make_tools({"default": default_engine, "rachael": rachael_engine})

    def _ctx(memory_engine: str, principal: str = "user") -> ToolExecutionContext:
        return ToolExecutionContext(
            request_id="req",
            account_id="acc",
            tenant_id="t1",
            session_id="chat:t1",
            turn_id="turn",
            channel="chat",
            chat_id="t1",
            principal_type=principal,
            memory_engine=memory_engine,
        )

    visible_default = tenant_visible_names(tools, _ctx("default"))
    assert visible_default is not None
    assert "recall_memory" in visible_default and "memorize" in visible_default
    assert "reinforce_memory" not in visible_default  # rachael 独有 → 不可见

    visible_rachael = tenant_visible_names(tools, _ctx("rachael"))
    assert visible_rachael is not None
    assert "recall_memory" in visible_rachael and "reinforce_memory" in visible_rachael
    assert "memorize" not in visible_rachael  # default 独有 → 不可见

    # 无绑定（dev）路径：engine 工具全量可见（现状行为）。
    visible_dev = tenant_visible_names(tools, _ctx(""))
    assert visible_dev is not None
    assert {"recall_memory", "memorize", "reinforce_memory"} <= visible_dev
    # 非 user principal（dev/owner）不过滤。
    assert tenant_visible_names(tools, _ctx("default", principal="dev")) is None


# ── 验收 5：ingest 独占（非 active 引擎不写入）──────────────────────────────


def _turn_committed(tenant_id: str) -> Any:
    from bus.events_lifecycle import TurnCommitted

    return TurnCommitted(
        session_key=f"chat:{tenant_id}",
        channel="chat",
        chat_id=tenant_id,
        input_message="m",
        persisted_user_message=None,
        assistant_response="r",
        tools_used=[],
        tenant_id=tenant_id,
    )


async def test_ingest_gate_active_engine_only() -> None:
    from bus.event_bus import EventBus

    from bootstrap.memory import _EngineScopedEventBus

    bus = EventBus()
    gate = EngineIngestGate()
    gate.bind_reader(lambda tenant: {"t1": "default"}.get(tenant))
    seen: list[tuple[str, str]] = []
    default_view = _EngineScopedEventBus(bus, engine_id="default", gate=gate)
    rachael_view = _EngineScopedEventBus(bus, engine_id="rachael", gate=gate)
    default_view.on(type(_turn_committed("t1")), lambda e: seen.append(("default", e.tenant_id)))
    rachael_view.on(type(_turn_committed("t1")), lambda e: seen.append(("rachael", e.tenant_id)))

    await bus.emit(_turn_committed("t1"))
    assert seen == [("default", "t1")]  # 非 active 引擎不处理

    seen.clear()
    await bus.emit(_turn_committed("t2"))  # 快照未命中 → fail-open（两引擎都处理）
    assert sorted(seen) == [("default", "t2"), ("rachael", "t2")]


async def test_ingest_gate_async_handler_and_reader_exception_fail_open() -> None:
    from bus.event_bus import EventBus

    from bootstrap.memory import _EngineScopedEventBus

    bus = EventBus()
    gate = EngineIngestGate()

    def _boom(tenant: str) -> str:
        raise RuntimeError("reader down")

    gate.bind_reader(_boom)
    seen: list[str] = []

    async def _handler(event: Any) -> None:
        seen.append(event.tenant_id)

    view = _EngineScopedEventBus(bus, engine_id="default", gate=gate)
    view.on(type(_turn_committed("t1")), _handler)
    await bus.emit(_turn_committed("t1"))
    assert seen == ["t1"]  # reader 异常 fail-open
