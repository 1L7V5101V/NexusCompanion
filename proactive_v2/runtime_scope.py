from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from agent.plugins.specs import RegisteredProactiveSource
from agent.tool_hooks import ToolHook
from agent.tools.registry import ToolRegistry
from agent.turns.orchestrator import TurnOrchestrator
from bus.event_bus import EventBus
from proactive_v2.mcp_sources import McpGateway

if TYPE_CHECKING:
    from agent.core.prompt_block import ProfileSectionRenderer
    from turn_logging.turn_logger import RoutingTurnLogger


@dataclass
class ProactiveRuntimeScope:
    cfg: Any
    sense: Any
    presence: Any | None
    provider: Any
    model: str
    max_tokens: int
    memory: Any | None
    state_store: Any
    any_action_gate: Any | None
    passive_busy_fn: Any | None
    deduper: Any | None
    rng: Any
    workspace_context_fn: Callable[[], str]
    mcp_gateway: McpGateway
    proactive_sources: list[RegisteredProactiveSource] = field(default_factory=list)
    shared_tools: ToolRegistry | None = None
    turn_orchestrator: TurnOrchestrator | None = None
    event_bus: EventBus | None = None
    tool_hooks: list[ToolHook] = field(default_factory=list)
    schedule_fn: Callable[[float | None], int] | None = None
    turn_logger: Any | None = None
    """RoutingTurnLogger: 日志记录器（运行时组装，避免循环导入）"""
    profile_renderer: "ProfileSectionRenderer | None" = None
    """生产唯一的段渲染器（c18 ADR-15）。

    主动推送与 drift 都用它渲染画像段，因此共用同一份带租户维度的 `SectionCache`。插件侧
    只依赖这个窄协议，不依赖 `ContextBuilder` 或 `AgentLoop` 具体类。
    """
