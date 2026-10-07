from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from agent.config_models import Config
from agent.provider import LLMProvider
from agent.tools.meta import register_memory_tools_for_engines
from agent.tools.registry import ToolRegistry
from bus.events_lifecycle import TurnCommitted
from core.memory.engine import MemoryEngine
from core.memory.events import ConsolidationCommitted
from core.memory.markdown import build_markdown_memory_runtime
from core.memory.plugin import (
    DisabledMemoryEngine,
    MemoryPluginBuildDeps,
    MemoryPluginRuntime,
)
from core.memory.runtime import MemoryRuntime
from core.net.http import SharedHttpResources

if TYPE_CHECKING:
    from bootstrap.memory_binding import EngineIngestGate
    from bus.event_bus import EventBus, Handler
    from core.memory.markdown import MarkdownMemoryRuntime
    from infra.storage.runtime import StorageRuntime


# C14 ADR-5：多引擎并存时给每个 engine 的 TurnCommitted/ConsolidationCommitted
# 处理加 ingest 独占门控——仅租户 active engine 处理（§5.9.16 固定表），包装为
# per-engine EventBus 视图（其余属性透传真实 bus）。单引擎时不包装。
_GATED_EVENT_TYPES: tuple[type[object], ...] = (TurnCommitted, ConsolidationCommitted)


class _EngineScopedEventBus:
    """per-engine EventBus 视图：gated 事件按租户 active engine 过滤。

    引擎只用 ``on()``/``enqueue()``（与 ``bind_runtime_snapshot_store``）；本
    视图拦截 ``on()`` 中 gated 事件类型的 handler（sync/async 均支持），其余
    一律透传 ``inner``。快照未命中/未接线 fail-open（EngineIngestGate）。
    """

    def __init__(
        self,
        inner: "EventBus",
        *,
        engine_id: str,
        gate: "EngineIngestGate",
    ) -> None:
        self._inner = inner
        self._engine_id = engine_id
        self._gate = gate

    def bind_runtime_snapshot_store(self, store: object) -> None:
        self._inner.bind_runtime_snapshot_store(store)

    def on(self, event_type: type[object], handler: object) -> object:
        if event_type not in _GATED_EVENT_TYPES:
            return self._inner.on(
                event_type, cast("Handler[object]", handler)
            )

        import inspect

        if inspect.iscoroutinefunction(handler):

            async def _gated_async(event: object) -> object:
                tenant = str(getattr(event, "tenant_id", "") or "")
                if self._gate.is_active(tenant, self._engine_id):
                    return await handler(event)  # type: ignore[misc]
                return None

            wrapped: object = _gated_async
        else:

            def _gated_sync(event: object) -> object:
                tenant = str(getattr(event, "tenant_id", "") or "")
                if self._gate.is_active(tenant, self._engine_id):
                    return handler(event)  # type: ignore[operator]
                return None

            wrapped = _gated_sync
        return self._inner.on(
            event_type, cast("Handler[object]", wrapped)
        )

    def __getattr__(self, name: str) -> object:
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)


# 统一插件构造入口，参数化 engine_name 以支持多引擎构建。
def _build_memory_plugin_runtime(
    engine_name: str,
    *,
    config: Config,
    workspace: Path,
    provider: LLMProvider,
    light_provider: LLMProvider | None,
    http_resources: SharedHttpResources,
    markdown: "MarkdownMemoryRuntime",
    event_publisher: "EventBus | None" = None,
    storage_runtime: "StorageRuntime | None" = None,
) -> MemoryPluginRuntime:
    from bootstrap.wiring import resolve_memory_plugin

    plugin = resolve_memory_plugin(engine_name)
    return plugin.build(
        MemoryPluginBuildDeps(
            config=config,
            workspace=workspace,
            provider=provider,
            light_provider=light_provider,
            http_resources=http_resources,
            event_publisher=event_publisher,
            markdown=markdown,
            storage_runtime=storage_runtime,
        )
    )


def _memory_plugin_enabled(config: Config) -> bool:
    return bool(config.memory.enabled)


def ensure_memory_plugin_storage(
    config: Config,
    workspace: Path,
) -> list[tuple[Path, bool]]:
    if not _memory_plugin_enabled(config):
        return []
    from bootstrap.wiring import resolve_memory_plugin

    all_results: list[tuple[Path, bool]] = []
    for engine_name in config.memory.engine_names:
        plugin = resolve_memory_plugin(engine_name)
        initializer = getattr(plugin, "ensure_workspace_storage", None)
        if not callable(initializer):
            continue
        result: object = initializer(config=config, workspace=workspace)
        if isinstance(result, list):
            for item in cast(list[object], result):
                if isinstance(item, tuple):
                    values = cast(tuple[object, ...], item)
                    if len(values) != 2:
                        continue
                    raw_path, raw_existed = values
                    path = Path(str(raw_path))
                    all_results.append((path, bool(raw_existed)))
                elif isinstance(item, str | Path):
                    path = Path(item)
                    all_results.append((path, path.exists()))
    return all_results


