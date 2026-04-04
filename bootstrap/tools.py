from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent.config_models import Config
from agent.core import (
    AgentCore,
    CoreRunner,
    DefaultContextStore,
    DefaultReasoner,
    PassiveRunner,
    ProviderLLMAdapter,
)
from agent.core.consolidation import ConsolidationService
from agent.core.runtime_support import LLMServices, MemoryConfig, MemoryServices, ToolDiscoveryState
from agent.core.turn_scheduler import TurnScheduler
from agent.peer_agent.process_manager import PeerProcessManager
from agent.peer_agent.poller import PeerAgentPoller
from agent.peer_agent.registry import PeerAgentRegistry
from agent.mcp.registry import McpServerRegistry
from agent.postturn.default_pipeline import DefaultPostTurnPipeline
from agent.provider import LLMProvider
from agent.retrieval.default_pipeline import DefaultMemoryRetrievalPipeline
from agent.scheduler import SchedulerService
from agent.tools.message_push import MessagePushTool
from agent.tools.registry import ToolRegistry
from bootstrap.toolsets.fitbit import register_fitbit_tools
from bootstrap.toolsets.mcp import register_mcp_tools
from bootstrap.toolsets.memory import build_memory_toolset
from bootstrap.toolsets.meta import (
    build_readonly_tools,
    register_meta_and_common_tools,
    register_spawn_tool,
)
from bootstrap.toolsets.peer import build_peer_agent_resources
from bootstrap.toolsets.schedule import build_scheduler, register_scheduler_tools
from bootstrap.providers import build_providers
from bus.processing import ProcessingState
from bus.queue import MessageBus
from core.memory.runtime import MemoryRuntime
from core.net.http import SharedHttpResources
from memory2.profile_extractor import ProfileFactExtractor
from memory2.query_rewriter import QueryRewriter
from memory2.sufficiency_checker import SufficiencyChecker
from proactive_v2.presence import PresenceStore
from session.manager import SessionManager


@dataclass
class CoreRuntime:
    config: Config
    http_resources: SharedHttpResources
    agent_core: AgentCore
    runner: PassiveRunner
    bus: MessageBus
    tools: ToolRegistry
    push_tool: MessagePushTool
    session_manager: SessionManager
    scheduler: SchedulerService
    provider: LLMProvider
    light_provider: LLMProvider | None
    mcp_registry: McpServerRegistry
    memory_runtime: MemoryRuntime
    presence: PresenceStore
    peer_process_manager: PeerProcessManager | None
    peer_poller: PeerAgentPoller | None

    async def start(self) -> None:
        await self.mcp_registry.load_and_connect_all()

        if self.peer_poller is not None and self.config.peer_agents:
            peer_registry = PeerAgentRegistry(
                process_manager=self.peer_process_manager,
                poller=self.peer_poller,
                requester=self.http_resources.local_service,
            )
            peer_tools = await peer_registry.discover_all(self.config.peer_agents)
            for t in peer_tools:
                self.tools.register(
                    t,
                    always_on=False,
                    risk="external-side-effect",
                )
            self.peer_poller.start()

    async def stop(self) -> None:
        if self.peer_poller is not None:
            await self.peer_poller.stop()
        if self.peer_process_manager is not None:
            await self.peer_process_manager.shutdown_all()


def build_registered_tools(
    config: Config,
    workspace: Path,
    http_resources: SharedHttpResources,
    *,
    bus: MessageBus,
    provider,
    light_provider,
    session_store=None,
    tools: ToolRegistry | None = None,
    observe_writer=None,
    agent_loop_provider: Callable[[], Any] | None = None,
) -> tuple[ToolRegistry, MessagePushTool, SchedulerService, McpServerRegistry, MemoryRuntime, PeerProcessManager | None, PeerAgentPoller | None]:
    from session.store import SessionStore

    # ── 第一阶段：建服务（依赖无顺序陷阱）────────────────────────────────────
    tools = tools or ToolRegistry()
    readonly_tools = build_readonly_tools(http_resources)
    store = session_store or SessionStore(workspace / "sessions.db")
    push_tool = MessagePushTool()
    memory_runtime = build_memory_toolset(
        config,
        workspace,
        tools,
        provider,
        light_provider,
        http_resources,
        observe_writer=observe_writer,
    )
    scheduler = build_scheduler(
        workspace,
        push_tool,
        agent_loop_provider=agent_loop_provider,
    )
    peer_process_manager, peer_poller = build_peer_agent_resources(
        config, bus, http_resources
    )

    # ── 第二阶段：注册工具（所有服务已就绪）──────────────────────────────────
    register_meta_and_common_tools(tools, readonly_tools, store, push_tool=push_tool)
    register_fitbit_tools(tools, config, http_resources)
    register_spawn_tool(
        tools,
        config,
        workspace,
        bus,
        provider,
        http_resources,
        memory_port=memory_runtime.port,
    )
    register_scheduler_tools(tools, scheduler)
    mcp_registry = register_mcp_tools(tools, workspace)

    return tools, push_tool, scheduler, mcp_registry, memory_runtime, peer_process_manager, peer_poller


