from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Set as AbstractSet
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from agent.admission.tool_audit import ToolAuditEvent, redact_arguments
from agent.tools.base import Tool, ToolEffect, ToolResult, RISK_TO_EFFECT
from agent.tools.context import ToolExecutionContext
from agent.tools.search_backend import KeywordSearchBackend, SearchBackend

logger = logging.getLogger(__name__)

# 元工具（不参与搜索结果，也不出现在 deferred 工具目录里）
_META_TOOLS: frozenset[str] = frozenset({"tool_search"})


def _classify_tool_result(result: str) -> tuple[str, str | None]:
    """从工具返回串判定审计终态（C7 task 7.1/ADR-6）。

    - 租户取消结构化结果 → cancelled；
    - 其它 ``code=`` 拒绝消息 → rejected（带 error_code，执行前拒绝）；
    - 其余字符串结果视为成功输出。
    """
    if "tool_cancelled_account_status" in result:
        return "cancelled", "tool_cancelled_account_status"
    if "code=" in result:
        code = (
            result.split("code=", 1)[1].split("）", 1)[0].split("，", 1)[0].strip()
        )
        if code:
            return "rejected", code
    return "succeeded", None

# C7（§5.9.7 / design ADR-2）：模型与客户端帧 SHALL NOT 提供或覆盖可信归属字段。
# 这些 key 一律从 arguments 剥离；可信值只能经 ToolExecutionContext 注入
# （tool_kwargs() 以最高优先级覆盖），共享可变 set_context() 不再参与授权。
TRUST_ARGUMENT_FIELDS: frozenset[str] = frozenset(
    {
        "tenant_id",
        "account_id",
        "session_id",
        "session_key",
        "turn_id",
        "request_id",
        "principal_type",
        "channel",
        "chat_id",
    }
)

# 路由目标字段（channel/chat_id）：user principal 一律剥离（message_push 只能
# 推送到服务端绑定目标，task 3.3）；dev/owner 保留参数覆盖（合法跨目标推送）。
ROUTING_ARGUMENT_FIELDS: frozenset[str] = frozenset({"channel", "chat_id"})
_PROGRESS_DESCRIPTION_FIELD = "description"
_PROGRESS_DESCRIPTION_SCHEMA: dict[str, str] = {
    "type": "string",
    "description": (
        "用 5-12 个字说明这次工具调用的意图，只写给用户看的短语。"
        "不要复述工具名，不要粘贴长参数。例如：查看目录、读取配置、搜索健康数据。"
    ),
}


def _schema_properties(parameters: dict[str, Any]) -> dict[str, Any]:
    raw_properties = parameters.get("properties")
    if isinstance(raw_properties, dict):
        return cast(dict[str, Any], raw_properties)
    properties: dict[str, Any] = {}
    parameters["properties"] = properties
    return properties


def _tool_defines_parameter(tool: Tool, name: str) -> bool:
    parameters: dict[str, Any] = tool.parameters or {}
    properties = parameters.get("properties")
    return isinstance(properties, dict) and name in properties


def _with_progress_description(schema: dict[str, Any], tool: Tool) -> dict[str, Any]:
    cloned = cast(dict[str, Any], deepcopy(schema))
    function = cloned.get("function")
    if not isinstance(function, dict):
        return cloned
    function = cast(dict[str, Any], function)
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        return cloned
    parameters = cast(dict[str, Any], parameters)
    if _tool_defines_parameter(tool, _PROGRESS_DESCRIPTION_FIELD):
        return cloned
    properties = _schema_properties(parameters)
    properties[_PROGRESS_DESCRIPTION_FIELD] = dict(_PROGRESS_DESCRIPTION_SCHEMA)
    required = parameters.get("required")
    if isinstance(required, list):
        if _PROGRESS_DESCRIPTION_FIELD not in required:
            cast(list[Any], required).append(_PROGRESS_DESCRIPTION_FIELD)
    else:
        parameters["required"] = [_PROGRESS_DESCRIPTION_FIELD]
    return cloned


