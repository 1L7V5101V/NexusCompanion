from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from agent.config_models import Config
from agent.provider import LLMProvider
from agent.tools.meta import register_memory_meta_tools
from agent.tools.registry import ToolRegistry
from core.memory.markdown import build_markdown_memory_runtime
from core.memory.plugin import (
    DisabledMemoryEngine,
    MemoryPluginBuildDeps,
    MemoryPluginRuntime,
)
from core.memory.runtime import MemoryRuntime
from core.net.http import SharedHttpResources

if TYPE_CHECKING:
    from bus.event_bus import EventBus
    from core.memory.markdown import MarkdownMemoryRuntime


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
    )

    closeables: list[object] = []
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
            )
            engines[engine_name] = plugin_runtime.engine
            closeables.extend(plugin_runtime.closeables)

            # 仅为 default engine 注册内存工具（recall_memory / memorize / forget）
            # Rachael 的写入是 TurnCommitted 自动触发，不需要显式工具
            if engine_name == "default":
                register_memory_meta_tools(tools, plugin_runtime.engine)

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