def build_core_runtime(
    config: Config,
    workspace: Path,
    http_resources: SharedHttpResources,
    observe_writer=None,
) -> CoreRuntime:
    # 1. 构造基础依赖
    bus = MessageBus()
    provider, light_provider = build_providers(config)
    session_manager = SessionManager(workspace)
    runner_ref: dict[str, PassiveRunner] = {}
    tools, push_tool, scheduler, mcp_registry, memory_runtime, peer_pm, peer_poller = build_registered_tools(
        config,
        workspace,
        http_resources,
        bus=bus,
        provider=provider,
        light_provider=light_provider,
        session_store=session_manager._store,
        observe_writer=observe_writer,
        agent_loop_provider=lambda: runner_ref.get("runner"),
    )
    presence = PresenceStore(workspace / "presence.json")
    processing_state = ProcessingState()
    light = light_provider or provider
    resolved_memory_config = MemoryConfig(
        window=config.memory_window,
        top_k_procedure=min(3, max(1, int(config.memory_v2.top_k_procedure))),
        top_k_history=max(1, int(config.memory_v2.top_k_history)),
        route_intention_enabled=config.memory_v2.route_intention_enabled,
        sop_guard_enabled=config.memory_v2.sop_guard_enabled,
        gate_llm_timeout_ms=max(100, int(config.memory_v2.gate_llm_timeout_ms)),
        gate_max_tokens=max(32, int(config.memory_v2.gate_max_tokens)),
        hyde_enabled=config.memory_v2.hyde_enabled,
        hyde_timeout_ms=config.memory_v2.hyde_timeout_ms,
    )

    # 2. 构造 retrieval / post-turn 所需的轻量服务
    query_rewriter = (
        QueryRewriter(
            llm_client=light,
            model=config.light_model or config.model,
            max_tokens=config.memory_v2.gate_max_tokens,
            timeout_ms=config.memory_v2.gate_llm_timeout_ms,
        )
        if config.memory_v2.route_intention_enabled
        else None
    )
    sufficiency_checker = (
        SufficiencyChecker(
            llm_client=light,
            model=config.light_model or config.model,
        )
        if config.memory_v2.sufficiency_check_enabled
        else None
    )
    profile_extractor = (
        ProfileFactExtractor(
            llm_client=light,
            model=config.light_model or config.model,
        )
        if config.memory_v2.profile_extraction_enabled
        else None
    )
    hyde_enhancer = None
    if config.memory_v2.hyde_enabled and config.light_model:
        from memory2.hyde_enhancer import HyDEEnhancer

        hyde_enhancer = HyDEEnhancer(
            light_provider=light,
            light_model=config.light_model,
            timeout_s=config.memory_v2.hyde_timeout_ms / 1000.0,
        )

    llm_services = LLMServices(provider=provider, light_provider=light)
    memory_services = MemoryServices(
        port=memory_runtime.port,
        query_rewriter=query_rewriter,
        hyde_enhancer=hyde_enhancer,
        sufficiency_checker=sufficiency_checker,
    )
    tool_discovery = ToolDiscoveryState()
    consolidation = ConsolidationService(
        memory_port=memory_runtime.port,
        provider=provider,
        model=config.model,
        memory_window=config.memory_window,
        profile_extractor=profile_extractor,
    )

    async def _consolidate_and_save(session: object) -> None:
        # 3. consolidation 后立即保存 session
        await consolidation.consolidate(session)  # type: ignore[arg-type]
        await session_manager.save_async(session)  # type: ignore[arg-type]

    turn_scheduler = TurnScheduler(
        post_mem_worker=memory_runtime.post_response_worker,
        consolidation_runner=_consolidate_and_save,
        memory_window=config.memory_window,
    )
    retrieval_pipeline = DefaultMemoryRetrievalPipeline(
        memory=memory_services,
        memory_config=resolved_memory_config,
        llm=llm_services,
        workspace=workspace,
        light_model=config.light_model or config.model,
    )
    post_turn_pipeline = DefaultPostTurnPipeline(
        scheduler=turn_scheduler,
        post_mem_worker=memory_runtime.post_response_worker,
    )

    # 4. 构造新 AgentCore
    agent_core = AgentCore(
        context_store=DefaultContextStore(
            session_manager=session_manager,
            retrieval_pipeline=retrieval_pipeline,
            post_turn_pipeline=post_turn_pipeline,
            workspace=workspace,
            presence=presence,
            observe_writer=observe_writer,
        ),
        reasoner=DefaultReasoner(
            llm_provider=ProviderLLMAdapter(
                provider=provider,
                model=config.model,
                max_tokens=config.max_tokens,
            ),
            max_iterations=config.max_iterations,
            max_tokens=config.max_tokens,
            tool_registry=tools,
            tool_search_enabled=config.tool_search_enabled,
            memory_port=memory_runtime.port,
            tool_discovery=tool_discovery,
        ),
        tools=tools.get_tools(),
        prompt_blocks=[],
        identity_prompt=config.system_prompt,
    )

    # 5. 构造新的被动 runtime runner
    runner = CoreRunner(
        bus=bus,
        agent_core=agent_core,
        processing_state=processing_state,
    )
    runner_ref["runner"] = runner

    # 6. 返回主运行时对象图
    return CoreRuntime(
        config=config,
        http_resources=http_resources,
        agent_core=agent_core,
        runner=runner,
        bus=bus,
        tools=tools,
        push_tool=push_tool,
        session_manager=session_manager,
        scheduler=scheduler,
        provider=provider,
        light_provider=light_provider,
        mcp_registry=mcp_registry,
        memory_runtime=memory_runtime,
        presence=presence,
        peer_process_manager=peer_pm,
        peer_poller=peer_poller,
    )