# ── ToolMeta ──────────────────────────────────────────────────────────────────


@dataclass
class ToolMeta:
    risk: str = "read-only"  # "read-only" | "write" | "external-side-effect"（存量标签）
    always_on: bool = False
    # 可选：3–10 词短语，补充工具名和描述中没有的别名或口语化表达。
    # 不需要重复名称或描述里已有的词——搜索后端自动索引 name + description。
    search_hint: str | None = None
    # C7 task 4.1：作用等级（缺省由 risk 标签保守映射；注册点可显式覆盖，
    # 见 effect-mapping.md）与补偿登记（external-write 无登记即默认拒绝）。
    effect: ToolEffect = ToolEffect.READ_ONLY
    requires_compensation: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.effect, ToolEffect):
            self.effect = ToolEffect(self.effect)


# ── ToolDocument ──────────────────────────────────────────────────────────────


@dataclass
class ToolDocument:
    """工具的索引态视图，派生自 Tool + ToolMeta，供搜索后端使用。

    搜索后端自动索引：name、description。
    search_hint 是可选补充，仅在名称和描述无法覆盖某些口语别名时填写。
    """

    name: str
    description: str
    risk: str
    always_on: bool
    search_hint: str | None
    source_type: str  # "builtin" | "mcp"
    source_name: str  # mcp server 名，builtin 为空字符串

    @classmethod
    def from_tool_and_meta(
        cls,
        tool: "Tool",
        meta: ToolMeta,
        source_type: str = "builtin",
        source_name: str = "",
    ) -> "ToolDocument":
        return cls(
            name=tool.name,
            description=tool.description,
            risk=meta.risk,
            always_on=meta.always_on,
            search_hint=meta.search_hint,
            source_type=source_type,
            source_name=source_name,
        )


# ── ToolRegistry ──────────────────────────────────────────────────────────────


