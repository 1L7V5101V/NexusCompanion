"""
memorize 工具：用户主动写记忆
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from agent.tools.base import Tool

if TYPE_CHECKING:
    from memory2.memorizer import Memorizer

logger = logging.getLogger(__name__)


def _append_to_sop_file(persist_file: str, summary: str, steps: list[str] | None) -> None:
    """将规则追加写入 workspace/sop/ 下的对应文件"""
    workspace = Path.home() / ".akasic" / "workspace"
    sop_dir = workspace / "sop"
    sop_dir.mkdir(parents=True, exist_ok=True)
    target = sop_dir / persist_file
    lines = [f"\n## {summary}\n"]
    if steps:
        for s in steps:
            lines.append(f"- {s}")
    content = "\n".join(lines) + "\n"
    with open(target, "a", encoding="utf-8") as f:
        f.write(content)
    logger.info(f"memorize: appended to {target}")


class MemorizeTool(Tool):
    name = "memorize"
    description = (
        "将重要规则/流程/偏好永久写入记忆。"
        "用户说「记住/以后/下次」等时必须调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "一句话描述要记住的内容",
            },
            "memory_type": {
                "type": "string",
                "enum": ["procedure", "preference", "event", "profile"],
                "description": "记忆类型",
            },
            "tool_requirement": {
                "type": "string",
                "description": "该规则要求必须调用的工具名（可选）",
            },
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "执行步骤（可选）",
            },
            "persist_file": {
                "type": "string",
                "description": "同步写入的 SOP 文件名（可选，如 user-preferences.md）",
            },
        },
        "required": ["summary", "memory_type"],
    }

    routing_hint = "用户要求记住某条规则、偏好或流程时调用"

    def __init__(self, memorizer: "Memorizer") -> None:
        self._memorizer = memorizer

    async def execute(
        self,
        summary: str,
        memory_type: str,
        tool_requirement: str | None = None,
        steps: list[str] | None = None,
        persist_file: str | None = None,
        **_: Any,
    ) -> str:
        extra = {
            "tool_requirement": tool_requirement,
            "steps": steps or [],
            "persist_file": persist_file,
        }
        result = await self._memorizer.save_item(
            summary=summary,
            memory_type=memory_type,
            extra=extra,
            source_ref="memorize_tool",
        )
        if persist_file:
            try:
                _append_to_sop_file(persist_file, summary, steps)
            except Exception as e:
                logger.warning(f"memorize: 写入 SOP 文件失败: {e}")
        return f"已记住（{result}）：{summary}"
