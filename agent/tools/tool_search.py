import json
from typing import TYPE_CHECKING, Any

from agent.tools.base import Tool

if TYPE_CHECKING:
    from agent.tools.registry import ToolRegistry


class ToolSearchTool(Tool):
    """在工具目录中搜索可用工具，帮助模型发现并解锁需要的工具。

    调用此工具后，匹配到的工具将在本轮对话中解锁，可直接调用。
    """

    def __init__(self, registry: "ToolRegistry") -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "tool_search"

    @property
    def description(self) -> str:
        return (
            "在工具目录中搜索可用工具。搜索结果中的工具将立即解锁，之后可直接调用。\n\n"
            "调用时机：\n"
            "- 需要某类功能，但不知道工具名称 → 必须调用\n"
            "- 知道工具名且已可见 → 直接调用，不要先搜索\n"
            "- 知道工具名但不可见 → 可直接调用（系统会自动解锁），或先搜索确认\n"
            "- 收到'工具不存在'错误 → 必须调用，用错误中的建议关键词搜索\n"
            "- 纯对话/推理，不涉及工具能力 → 不调用\n\n"
            "正确流程：tool_search(query) → 从结果中选择工具 → 立即调用（不需二次搜索）\n"
            "查询示例：'发送消息给用户'、'定时提醒'、'RSS订阅管理'、'Fitbit健康数据'"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，描述你需要的功能，例如：'定时任务'、'文件读取'、'订阅管理'",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回的最大工具数量，默认 5，最大 10",
                    "default": 5,
                },
                "allowed_risk": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["read-only", "write", "external-side-effect"],
                    },
                    "description": "允许的风险等级，不填则不过滤。read-only=只读，write=写操作，external-side-effect=外部副作用",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        top_k: int = 5,
        allowed_risk: list[str] | None = None,
        **_: Any,
    ) -> str:
        top_k = min(max(1, int(top_k)), 10)
        results = self._registry.search(
            query=query, top_k=top_k, allowed_risk=allowed_risk
        )
        if not results:
            return json.dumps(
                {"matched": [], "tip": "没有找到匹配工具，请换个关键词重试"},
                ensure_ascii=False,
            )
        return json.dumps({"matched": results}, ensure_ascii=False, indent=2)
