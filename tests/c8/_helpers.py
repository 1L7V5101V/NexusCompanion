"""C8 测试辅助：最小 RuntimeSnapshot / Store / RevocationGate provider。

构造最小快照时不需要真实插件 generation——RuntimeSnapshot 是可直接实例化的
dataclass，generations 为空即满足 lease/binding 语义测试所需。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any

from agent.admission.revocation import TenantStatus
from agent.plugins.jobs import RegisteredPluginJob
from agent.plugins.snapshot import RuntimeSnapshot, RuntimeSnapshotStore


def make_snapshot(
    snapshot_id: str,
    *,
    jobs: Mapping[str, RegisteredPluginJob] | None = None,
) -> RuntimeSnapshot:
    """构造满足 RuntimeSnapshot 必需字段的最小实例（generations 为空）。"""
    return RuntimeSnapshot(
        snapshot_id=snapshot_id,
        generations=MappingProxyType({}),
        before_turn_modules=(),
        before_reasoning_modules=(),
        prompt_render_modules=(),
        before_step_modules=(),
        after_step_modules=(),
        after_reasoning_modules=(),
        after_turn_modules=(),
        jobs=MappingProxyType(dict(jobs or {})),
        proactive_sources=MappingProxyType({}),
        proactive_modules=(),
        proactive_lifecycles=(),
        proactive_module_factories=(),
        proactive_runtime_factories=(),
        tool_hooks=(),
        channels=MappingProxyType({}),
        skill_catalog_generation_id=None,
        mcp_catalog_generation_ids=MappingProxyType({}),
    )


def make_store(
    snapshot_id: str = "snap-a",
    *,
    jobs: Mapping[str, RegisteredPluginJob] | None = None,
) -> RuntimeSnapshotStore:
    """构造已安装单快照的 store。"""
    store = RuntimeSnapshotStore()
    store.install(make_snapshot(snapshot_id, jobs=jobs))
    return store


def make_status_provider(
    statuses: list[TenantStatus],
) -> Callable[[str], Awaitable[TenantStatus]]:
    """按调用顺序逐个返回 status 的 provider（用尽后固定为最后一个）。"""
    iterator = iter(statuses)

    async def provider(tenant_id: str) -> TenantStatus:
        try:
            return next(iterator)
        except StopIteration:
            return statuses[-1]

    return provider


def make_active_provider() -> Callable[[str], Awaitable[TenantStatus]]:
    async def provider(tenant_id: str) -> TenantStatus:
        return TenantStatus.ACTIVE

    return provider


def make_dummy_llm() -> Any:
    """满足 PluginLlmService 协议形状的 dummy（gate 测试不真正调用）。"""

    class _Dummy:
        async def generate_text(self, **kwargs: Any) -> str:
            raise AssertionError("gate 拒绝路径不应调用 llm")

    return _Dummy()