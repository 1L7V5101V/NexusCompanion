import json
from typing import TYPE_CHECKING, Any

from agent.tools.base import Tool

if TYPE_CHECKING:
    from agent.tools.registry import ToolRegistry


class ListToolsTool(Tool):
    """列出当前所有已注册的可用工具及其简要说明。"""

    def __init__(self, registry: "ToolRegistry") -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "list_tools"

    @property
    def description(self) -> str:
        return (
            "列出系统中所有已注册工具的完整目录（全量，含当前不可见的工具），支持按 tag 过滤。\n\n"
            "适用场景：宏观了解系统能力全貌、按分类（tag）浏览某领域工具。\n"
            "不适用场景：按具体功能查找最合适的工具——请使用 tool_search(query=...) 而非 list_tools。\n"
            "注意：list_tools 只做目录展示，不解锁工具；调用工具前仍需确认其在可见列表中或直接尝试调用。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "按标签过滤，例如 'filesystem'、'scheduling'、'mcp'。不填则返回全部。",
                },
            },
            "required": [],
        }

    async def execute(self, tag: str = "", **_: Any) -> str:
        tag = tag.strip().lower()
        tools = []
        for name, tool in self._registry._tools.items():
            if name in ("tool_search", "list_tools"):
                continue
            meta = self._registry._metadata.get(name)
            if tag and meta and tag not in meta.tags:
                continue
            tools.append(
                {
                    "name": name,
                    "summary": tool.description[:80],
                    "tags": meta.tags if meta else [],
                    "risk": meta.risk if meta else "unknown",
                }
            )

        return json.dumps(
            {"total": len(tools), "tools": tools},
            ensure_ascii=False,
            indent=2,
        )