class ToolRegistry:
    """管理所有可用工具"""

    def __init__(self, backend: SearchBackend | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._metadata: dict[str, ToolMeta] = {}
        self._documents: dict[str, ToolDocument] = {}
        self._backend: SearchBackend = backend or KeywordSearchBackend()
        # C7 task 5.1：租户路径解析器（进程级无状态配置对象，bootstrap 设置一次；
        # 每次执行时注入 kwargs，文件工具据此按租户解析 root）。
        self._path_resolver: Any | None = None
        self._audit_sink: Any | None = None

    def set_path_resolver(self, resolver: Any) -> None:
        self._path_resolver = resolver

    def set_audit_sink(self, sink: Any) -> None:
        """C7 task 7.1（ADR-6）：接入审计写入接缝（PG INSERT / 单机日志兜底）。

        未接线（None）时行为与历史一致：不产生审计行。
        """
        self._audit_sink = sink

    def register(
        self,
        tool: Tool,
        *,
        risk: str = "read-only",
        always_on: bool = False,
        search_hint: str | None = None,
        source_type: str = "builtin",
        source_name: str = "",
        effect: ToolEffect | str | None = None,
        requires_compensation: bool = False,
    ) -> None:
        self._tools[tool.name] = tool
        meta = ToolMeta(
            risk=risk,
            always_on=always_on,
            search_hint=search_hint,
            effect=effect if effect is not None else RISK_TO_EFFECT.get(risk, ToolEffect.READ_ONLY),
            requires_compensation=requires_compensation,
        )
        self._metadata[tool.name] = meta
        doc = ToolDocument.from_tool_and_meta(
            tool, meta, source_type=source_type, source_name=source_name
        )
        self._documents[tool.name] = doc
        self._backend.add(doc)
        logger.debug(f"注册工具: {tool.name}")

    def unregister(self, name: str) -> None:
        _ = self._tools.pop(name, None)
        _ = self._metadata.pop(name, None)
        _ = self._documents.pop(name, None)
        self._backend.remove(name)
        logger.debug(f"注销工具: {name}")

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def get_tool(self, name: str) -> "Tool | None":
        return self._tools.get(name)

    def fork(
        self,
        *,
        excluded_source_types: set[str] | None = None,
        excluded_sources: set[tuple[str, str]] | None = None,
    ) -> ToolRegistry:
        """创建当前 registry 的副本，排除指定来源的工具。

        Args:
            excluded_source_types: 排除整个来源类型（如 {"plugin"}）。
            excluded_sources: 排除特定 (source_type, source_name) 组合。
        """
        fork_registry = ToolRegistry(backend=self._backend)
        for name, tool in self._tools.items():
            meta = self._metadata.get(name)
            doc = self._documents.get(name)
            if doc is None:
                continue
            # 排除整个来源类型
            if excluded_source_types and doc.source_type in excluded_source_types:
                continue
            # 排除特定来源
            if excluded_sources and (doc.source_type, doc.source_name) in excluded_sources:
                continue
            fork_registry.register(
                tool,
                risk=meta.risk if meta else "read-only",
                always_on=meta.always_on if meta else False,
                search_hint=meta.search_hint if meta else None,
                source_type=doc.source_type,
                source_name=doc.source_name,
                effect=meta.effect if meta else ToolEffect.READ_ONLY,
                requires_compensation=meta.requires_compensation if meta else False,
            )
        return fork_registry

    def get_registered_names(self) -> set[str]:
        """返回当前已注册工具名集合。"""
        return set(self._tools.keys())

    def get_schemas(
        self,
        names: AbstractSet[str] | Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """返回 OpenAI function calling 格式的工具定义列表。

        names 为 None 时返回全量；传 set 时按注册顺序过滤；传 list/tuple 时按调用方顺序返回。
        """
        if names is None:
            return [
                _with_progress_description(t.to_schema(), t)
                for t in self._tools.values()
            ]
        if not isinstance(names, AbstractSet):
            return [
                _with_progress_description(tool.to_schema(), tool)
                for name in names
                if (tool := self._tools.get(name)) is not None
            ]
        return [
            _with_progress_description(t.to_schema(), t)
            for name, t in self._tools.items()
            if name in names
        ]

    def get_registered_order(self, names: AbstractSet[str] | None = None) -> list[str]:
        if names is None:
            return list(self._tools.keys())
        return [name for name in self._tools.keys() if name in names]

    def get_always_on_names(self) -> set[str]:
        """返回标记为 always_on 的工具名称集合。"""
        return {name for name, meta in self._metadata.items() if meta.always_on}

    def get_documents(self) -> list[ToolDocument]:
        """返回所有已注册工具的索引文档列表。"""
        return list(self._documents.values())

    def get_deferred_names(
        self, visible: set[str] | None = None
    ) -> dict[str, object]:
        """返回所有 deferred 工具名，按来源分组。

        visible: 当前 turn 已可见工具名（always_on + preloaded），从结果中排除。
        deferred = 全量注册工具 - always_on - meta_tools - visible
        格式: {"builtin": [...], "mcp": {"server_name": [...], ...}}
        """
        always_on = self.get_always_on_names()
        excluded = always_on | _META_TOOLS | (visible or set())
        builtin: list[str] = []
        mcp: dict[str, list[str]] = {}

        for name, doc in self._documents.items():
            if name in excluded:
                continue
            if doc.source_type == "mcp":
                mcp.setdefault(doc.source_name, []).append(name)
            else:
                builtin.append(name)

        return {
            "builtin": sorted(builtin),
            "mcp": {k: sorted(v) for k, v in sorted(mcp.items())},
        }

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> str | ToolResult:
        """执行工具调用并写审计终态行（C7 task 7.1/ADR-6）。

        status 判定：执行前拒绝（rejected，error_code 取自返回串 ``code=``）；
        执行异常 → failed；租户取消结构化结果 → cancelled；外层 turn 取消→
        先写 cancelled 审计再原样上抛（不吞取消）；其余成功 → succeeded。
        """
        started_at = time.monotonic()
        audit_args: dict[str, Any] = {}
        try:
            result = await self._execute_inner(name, arguments, context, audit_args)
            if isinstance(result, str):
                status, error_code = _classify_tool_result(result)
            else:
                status, error_code = "succeeded", None
        except asyncio.CancelledError:
            await self._emit_audit(
                name, context, audit_args, started_at, "cancelled", None
            )
            raise
        except Exception as exc:
            logger.error(f"工具 {name} 执行出错: {exc}", exc_info=True)
            result = f"工具执行出错: {exc}"
            status, error_code = "failed", None
        await self._emit_audit(name, context, audit_args, started_at, status, error_code)
        return result

    async def _execute_inner(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None,
        audit_args: dict[str, Any],
    ) -> str | ToolResult:
        """执行体：剥离信任字段 → 注入 context → effect 闸门 → 执行/取消管理。

        执行异常向上一级（execute）传播，由审计包装层判定 failed（C7 task 7.1）；
        执行前拒绝以带 ``code=`` 的结构化串返回（rejected）。
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"工具 '{name}' 不存在（code=tool_not_found）"
        # C7 语义反转（design ADR-2）：
        #   1. arguments 剥离可信归属字段（模型/客户端 SHALL NOT 覆盖身份）；
        #   2. ToolExecutionContext 注入身份与 per-turn 提示键——唯一授权依据。
        strip_fields = TRUST_ARGUMENT_FIELDS
        if context is not None and context.principal_type != "user":
            # dev/owner：路由目标允许由模型指定（合法跨目标推送）。
            strip_fields = strip_fields - ROUTING_ARGUMENT_FIELDS
        safe_arguments = {
            k: v for k, v in arguments.items() if k not in strip_fields
        }
        # C7 task 7.1：审计基于模型可见参数（trust 字段已剥离）。
        audit_args["arguments"] = dict(safe_arguments)
        merged: dict[str, Any] = dict(safe_arguments)
        if context is not None:
            merged.update(context.tool_kwargs())
            if (
                context.principal_type == "user"
                and set(arguments) & ROUTING_ARGUMENT_FIELDS
            ):
                # user 试图指定任意推送目标 → 剥离并打标，工具侧结构化拒绝。
                merged["_routing_overridden"] = True
            # C7 task 4.1（ADR-4）：effect 等级执行面强制（仅 user principal；
            # owner/dev 路径不受限，与租户白名单同哲学）。
            meta = self._metadata.get(name)
            if (
                meta is not None
                and context.principal_type == "user"
                and not self._effect_allowed(meta)
            ):
                return (
                    f"错误：工具作用等级不允许在当前会话执行"
                    f"（code=tool_denied_effect，effect={meta.effect.value}）"
                )
        if self._path_resolver is not None:
            merged.setdefault("path_resolver", self._path_resolver)
        if not _tool_defines_parameter(tool, _PROGRESS_DESCRIPTION_FIELD):
            merged.pop(_PROGRESS_DESCRIPTION_FIELD, None)
        if context is not None:
            result = await self._execute_managed(tool, merged, context)
        else:
            result = await tool.execute(**merged)
        if isinstance(result, ToolResult) and context is not None:
            result.tool_call_id = context.request_id
        return result

    async def _emit_audit(
        self,
        name: str,
        context: ToolExecutionContext | None,
        audit_args: dict[str, Any],
        started_at: float,
        status: str,
        error_code: str | None,
    ) -> None:
        """C7 task 7.1（ADR-6）：写审计终态行；未接线/失败均不阻断工具调用。"""
        sink = self._audit_sink
        if sink is None or context is None:
            return
        try:
            meta = self._metadata.get(name)
            effect_class = (
                meta.effect.value
                if meta is not None and meta.effect is not None
                else "unknown"
            )
            redacted, digest = redact_arguments(audit_args.get("arguments", {}))
            event = ToolAuditEvent(
                tenant_id=context.tenant_id,
                account_id=context.account_id,
                request_id=context.request_id,
                session_id=context.session_id,
                turn_id=context.turn_id,
                # registry 的 tool_call_id 分配语义（task 4.1）：等于 request_id。
                tool_call_id=context.request_id,
                tool_name=name,
                effect_class=effect_class,
                status=status,
                arguments_redacted=redacted,
                arguments_hash=digest,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                error_code=error_code,
            )
            await sink.write(event)
        except Exception:  # noqa: BLE001 —— 审计失败不阻断工具调用
            logger.warning("tool_audit 写入失败 name=%s", name, exc_info=True)

    @staticmethod
    async def _execute_managed(
        tool: Tool,
        merged: dict[str, Any],
        context: ToolExecutionContext,
    ) -> str | ToolResult:
        """C7 task 6.1（ADR-7）：执行中工具挂入租户取消注册表。

        封禁事件经 :meth:`TenantCancellationRegistry.cancel_tenant` 直接取消任务；
        区分「工具被租户取消」（转结构化取消结果，turn 继续收束）与「外层 turn
        取消」（原样上抛，不吞取消）。

        判别依据：3.12 起 ``Task.cancel()`` 会把取消转发给被 await 的子任务，外层
        turn 取消也会让工具任务收到 CancelledError——以注册表的 handle 标记
        （:meth:`TenantCancellationRegistry._take_cancellation`）区分来源，而非
        仅凭 ``task.cancelled()``。
        """
        from agent.admission.tool_cancellation import shared_registry

        task = asyncio.create_task(tool.execute(**merged))
        handle = await shared_registry().register(context.tenant_id, task)
        try:
            return await task
        except asyncio.CancelledError:
            if task.cancelled() and await shared_registry()._take_cancellation(  # noqa: SLF001
                handle
            ):
                return (
                    "错误：工具调用已被账号状态变更取消"
                    "（code=tool_cancelled_account_status）"
                )
            raise
        finally:
            await shared_registry().unregister(handle)

    @staticmethod
    def _effect_allowed(meta: ToolMeta) -> bool:
        """effect 执行面强制（ADR-4）：process-exec/admin 一律拒绝普通租户；
        external-write 无补偿登记即拒绝；其余等级放行。"""
        if meta.effect in (ToolEffect.PROCESS_EXEC, ToolEffect.ADMIN):
            return False
        if meta.effect is ToolEffect.EXTERNAL_WRITE:
            return meta.requires_compensation
        return True

    def get_schemas_as_doc_results(self, names: list[str]) -> list[dict[str, Any]]:
        """将工具名列表转为与 search() 相同格式的结果列表。

        供 select: 精确加载路径使用，why_matched 固定为"名称:精确匹配"。
        """
        results: list[dict[str, Any]] = []
        for name in names:
            doc = self._documents.get(name)
            if doc:
                results.append(
                    {
                        "name": doc.name,
                        "summary": doc.description[:120],
                        "why_matched": ["名称:精确匹配"],
                        "risk": doc.risk,
                        "always_on": doc.always_on,
                    }
                )
        return results

    def get_mcp_server_names(self) -> set[str]:
        """返回当前已注册的所有 MCP server 名称。"""
        return {
            doc.source_name
            for doc in self._documents.values()
            if doc.source_type == "mcp"
        }

    def get_tool_names_by_source(self, source_type: str, source_name: str) -> set[str]:
        """返回指定来源的所有工具名。"""
        return {
            name
            for name, doc in self._documents.items()
            if doc.source_type == source_type and doc.source_name == source_name
        }

    def search(
        self,
        query: str,
        top_k: int = 5,
        allowed_risk: list[str] | None = None,
        excluded_names: AbstractSet[str] | None = None,
    ) -> list[dict[str, Any]]:
        """关键词搜索工具目录，返回匹配的工具信息列表。

        excluded_names: 调用方（当前 turn）传入的排除集合，通常为已可见工具名。
        meta_tools 始终被排除。搜索逻辑委托给 SearchBackend。
        """
        excluded = _META_TOOLS | (excluded_names or set())
        return cast(
            list[dict[str, Any]],
            self._backend.search(
                query=query,
                top_k=top_k,
                allowed_risk=allowed_risk,
                excluded_names=excluded,
            ),
        )
