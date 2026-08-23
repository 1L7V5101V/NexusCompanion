"""负载 turn 驱动器：组装最小真实 passive turn 路径并驱动 N 轮（C0 任务 2.4+）。

「真实」部分：StorageRuntime（按 config storage.backend）+ SessionManager +
EventBus + PassiveTurnPipeline 全 phase 链（before_turn / before_reasoning /
reasoner / after_reasoning / after_turn）。
「桩」部分：ContextStore（不检索）、ContextBuilder（不拼 prompt）、ToolRegistry
（set_context no-op），reasoner 二选一：
- script：注入式 ScriptedReasoner，无真实 LLM 调用，用于高并发；
- llm：真实 DefaultReasoner + config provider，小批量冒烟。

消息带 skip_memory_context_guard=True，绕过归档守卫（把「记忆归档」压力排除在
存储负载口径之外）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Any

from agent.config_models import StorageConfig
from agent.control.context import current_turn_id
from agent.core.passive_turn import AgentCore, AgentCoreDeps, Reasoner
from agent.core.runtime_support import TurnRunResult
from agent.core.types import (
    ContextBundle,
    ContextRenderResult,
    ReasonerResult,
)
from agent.looping.ports import SessionServices
from bus.event_bus import EventBus
from bus.events import InboundMessage
from core.error_context import current_session_key
from core.telemetry.builtin import BuiltinMetrics, register_builtin_metrics
from infra.storage.factory import create_storage_runtime
from session.manager import SessionManager

from scripts.load.result import LoadSpec, RunStats

logger = logging.getLogger(__name__)


class StubContextStore:
    """不走检索的最小 ContextStore：prepare 直接返回空 ContextBundle。"""

    async def prepare(
        self,
        *,
        msg: InboundMessage,
        session_key: str,
        session: Any,
    ) -> ContextBundle:
        return ContextBundle()


class StubContextBuilder:
    """不拼 prompt 的最小 ContextBuilder：render 返回空结果，供 after_turn 预算读取。"""

    def __init__(self) -> None:
        self._last_debug_breakdown: list[Any] = []

    @property
    def last_debug_breakdown(self) -> list[Any]:
        return list(self._last_debug_breakdown)

    @property
    def last_assembled_contexts(self) -> dict[str, dict[str, str]]:
        return {"turn_injection_context": {}}

    def render(
        self,
        request: Any,
        *,
        system_sections_top: list[Any] | None = None,
        system_sections_bottom: list[Any] | None = None,
    ) -> ContextRenderResult:
        messages: list[dict[str, Any]] = []
        if getattr(request, "current_message", ""):
            messages.append({"role": "user", "content": request.current_message})
        self._last_debug_breakdown = []
        return ContextRenderResult(
            system_prompt="",
            turn_injection_context={},
            messages=messages,
            debug_breakdown=[],
        )


class StubToolRegistry:
    """最小 ToolRegistry：set_context no-op，schema 类接口返回空。"""

    def set_context(self, **kwargs: Any) -> None:
        return None

    def get_schemas(self, names: Any = None) -> list[dict[str, Any]]:
        return []

    def get_always_on_names(self) -> set[str]:
        return set()

    def get_registered_names(self) -> set[str]:
        return set()

    def get_registered_order(self, names: set[str] | list[str]) -> list[str]:
        return []


class ScriptedReasoner(Reasoner):
    """注入式脚本 reasoner：无真实 LLM 调用。

    支持 sim_latency_ms（模拟 LLM 延迟）与 sim_fail_rate（按 seed 确定性的失败率），
    两者都作为输入参数回写结果，保证 2.6 可复现。
    """

    def __init__(
        self,
        *,
        latency_ms: float = 0.0,
        fail_rate: float = 0.0,
        seed: int | None = None,
    ) -> None:
        self.latency_ms = max(0.0, float(latency_ms))
        self.fail_rate = max(0.0, min(1.0, float(fail_rate)))
        self._rng = random.Random(seed)
        self.invocations = 0
        self.failures = 0

    async def run(
        self,
        initial_messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> ReasonerResult:
        # passive 路径走 run_turn；run 仅供 Reasoner 抽象完整性。
        return ReasonerResult(reply="")

    async def run_turn(
        self,
        *,
        msg: InboundMessage,
        session: Any,
        skill_names: list[str] | None = None,
        base_history: list[dict[str, Any]] | None = None,
        retrieved_memory_block: str = "",
        extra_hints: list[str] | None = None,
    ) -> TurnRunResult:
        self.invocations += 1
        if self.latency_ms > 0:
            await asyncio.sleep(self.latency_ms / 1000.0)
        if self.fail_rate > 0 and self._rng.random() < self.fail_rate:
            self.failures += 1
            # 模拟「LLM 无产出」：reply=None 走 after_reasoning 的 fallback 文案，
            # 出站内容不再以 echo: 开头，_drive_one 据此判失败（不抛异常、不打 trace）。
            return TurnRunResult(
                reply=None,
                tools_used=[],
                tool_chain=[],
                thinking=None,
                streamed=False,
                context_retry={},
                initial_messages=[],
                tools_schema=[],
            )
        return TurnRunResult(
            reply=f"echo:{getattr(msg, 'content', '')}",
            tools_used=[],
            tool_chain=[],
            thinking=None,
            streamed=False,
            context_retry={},
            initial_messages=[],
            tools_schema=[],
        )

    async def render_prompt(self, input: Any) -> Any:
        from agent.lifecycle.types import PromptRenderResult

        return PromptRenderResult(messages=[])


def build_reasoner(spec: LoadSpec) -> Reasoner:
    """按 spec.reasoner 选择 reasoner。llm 模式延迟导入真实 provider 与 DefaultReasoner。"""
    if spec.reasoner == "script":
        return ScriptedReasoner(
            latency_ms=spec.sim_latency_ms,
            fail_rate=spec.sim_fail_rate,
            seed=spec.seed,
        )

    if spec.reasoner == "llm":
        from agent.core.passive_turn import DefaultReasoner
        from agent.core.runtime_support import ToolDiscoveryState
        from agent.looping.ports import LLMConfig, LLMServices
        from agent.config_models import Config
        from bootstrap.tools import build_providers

        config = Config.load(Path(spec.workspace) / "config.toml")
        provider, light_provider, agent_provider = build_providers(config)
        llm_services = LLMServices(
            provider=agent_provider or provider,
            light_provider=light_provider,
        )
        return DefaultReasoner(
            llm=llm_services,
            llm_config=LLMConfig(
                model=config.agent_model or config.model,
                light_model=config.light_model,
                max_iterations=config.max_iterations,
                max_tokens=config.max_tokens,
                tool_search_enabled=False,
                multimodal=bool(getattr(config, "multimodal", True)),
                vl_available=bool(getattr(config, "vl_model", "")),
            ),
            tools=StubToolRegistry(),
            discovery=ToolDiscoveryState(),
            tool_search_enabled=False,
            memory_window=40,
            context=StubContextBuilder(),
            session_manager=None,
            event_bus=EventBus(),
            turn_logger=None,
        )

    raise ValueError(f"unknown reasoner mode: {spec.reasoner!r}")


def build_core(
    *,
    workspace: Path,
    storage: StorageConfig,
    spec: LoadSpec,
    trace_store: Any | None = None,
) -> tuple[AgentCore, SessionManager, Any]:
    """组装最小真实 passive turn 路径。返回 (core, session_manager, runtime)。

    trace_store 非空时经 AgentCoreDeps 接入 PassiveTurnPipeline，各 phase 边界
    记录 TraceSpan（3.2）。
    """
    workspace.mkdir(parents=True, exist_ok=True)
    runtime = create_storage_runtime(
        storage,
        workspace / "memory" / "memory2.db",
        workspace / "sessions.db",
    )
    session_manager = SessionManager(workspace, storage_runtime=runtime)
    reasoner = build_reasoner(spec)
    core = AgentCore(
        AgentCoreDeps(
            session=SessionServices(session_manager),
            context_store=StubContextStore(),
            context=StubContextBuilder(),
            tools=StubToolRegistry(),
            reasoner=reasoner,
            event_bus=EventBus(),
            outbound_port=None,
            history_window=500,
            memory_consolidator=None,
            before_turn_plugin_modules=None,
            before_reasoning_plugin_modules=None,
            before_step_plugin_modules=None,
            after_step_plugin_modules=None,
            after_reasoning_plugin_modules=None,
            after_turn_plugin_modules=None,
            turn_logger=None,
            trace_store=trace_store,
        )
    )
    return core, session_manager, runtime


_DEGRADED_REPLY = "处理消息时出错，请稍后再试。"


def _turn_succeeded(result: Any, content: str, reasoner_mode: str) -> bool:
    """按出站回复判定 turn 成功。

    - script：脚本回复固定为 echo:<content>，精确匹配即成功（reply=None / 异常降级
      文案都判失败）。
    - llm：非空且不是异常降级文案即为成功。
    """
    reply = getattr(result, "content", "") or ""
    if reasoner_mode == "script":
        return reply == f"echo:{content}"
    return bool(reply) and reply != _DEGRADED_REPLY


async def _drive_one(
    core: AgentCore,
    *,
    tenant: str,
    channel: str,
    chat_id: str,
    content: str,
    turn_seq: int,
    reasoner_mode: str,
    metric_builtin: BuiltinMetrics | None = None,
) -> tuple[bool, float]:
    msg = InboundMessage(
        channel=channel,
        sender="user",
        chat_id=chat_id,
        tenant_id=tenant,
        content=content,
        metadata={"skip_memory_context_guard": True},
    )
    key = f"{channel}:{chat_id}"
    turn_id = f"load-{time.time_ns():x}-{turn_seq}"
    session_token = current_session_key.set(key)
    turn_token = current_turn_id.set(turn_id)
    started = time.perf_counter()
    try:
        result = await core.process(msg, key, dispatch_outbound=False)
        ok = _turn_succeeded(result, content, reasoner_mode)
    except Exception as exc:  # noqa: BLE001 - 负载统计需要捕获全部失败
        logger.warning("load turn failed turn=%s tenant=%s channel=%s: %s",
                       turn_id, tenant, channel, type(exc).__name__)
        ok = False
    finally:
        current_session_key.reset(session_token)
        current_turn_id.reset(turn_token)
    elapsed = time.perf_counter() - started
    if metric_builtin is not None:
        metric_builtin.turns_total.inc(labels={"channel": channel})
        metric_builtin.turn_duration_seconds.observe(
            elapsed, labels={"channel": channel}
        )
    return ok, elapsed


async def drive_turns(
    core: AgentCore,
    spec: LoadSpec,
    metric_registry: Any | None = None,
) -> RunStats:
    """对每个 (tenant, channel) 跑 rounds 轮，整体并发受 spec.concurrency 限制。

    metric_registry 非空时注册内建 turn 指标族并把每轮耗时/结果记录进去
    （进程内指标导出，C0 4.2 观测面）。
    """
    stats = RunStats(backend=spec.backend)
    sem = asyncio.Semaphore(max(1, spec.concurrency))
    metric_builtin: BuiltinMetrics | None = (
        register_builtin_metrics(metric_registry)
        if metric_registry is not None
        else None
    )

    async def one(tenant: str, channel: str, chat_id: str, content: str, seq: int) -> None:
        async with sem:
            ok, latency = await _drive_one(
                core,
                tenant=tenant,
                channel=channel,
                chat_id=chat_id,
                content=content,
                turn_seq=seq,
                reasoner_mode=spec.reasoner,
                metric_builtin=metric_builtin,
            )
            stats.record(tenant, channel, ok=ok, latency_s=latency)

    tasks: list[asyncio.Task[None]] = []
    seq = 0
    for tenant in spec.tenants:
        for channel in spec.channels:
            chat_id = f"load-{tenant}"
            for _ in range(spec.rounds):
                seq += 1
                tasks.append(
                    asyncio.create_task(
                        one(tenant, channel, chat_id, f"load-msg-{seq}", seq)
                    )
                )
    await asyncio.gather(*tasks)
    return stats
