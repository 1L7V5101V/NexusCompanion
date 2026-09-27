"""C8 P2 per-task tenant runtime plan 接缝测试（task-08 2.1）。

覆盖：contribution_id 稳定 + 固定 kind + binding_policy/tenant_configurable
元数据断言；resolver 启用语义（required 不可关 / default_on 默认开 / opt_in
显式开）；未在 plan 中的 contribution 不可见（allows=False）；plan 不可变且
相同输入产出等价 plan；PluginInvocationContext 显式携带 plan/work。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from agent.plugins.generation import GateResult, PluginContributions, PluginGeneration
from agent.plugins.jobs import PluginJobSpec, RegisteredPluginJob
from agent.plugins.snapshot import RuntimeSnapshot
from agent.plugins.specs import ProactiveSourceSpec, RegisteredProactiveSource
from agent.plugins.tenant_plan import (
    ContributionMeta,
    PluginInvocationContext,
    TenantRuntimePlan,
    TenantRuntimeResolver,
    WorkContext,
    build_contribution_catalog,
)


class _SlotModule:
    def __init__(self, slot: str) -> None:
        self.slot = slot


def _make_generation(
    plugin_id: str,
    *,
    modules: tuple[object, ...] = (),
    jobs: tuple[RegisteredPluginJob, ...] = (),
    sources: tuple[RegisteredProactiveSource, ...] = (),
    instance: object | None = None,
) -> PluginGeneration:
    return PluginGeneration(
        plugin_id=plugin_id,
        generation_id=f"{plugin_id}-g1",
        module_path=f"plugins/{plugin_id}/plugin.py",
        source_revision="rev0",
        config_revision="cfg0",
        instance=instance if instance is not None else SimpleNamespace(),
        scope=SimpleNamespace(),
        contributions=PluginContributions(
            manifest={},
            before_turn_modules=modules,
            jobs=jobs,
            proactive_sources=sources,
        ),
        gate_result=GateResult(
            gate_id=f"{plugin_id}-gate",
            plugin_id=plugin_id,
            candidate_revision="rev0",
            status="passed",
            checks=(),
        ),
    )


def _make_snapshot(generations: dict[str, PluginGeneration]) -> RuntimeSnapshot:
    from tests.c8._helpers import make_snapshot

    base = make_snapshot("snap-plan")
    return RuntimeSnapshot(
        snapshot_id=base.snapshot_id,
        generations=MappingProxyType(dict(generations)),
        before_turn_modules=base.before_turn_modules,
        before_reasoning_modules=base.before_reasoning_modules,
        prompt_render_modules=base.prompt_render_modules,
        before_step_modules=base.before_step_modules,
        after_step_modules=base.after_step_modules,
        after_reasoning_modules=base.after_reasoning_modules,
        after_turn_modules=base.after_turn_modules,
        jobs=base.jobs,
        proactive_sources=base.proactive_sources,
        proactive_modules=base.proactive_modules,
        proactive_lifecycles=base.proactive_lifecycles,
        proactive_module_factories=base.proactive_module_factories,
        proactive_runtime_factories=base.proactive_runtime_factories,
        tool_hooks=base.tool_hooks,
        channels=base.channels,
        skill_catalog_generation_id=None,
        mcp_catalog_generation_ids=base.mcp_catalog_generation_ids,
    )


def _make_job(plugin_id: str, job_id: str) -> RegisteredPluginJob:
    async def _handler(ctx: object) -> None:
        raise AssertionError("测试不执行 job handler")

    return RegisteredPluginJob(
        plugin_id=plugin_id,
        plugin_context=None,
        spec=PluginJobSpec(id=job_id, triggers=(), handler=_handler),
    )


def _make_source(plugin_id: str, source_id: str) -> RegisteredProactiveSource:
    return RegisteredProactiveSource(
        plugin_id=plugin_id,
        spec=ProactiveSourceSpec(
            id=source_id,
            channels=("alert",),
            server="s",
            fetch_tool="f",
        ),
    )


def test_contribution_metadata_fixed_and_stable() -> None:
    """contribution_id 稳定 + 固定 hook/tool/job 类型 + 声明元数据固化。"""
    module = _SlotModule("alpha.greeter")
    job = _make_job("alpha", "digest")
    source = _make_source("alpha", "inbox")
    instance = SimpleNamespace(
        binding_policies={
            "alpha.greeter": ("required", False),
            "alpha:digest": ("default_on", True),
        }
    )
    snapshot = _make_snapshot(
        {"alpha": _make_generation("alpha", modules=(module,), jobs=(job,), sources=(source,), instance=instance)}
    )
    catalog = build_contribution_catalog(snapshot)

    assert catalog["alpha.greeter"] == ContributionMeta(
        contribution_id="alpha.greeter",
        kind="hook",
        plugin_id="alpha",
        binding_policy="required",
        tenant_configurable=False,
    )
    assert catalog["alpha:digest"].kind == "job"
    assert catalog["alpha:digest"].binding_policy == "default_on"
    assert catalog["alpha:inbox"].kind == "job"
    # 未声明的 contribution 默认 opt_in + tenant_configurable
    assert catalog["alpha:inbox"].binding_policy == "opt_in"
    assert catalog["alpha:inbox"].tenant_configurable is True

    # 同一 snapshot 重复派生 → 完全一致的稳定 id
    again = build_contribution_catalog(snapshot)
    assert set(again) == set(catalog)


def test_resolver_required_cannot_be_disabled() -> None:
    module = _SlotModule("alpha.gate")
    snapshot = _make_snapshot(
        {
            "alpha": _make_generation(
                "alpha",
                modules=(module,),
                instance=SimpleNamespace(
                    binding_policies={"alpha.gate": ("required", False)}
                ),
            )
        }
    )
    resolver = TenantRuntimeResolver()
    plan = resolver.resolve(
        snapshot,
        tenant_id="t1",
        tenant_policy_revision="r0",
    )
    assert plan.allows("alpha.gate")
    with pytest.raises(ValueError, match="不可关闭"):
        resolver.resolve(
            snapshot,
            tenant_id="t1",
            tenant_policy_revision="r0",
            bindings={"alpha.gate": False},
        )


def test_resolver_default_on_and_opt_in_binding_semantics() -> None:
    default_on = _SlotModule("alpha.memory")
    opt_in = _SlotModule("alpha.extra")
    snapshot = _make_snapshot(
        {
            "alpha": _make_generation(
                "alpha",
                modules=(default_on, opt_in),
                instance=SimpleNamespace(
                    binding_policies={
                        "alpha.memory": ("default_on", True),
                        "alpha.extra": ("opt_in", True),
                    }
                ),
            )
        }
    )
    resolver = TenantRuntimeResolver()

    plan = resolver.resolve(snapshot, tenant_id="t1", tenant_policy_revision="r0")
    assert plan.allows("alpha.memory")
    assert not plan.allows("alpha.extra")

    disabled = resolver.resolve(
        snapshot,
        tenant_id="t1",
        tenant_policy_revision="r1",
        bindings={"alpha.memory": False},
    )
    assert not disabled.allows("alpha.memory")

    enabled = resolver.resolve(
        snapshot,
        tenant_id="t1",
        tenant_policy_revision="r1",
        bindings={"alpha.extra": True},
    )
    assert enabled.allows("alpha.extra")


def test_resolver_rejects_unknown_binding_and_empty_tenant() -> None:
    snapshot = _make_snapshot({})
    resolver = TenantRuntimeResolver()
    with pytest.raises(ValueError, match="未知 contribution"):
        resolver.resolve(
            snapshot,
            tenant_id="t1",
            tenant_policy_revision="r0",
            bindings={"ghost": True},
        )
    with pytest.raises(ValueError, match="tenant_id"):
        resolver.resolve(snapshot, tenant_id="", tenant_policy_revision="r0")
    with pytest.raises(ValueError, match="tenant_policy_revision"):
        resolver.resolve(snapshot, tenant_id="t1", tenant_policy_revision="")


def test_plan_is_immutable_and_reproducible() -> None:
    module = _SlotModule("alpha.m1")
    snapshot = _make_snapshot({"alpha": _make_generation("alpha", modules=(module,))})
    resolver = TenantRuntimeResolver()
    kwargs = {
        "tenant_id": "t1",
        "tenant_policy_revision": "r0",
        "bindings": {"alpha.m1": True},
    }
    plan_a = resolver.resolve(snapshot, **kwargs)
    plan_b = resolver.resolve(snapshot, **kwargs)
    assert plan_a.snapshot_id == plan_b.snapshot_id
    assert plan_a.enabled == plan_b.enabled
    assert set(plan_a.catalog) == set(plan_b.catalog)

    with pytest.raises(FrozenInstanceError):
        plan_a.enabled = frozenset()  # type: ignore[misc]
    with pytest.raises(TypeError):
        plan_a.catalog["patched"] = plan_a.catalog["alpha.m1"]  # type: ignore[index]


def test_plan_not_listed_contribution_not_allowed() -> None:
    """未在 plan 中的 contribution 不可见/不可调用（allows 唯一判定）。"""
    seen = _SlotModule("alpha.seen")
    snapshot = _make_snapshot(
        {
            "alpha": _make_generation(
                "alpha",
                modules=(seen,),
                instance=SimpleNamespace(
                    binding_policies={"alpha.seen": ("default_on", True)}
                ),
            )
        }
    )
    resolver = TenantRuntimeResolver()
    plan = resolver.resolve(snapshot, tenant_id="t1", tenant_policy_revision="r0")
    assert plan.allows("alpha.seen")
    assert not plan.allows("alpha.absent")
    assert plan.meta("alpha.absent") is None
    assert plan.engine_binding is None


def test_invocation_context_carries_plan_and_work() -> None:
    module = _SlotModule("alpha.m1")
    snapshot = _make_snapshot(
        {
            "alpha": _make_generation(
                "alpha",
                modules=(module,),
                instance=SimpleNamespace(
                    binding_policies={"alpha.m1": ("default_on", True)}
                ),
            )
        }
    )
    resolver = TenantRuntimeResolver()
    plan = resolver.resolve(
        snapshot,
        tenant_id="t1",
        tenant_policy_revision="r0",
        engine_binding="default",
    )
    ctx = PluginInvocationContext.for_work(
        plan,
        work_id="w1",
        turn_id="turn-1",
        session_key="chat:1",
        work_kind="interactive",
    )
    assert ctx.plan is plan
    assert ctx.work == WorkContext(
        work_id="w1",
        turn_id="turn-1",
        session_key="chat:1",
        tenant_id="t1",
        work_kind="interactive",
    )
    assert ctx.snapshot_id == plan.snapshot_id
    assert ctx.allows("alpha.m1")
    assert not ctx.allows("alpha.absent")
    with pytest.raises(FrozenInstanceError):
        ctx.work = WorkContext(work_id="w2")  # type: ignore[misc]


def test_resolver_engine_binding_passthrough(tmp_path: Path) -> None:
    snapshot = _make_snapshot({})
    resolver = TenantRuntimeResolver()
    plan = resolver.resolve(
        snapshot,
        tenant_id="t1",
        tenant_policy_revision="r0",
        engine_binding="rachael",
    )
    assert isinstance(plan, TenantRuntimePlan)
    assert plan.engine_binding == "rachael"
    assert plan.tenant_id == "t1"
