"""C8 per-task tenant runtime 接缝（task-08 P2，§5.9.16 / §5.9.7）。

冻结决策：共享 ``PluginManager`` 只承载当前有实际绑定需求的插件 union；每个
work 由 ``TenantRuntimeResolver`` 根据 (``snapshot_id``, ``tenant_id``,
``tenant_policy_revision``) 生成不可变 ``TenantRuntimePlan``；每个可选择能力项
使用稳定 ``contribution_id``、固定 hook/tool/job 类型和是否
``tenant_configurable``；未在 plan 中的 contribution 不可见、不可调用、不可由
后台任务隐式触发。每次 hook/tool/job 调用创建 ``PluginInvocationContext``，
显式携带不可变 ``WorkContext``；插件实例不得保存跨 await 的当前 tenant 状态，
``ContextVar`` 只做观测和兼容，不做授权边界。

本模块只交付 seam 与元数据契约；把 plan 接进工具执行（TenantToolCatalog）归
C7（E4），tenant binding/config 的 durable 存储与 revision 提升归 C14/C5。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Mapping

from agent.plugins.jobs import plugin_job_key
from agent.plugins.specs import proactive_source_key

if TYPE_CHECKING:
    from agent.plugins.generation import PluginGeneration
    from agent.plugins.snapshot import RuntimeSnapshot

#: 平台正确性能力（租户隔离、认证 gate、memory engine slot）= required 且不可
#: 关闭；产品基础能力（记忆、默认工具）= default_on（provisioning 时自动绑定）；
#: 普通扩展 = opt_in（管理员显式启用前不进 plan）。
BindingPolicy = Literal["required", "default_on", "opt_in"]

#: contribution 固定类型（§5.9.16：固定 hook/tool/job 类型，不得跨类搬运）。
ContributionKind = Literal["hook", "tool", "job"]

ContributionDeclaration = tuple[BindingPolicy, bool]
"""插件对单个 contribution 的声明：``(binding_policy, tenant_configurable)``。