def build_memory_runtime(
    config: Config,
    workspace: Path,
    tools: ToolRegistry,
    provider: LLMProvider,
    light_provider: LLMProvider | None,
    http_resources: SharedHttpResources,
    event_publisher: "EventBus | None" = None,
    storage_runtime: "StorageRuntime | None" = None,
    engine_ingest_gate: "EngineIngestGate | None" = None,
) -> MemoryRuntime:
    # 1. markdown 是默认记忆层，任何 engine 都共用。
    markdown = build_markdown_memory_runtime(
        workspace=workspace,
        provider=provider,
        model=config.model,
        keep_count=_memory_keep_count(config.memory_window),
        event_bus=event_publisher,
        recent_context_provider=light_provider or provider,
        recent_context_model=config.light_model or config.model,
        global_maintenance_limit=config.admission.global_maintenance_queue,
    )

    closeables: list[object] = []
    engines: dict[str, MemoryEngine] = {}
    primary_engine: MemoryEngine | None = None

    if _memory_plugin_enabled(config):
        engine_names = config.memory.engine_names
        # C14 ADR-5：多引擎并存时按租户 active engine 门控自动 ingest（快照
        # reader 由 bootstrap 后绑定）；单引擎不包装，行为逐字节不变。
        gate = engine_ingest_gate if engine_ingest_gate is not None else None
        for engine_name in engine_names:
            publisher_for_engine: "EventBus | None" = event_publisher
            if (
                gate is not None
                and event_publisher is not None
                and len(engine_names) > 1
            ):
                publisher_for_engine = cast(
                    "EventBus",
                    _EngineScopedEventBus(
                        event_publisher,
                        engine_id=engine_name,
                        gate=gate,
                    ),
                )
            plugin_runtime = _build_memory_plugin_runtime(
                engine_name,
                config=config,
                workspace=workspace,
                provider=provider,
                light_provider=light_provider,
                http_resources=http_resources,
                markdown=markdown,
                event_publisher=publisher_for_engine,
                storage_runtime=storage_runtime,
            )
            engines[engine_name] = plugin_runtime.engine
            closeables.extend(plugin_runtime.closeables)

        # 跨引擎合并注册 tool_profile 声明的记忆工具（C14 ADR-5）：
        # default → recall_memory + memorize + forget；rachael → recall_memory +
        # reinforce_memory。同名工具（recall_memory）注册为租户 active engine
        # 分发器，修复双引擎重复注册的启动崩溃。
        register_memory_tools_for_engines(tools, engines)

        if engines:
            primary_engine = next(iter(engines.values()))

    return MemoryRuntime(
        markdown=markdown,
        engine=primary_engine or DisabledMemoryEngine(),
        engines=engines,
        closeables=closeables,
    )


def build_memory_admin_runtime(
    config: Config,
    workspace: Path,
    provider: LLMProvider,
    light_provider: LLMProvider | None,
    http_resources: SharedHttpResources,
    event_publisher: "EventBus | None" = None,
    storage_runtime: "StorageRuntime | None" = None,
) -> MemoryRuntime:
    # dashboard 不注册工具，只需要 engine admin 能力和关闭生命周期。
    markdown = build_markdown_memory_runtime(
        workspace=workspace,
        provider=provider,
        model=config.model,
        keep_count=_memory_keep_count(config.memory_window),
        event_bus=event_publisher,
        recent_context_provider=light_provider or provider,
        recent_context_model=config.light_model or config.model,
        global_maintenance_limit=config.admission.global_maintenance_queue,
    )
    closeables: list[object] = [http_resources]
    engines: dict[str, MemoryEngine] = {}
    primary_engine: MemoryEngine | None = None

    if _memory_plugin_enabled(config):
        for engine_name in config.memory.engine_names:
            plugin_runtime = _build_memory_plugin_runtime(
                engine_name,
                config=config,
                workspace=workspace,
                provider=provider,
                light_provider=light_provider,
                http_resources=http_resources,
                markdown=markdown,
                event_publisher=event_publisher,
                storage_runtime=storage_runtime,
            )
            engines[engine_name] = plugin_runtime.engine
            closeables[:0] = plugin_runtime.closeables

        if engines:
            primary_engine = next(iter(engines.values()))

    return MemoryRuntime(
        markdown=markdown,
        engine=primary_engine or DisabledMemoryEngine(),
        engines=engines,
        closeables=closeables,
    )


def _memory_keep_count(window: int) -> int:
    aligned_window = max(4, ((max(1, window) + 3) // 4) * 4)
    return aligned_window // 2
