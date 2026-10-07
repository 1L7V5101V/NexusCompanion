from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from agent.tools.base import Tool
from agent.tools.filesystem import EditFileTool, WriteFileTool
from agent.tools.forget_memory import ForgetMemoryTool
from agent.tools.memorize import MemorizeTool
from agent.tools.message_lookup import FetchMessagesTool, SearchMessagesTool
from agent.tools.message_push import MessagePushTool
from agent.tools.recall_memory import RecallMemoryTool
from agent.tools.base import ToolEffect
from agent.tools.registry import ToolRegistry
from agent.tools.shell import ShellTool, ShellTaskOutputTool, ShellTaskStopTool
from agent.tools.tool_search import ToolSearchTool
from core.memory.engine import MemoryEngine, MemoryToolSpec
from infra.storage.interfaces import SessionStorage, TenantContext


class _MemorySignalTool(Tool):
    name = "memory_signal"
    description = "由当前 memory engine 的 tool_profile 注入工具描述。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(
        self,
        memory: MemoryEngine,
        spec: MemoryToolSpec,
    ) -> None:
        if not spec.name:
            raise ValueError("自定义 memory 工具缺少 name")
        self._memory = memory
        self._spec = spec
        self.name = spec.name
        self.description = spec.description
        self.parameters = spec.parameters

    async def execute(
        self,
        **kwargs: Any,
    ) -> str:
        return "已记录。"


def register_common_meta_tools(
    tools: ToolRegistry,
    readonly_tools: dict[str, Tool],
    session_store: Any = None,
    push_tool: MessagePushTool | None = None,
    *,
    store_for: Callable[[TenantContext], SessionStorage] | None = None,
) -> MessagePushTool:
    resolve_store: Callable[[TenantContext], SessionStorage]
    if store_for is not None:
        resolve_store = store_for
    elif session_store is not None:
        resolve_store = lambda ctx: cast(SessionStorage, session_store)
    else:
        raise ValueError("register_common_meta_tools 需要 session_store 或 store_for")
    tools.register(ToolSearchTool(tools), always_on=True, risk="read-only")
    tools.register(
        ShellTool(),
        always_on=True,
        risk="external-side-effect",
        effect=ToolEffect.PROCESS_EXEC,
        search_hint="终端 脚本 bash 命令",
    )
    tools.register(
        ShellTaskOutputTool(),
        always_on=True,
        risk="read-only",
        search_hint="后台任务输出 task_output 进程日志",
    )
    tools.register(
        ShellTaskStopTool(),
        always_on=True,
        risk="external-side-effect",
        effect=ToolEffect.PROCESS_EXEC,
        search_hint="停止后台任务 task_stop 杀进程",
    )
    tools.register(
        cast(Tool, readonly_tools["web_search"]),
        always_on=True,
        risk="read-only",
        effect=ToolEffect.NETWORK,
        search_hint="谷歌 Bing 查资料",
    )
    tools.register(
        cast(Tool, readonly_tools["web_fetch"]),
        always_on=True,
        risk="read-only",
        effect=ToolEffect.NETWORK,
        search_hint="读取网址 浏览网页",
    )
    tools.register(
        cast(Tool, readonly_tools["read_file"]),
        always_on=True,
        risk="read-only",
    )
    tools.register(
        cast(Tool, readonly_tools["list_dir"]),
        always_on=True,
        risk="read-only",
        search_hint="ls 查看目录",
    )
    tools.register(
        FetchMessagesTool(resolve_store),
        always_on=True,
        risk="read-only",
        search_hint="消息回溯 按ID查对话原文 source_ref",
    )
    tools.register(
        SearchMessagesTool(resolve_store),
        always_on=True,
        risk="read-only",
        search_hint="你之前说 聊过什么 历史对话",
    )
    resolved_push_tool = push_tool or MessagePushTool()
    tools.register(
        resolved_push_tool,
        always_on=True,
        risk="external-side-effect",
        effect=ToolEffect.NETWORK,
    )
    tools.register(
        WriteFileTool(),
        always_on=True,
        risk="write",
    )
    tools.register(
        EditFileTool(),
        always_on=True,
        risk="write",
    )
    return resolved_push_tool


def _register_memory_tool(
    tools: ToolRegistry,
    tool: Tool,
    *,
    risk: str,
    search_hint: str | None = None,
    engine_id: str = "",
) -> None:
    _validate_memory_tool_name(tool.name)
    if tools.has_tool(tool.name):
        raise ValueError(f"memory 工具重复注册: {tool.name}")
    tools.register(
        tool,
        always_on=True,
        risk=risk,
        search_hint=search_hint,
        # C7（ADR-3 类别规则）：memory engine tool_profile 注入的工具以注册来源
        # 标记，租户目录按来源放行（启用该引擎的租户可见），不按静态 id 枚举。
        # C14：source_name 承载 engine_id，租户目录按 active engine 过滤。
        source_type="memory_engine",
        source_name=engine_id,
    )