声明来源：插件实例类属性 ``binding_policies``（contribution_id → declaration）；
未声明的 contribution 默认 ``opt_in + tenant_configurable=True``。
"""

_PHASE_FIELDS = (
    "before_turn_modules",
    "before_reasoning_modules",
    "prompt_render_modules",
    "before_step_modules",
    "after_step_modules",
    "after_reasoning_modules",
    "after_turn_modules",
)


@dataclass(frozen=True)
class ContributionMeta:
    """单个可选择能力项的固化元数据。"""

    contribution_id: str
    kind: ContributionKind
    plugin_id: str
    binding_policy: BindingPolicy = "opt_in"
    tenant_configurable: bool = True


@dataclass(frozen=True)
class WorkContext:
    """一次 work 的不可变上下文（由 work runtime 构造，插件不得伪造）。"""

    work_id: str
    turn_id: str = ""
    session_key: str = ""
    tenant_id: str = ""
    work_kind: str = ""


@dataclass(frozen=True)
class TenantRuntimePlan:
    """per-work 不可变 runtime plan：可见性判定的唯一依据。"""

    snapshot_id: str
    tenant_id: str
    tenant_policy_revision: str
    catalog: Mapping[str, ContributionMeta] = field(
        default_factory=lambda: MappingProxyType({})
    )
    enabled: frozenset[str] = frozenset()
    engine_binding: str | None = None

    def allows(self, contribution_id: str) -> bool:
        """未在 plan 中的 contribution 不可见、不可调用、不可隐式触发。"""
        return contribution_id in self.enabled

    def meta(self, contribution_id: str) -> ContributionMeta | None:
        return self.catalog.get(contribution_id)


def _module_contribution_id(plugin_id: str, field_name: str, module: object, index: int) -> str:
    slot = getattr(module, "slot", None)
    if isinstance(slot, str) and slot:
        return slot
    return f"{plugin_id}:{field_name}:{index}"


def _declaration_for(
    declarations: Mapping[str, ContributionDeclaration],
    contribution_id: str,
) -> ContributionDeclaration:
    found = declarations.get(contribution_id)
    if found is not None:
        return found
    return ("opt_in", True)


def build_contribution_catalog(
    snapshot: RuntimeSnapshot,
    *,
    declarations: Mapping[str, ContributionDeclaration] | None = None,
) -> dict[str, ContributionMeta]:
    """从 snapshot 内容派生全量 contribution 索引（含插件声明合并）。

    同一 snapshot 派生的 contribution_id 稳定（slot / 稳定键 / 确定性合成），
    满足「稳定 contribution_id + 固定 hook/tool/job 类型」断言。
    """
    merged: dict[str, ContributionDeclaration] = {}
    for plugin_id, generation in snapshot.generations.items():
        instance_policies = getattr(generation.instance, "binding_policies", None)
        if isinstance(instance_policies, Mapping):
            for contribution_id, declaration in instance_policies.items():
                merged[str(contribution_id)] = (
                    declaration[0],
                    bool(declaration[1]),
                )
    if declarations:
        merged.update(declarations)

    catalog: dict[str, ContributionMeta] = {}

    def _add(
        contribution_id: str,
        kind: ContributionKind,
        plugin_id: str,
        *,
        policy: BindingPolicy | None = None,
        configurable: bool | None = None,
    ) -> None:
        if contribution_id in catalog:
            raise ValueError(f"contribution_id 冲突: {contribution_id}")
        if policy is None:
            resolved_policy, resolved_configurable = _declaration_for(
                merged, contribution_id
            )
        else:
            # 进程级 channel/managed service 只允许管理员控制（§5.9.16），
            # 不参与 tenant binding 声明。
            resolved_policy = policy
            resolved_configurable = (
                configurable if configurable is not None else False
            )
        catalog[contribution_id] = ContributionMeta(
            contribution_id=contribution_id,
            kind=kind,
            plugin_id=plugin_id,
            binding_policy=resolved_policy,
            tenant_configurable=resolved_configurable,
        )

    for plugin_id, generation in snapshot.generations.items():
        contributions = generation.contributions
        for field_name in _PHASE_FIELDS:
            for index, module in enumerate(getattr(contributions, field_name, ())):
                _add(
                    _module_contribution_id(plugin_id, field_name, module, index),
                    "hook",
                    plugin_id,
                )
        for index, module in enumerate(contributions.proactive_modules):
            _add(
                _module_contribution_id(plugin_id, "proactive_modules", module, index),
                "hook",
                plugin_id,
            )
        for index, module in enumerate(contributions.proactive_lifecycles):
            _add(
                _module_contribution_id(plugin_id, "proactive_lifecycles", module, index),
                "hook",
                plugin_id,
            )
        for job in contributions.jobs:
            _add(plugin_job_key(job), "job", plugin_id)
        for source in contributions.proactive_sources:
            _add(proactive_source_key(source), "job", plugin_id)
        # 进程级 channel / managed service：admin-controlled，required 且不可配置。
        for channel in contributions.channels:
            _add(
                f"{plugin_id}:channel:{channel.name}",
                "hook",
                plugin_id,
                policy="required",
                configurable=False,
            )
        for service_name in contributions.managed_services:
            _add(
                f"{plugin_id}:service:{service_name}",
                "hook",
                plugin_id,
                policy="required",
                configurable=False,
            )
    # tool_hooks 在 snapshot 上聚合（不带 plugin 归属），按 hook 名登记。
    for hook in snapshot.tool_hooks:
        _add(
            str(getattr(hook, "name", "") or f"hook:{id(hook):x}"),
            "hook",
            str(getattr(hook, "plugin_id", "") or ""),
        )
    return catalog


class TenantRuntimeResolver:
    """按 (snapshot, tenant_id, tenant_policy_revision) 解析不可变 plan。

    相同输入产出等价 plan；binding 语义（§5.9.16 provisioning 规则）：
    - ``required`` 恒启用，binding 尝试关闭 → ``ValueError``（不可关闭）；
    - ``default_on`` 默认启用，binding 显式 ``False`` 关闭；
    - ``opt_in`` 默认不启用，binding 显式 ``True`` 启用；
    - binding 指向未知 contribution → ``ValueError``（不制造幽灵绑定）。
    """

    def resolve(
        self,
        snapshot: RuntimeSnapshot,
        *,
        tenant_id: str,
        tenant_policy_revision: str,
        bindings: Mapping[str, bool] | None = None,
        declarations: Mapping[str, ContributionDeclaration] | None = None,
        engine_binding: str | None = None,
    ) -> TenantRuntimePlan:
        if not tenant_id:
            raise ValueError("tenant_id 不能为空（§5.9.5 禁止默认租户回退）")
        if not tenant_policy_revision:
            raise ValueError("tenant_policy_revision 不能为空")
        catalog = build_contribution_catalog(snapshot, declarations=declarations)
        binding_map = dict(bindings or {})
        unknown = sorted(set(binding_map) - set(catalog))
        if unknown:
            raise ValueError(f"binding 引用未知 contribution: {', '.join(unknown)}")
        enabled: set[str] = set()
        for contribution_id, meta in catalog.items():
            if meta.binding_policy == "required":
                if binding_map.get(contribution_id) is False:
                    raise ValueError(
                        f"required contribution 不可关闭: {contribution_id}"
                    )
                enabled.add(contribution_id)
            elif meta.binding_policy == "default_on":
                if binding_map.get(contribution_id, True):
                    enabled.add(contribution_id)
            else:
                if binding_map.get(contribution_id) is True:
                    enabled.add(contribution_id)
        return TenantRuntimePlan(
            snapshot_id=snapshot.snapshot_id,
            tenant_id=tenant_id,
            tenant_policy_revision=tenant_policy_revision,
            catalog=MappingProxyType(catalog),
            enabled=frozenset(enabled),
            engine_binding=engine_binding,
        )


@dataclass(frozen=True)
class PluginInvocationContext:
    """每次 hook/tool/job 调用显式携带的 per-call 上下文（§5.9.7）。

    授权判定一律读 ``plan``；``ContextVar`` 只做观测和兼容，不做授权边界。
    """

    plan: TenantRuntimePlan
    work: WorkContext
    snapshot_id: str = ""

    @classmethod
    def for_work(
        cls,
        plan: TenantRuntimePlan,
        *,
        work_id: str,
        turn_id: str = "",
        session_key: str = "",
        work_kind: str = "",
        snapshot_id: str | None = None,
    ) -> PluginInvocationContext:
        return cls(
            plan=plan,
            work=WorkContext(
                work_id=work_id,
                turn_id=turn_id,
                session_key=session_key,
                tenant_id=plan.tenant_id,
                work_kind=work_kind,
            ),
            snapshot_id=snapshot_id or plan.snapshot_id,
        )

    def allows(self, contribution_id: str) -> bool:
        return self.plan.allows(contribution_id)

    def with_work_id(self, work_id: str) -> PluginInvocationContext:
        return replace(self, work=replace(self.work, work_id=work_id))


_current_invocation: ContextVar[PluginInvocationContext | None] = ContextVar(
    "current_plugin_invocation",
    default=None,
)


def bind_invocation_context(
    context: PluginInvocationContext,
) -> object:
    """观测用绑定；返回 token 供 :func:`reset_invocation_context` 复原。

    仅用于观测/兼容层——任何授权判断不得依赖该 ContextVar（§5.9.16）。
    """
    return _current_invocation.set(context)


def reset_invocation_context(token: object) -> None:
    _current_invocation.reset(token)  # type: ignore[arg-type]


def current_invocation_context() -> PluginInvocationContext | None:
    return _current_invocation.get()
