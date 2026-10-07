"""C7 三层工具目录的租户可见层（design ADR-3 / §5.9.7 第 4 条）。

白名单按「静态精确 tool id + 租户 active engine 的 tool_profile 注入工具」两部分
组成（ADR-3 类别规则）：静态清单在这里冻结；engine 注入工具随引擎注册自然进入
registry，由本模块按记忆工具同类放行（不按静态 id 枚举）。

可见性三层防线中的第一道（schema 不可见）——执行前仍由 pre-tool hook 重新校验
（task 3.2），schema 可见性 SHALL NOT 被当作唯一防线（§5.8.2）。

引擎绑定过滤：C14 落地——多引擎并存时 registry 含全部已构建引擎的工具，
本模块按租户 active engine（``context.memory_engine``，work-start 冻结）过滤
``source_name`` 标注了引擎的工具；同名分发器（source_name 为空）恒可见。
"""

from __future__ import annotations

from agent.tools.context import ToolExecutionContext
from agent.tools.registry import ToolRegistry

#: ADR-3 允许清单（普通 tenant 可见；task 1.1 程序化清单对账收敛后的冻结版）。
TENANT_ALLOWED_TOOL_IDS: frozenset[str] = frozenset(
    {
        # 记忆（租户内）
        "recall_memory",
        "memorize",
        "forget_memory",
        # 本账号消息查询
        "search_messages",
        "fetch_messages",
        # 租户文件（task 5 接 TenantPathResolver）
        "read_file",
        "list_dir",
        "write_file",
        "edit_file",
        "read_image_vision",
        # 服务端绑定目标的推送
        "message_push",
        # 租户自有 schedule/reminder
        "schedule",
        "remind",
        "list_schedules",
        "cancel_schedule",
        # 网络读取（限流 + SSRF 防护，task 3.3）
        "web_search",
        "web_fetch",
        # 只读元工具：搜索范围 = 当前租户可见目录
        "tool_search",
    }
)

#: ADR-3 关闭清单（普通 tenant 不可见、不可执行）。
TENANT_CLOSED_TOOL_IDS: frozenset[str] = frozenset(
    {
        "shell",
        "spawn",
        "spawn_manage",
        "task_output",
        "task_stop",
        "load_skill",
        "mcp_add",
        "mcp_remove",
        "mcp_list",
    }
)


#: memory engine tool_profile 注入工具的注册来源标记（ADR-3 类别规则）。
ENGINE_TOOL_SOURCE = "memory_engine"


def tenant_visible_names(
    registry: ToolRegistry,
    context: ToolExecutionContext | None,
) -> set[str] | None:
    """按执行上下文解析租户可见工具名集合。

    - ``context is None`` 或 principal 非 ``user``（dev/owner 路径）→ 返回
      ``None`` 表示不过滤（现状全量视图，保持 C4/C5 既有行为）；
    - principal 为 ``user`` → 返回 白名单 ∩ registry 已注册 − 关闭清单 ∪
      **engine 注入工具**（``source_type="memory_engine"``，按注册来源归类）。
      C14：多引擎并存时 engine 工具按租户 active engine 过滤——
      ``source_name`` 为空（同名分发器/未标引擎）恒可见，``source_name`` 非空
      时仅当等于 ``context.memory_engine``（work-start 冻结的租户引擎）可见。
    """
    if context is None or context.principal_type != "user":
        return None
    registered = registry.get_registered_names()
    active_engine = context.memory_engine
    engine_tools = {
        doc.name
        for doc in registry.get_documents()
        if doc.source_type == ENGINE_TOOL_SOURCE
        and (not doc.source_name or not active_engine or doc.source_name == active_engine)
    }
    return ((registered & TENANT_ALLOWED_TOOL_IDS) - TENANT_CLOSED_TOOL_IDS) | (
        engine_tools & registered
    )