class _EngineDispatchTool(Tool):
    """同名 memory 工具的租户引擎分发器（C14 ADR-5）。

    多引擎并存时同一工具名（如 ``recall_memory``）被多个引擎声明：registry 只
    注册一个分发器（schema 取 primary 引擎声明），执行时按 ``tool_kwargs()``
    注入的 ``memory_engine``（work-start 冻结的租户 active engine）分发到对应
    引擎实例；缺省/未知回退 primary。进行中 work 的 engine 已冻结，分发结果
    与检索路径一致。
    """

    name = "memory_engine_dispatch"
    description = "由租户 active memory engine 的 tool_profile 注入工具描述。"
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def __init__(
        self,
        *,
        impls: dict[str, Tool],
        primary: Tool,
    ) -> None:
        self._impls = impls
        self._primary = primary
        self.name = primary.name
        self.description = primary.description
        self.parameters = primary.parameters

    async def execute(self, **kwargs: Any) -> str:
        engine_id = str(kwargs.pop("memory_engine", "") or "")
        impl = self._impls.get(engine_id)
        if impl is None:
            impl = self._primary
        return await impl.execute(**kwargs)


def register_memory_meta_tools(
    tools: ToolRegistry,
    engine: MemoryEngine,
) -> None:
    register_memory_tools_for_engines(tools, {"": engine})


def register_memory_tools_for_engines(
    tools: ToolRegistry,
    engines: dict[str, MemoryEngine],
) -> None:
    """跨引擎合并注册 memory 工具（C14 ADR-5）。

    - 仅单引擎声明的工具 → 直注（绑定该引擎实例，``source_name=engine_id``）；
    - 多引擎同名工具（如 ``recall_memory``）→ 注册一个
      :class:`_EngineDispatchTool`（schema 取 primary=首个引擎声明；执行按租户
      active engine 分发，缺省/未知回退 primary）。
    修复双引擎并存时 ``recall_memory`` 重复注册的启动崩溃（roadmap §4 已知缺陷）。
    """
    if not engines:
        return
    ordered = list(engines.items())
    primary_id, _primary_engine = ordered[0]
    # tool name → [(engine_id, Tool, risk, search_hint), ...]（保序，primary 在前）。
    declared: dict[str, list[tuple[str, Tool, str, str | None]]] = {}
    for engine_id, engine in ordered:
        profile = engine.tool_profile()
        entries: list[tuple[Tool, str, str | None]] = []
        if profile.memorize is not None:
            entries.append((
                _build_tool(engine, profile.memorize, MemorizeTool),
                profile.memorize.risk,
                profile.memorize.search_hint or None,
            ))
        if profile.forget is not None:
            entries.append((
                _build_tool(engine, profile.forget, ForgetMemoryTool),
                profile.forget.risk,
                profile.forget.search_hint or None,
            ))
        if profile.recall is not None:
            entries.append((
                _build_tool(engine, profile.recall, RecallMemoryTool),
                profile.recall.risk,
                profile.recall.search_hint or None,
            ))
        for spec in profile.tools:
            entries.append((
                _build_tool(engine, spec, _MemorySignalTool),
                spec.risk,
                spec.search_hint or None,
            ))
        for tool, risk, hint in entries:
            declared.setdefault(tool.name, []).append((engine_id, tool, risk, hint))

    for name, impls in declared.items():
        _validate_memory_tool_name(name)
        if tools.has_tool(name):
            raise ValueError(f"memory 工具重复注册: {name}")
        if len(impls) == 1:
            engine_id, tool, risk, hint = impls[0]
            _register_memory_tool(
                tools, tool, risk=risk, search_hint=hint, engine_id=engine_id
            )
            continue
        # 多引擎同名：分发器。schema/risk/hint 取 primary（ordered[0]）的声明；
        # source_name 留空 = 对所有启用 engine 的租户可见（分发器本身多归属）。
        impl_map = {engine_id: tool for engine_id, tool, _r, _h in impls}
        primary_tool = impl_map[primary_id]
        primary_risk = next(r for eid, _t, r, _h in impls if eid == primary_id)
        primary_hint = next(
            h for eid, _t, _r, h in impls if eid == primary_id
        )
        _register_memory_tool(
            tools,
            _EngineDispatchTool(impls=impl_map, primary=primary_tool),
            risk=primary_risk,
            search_hint=primary_hint,
        )


def _build_tool(engine: MemoryEngine, spec: Any, default_cls: type) -> Tool:
    cls = spec.tool_class if spec.tool_class is not None else default_cls
    return cast(Tool, cls(engine, spec))


def _validate_memory_tool_name(name: str) -> None:
    if not name or not name[0].isalpha():
        raise ValueError(f"memory 工具名非法: {name}")
    if any(not (char.islower() or char.isdigit() or char == "_") for char in name):
        raise ValueError(f"memory 工具名非法: {name}")
