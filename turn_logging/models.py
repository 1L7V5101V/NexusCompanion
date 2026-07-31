from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnLogData:
    """统一的三条链路日志数据结构。"""

    # 基础标识
    session_key: str
    turn_type: str  # 'passive' | 'proactive' | 'drift'

    # 频道上下文
    channel: str | None = None
    chat_id: str | None = None

    # 时间
    timestamp: str = ""  # ISO 8601
    turn_duration_ms: int = 0

    # Prompt 相关
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools_schema: list[dict[str, Any]] = field(default_factory=list)

    # LLM 响应
    llm_model: str = ""
    llm_response: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    # Token 统计
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0

    # 链路上下文
    skill_names: list[str] = field(default_factory=list)
    retry_attempts: list[dict[str, Any]] = field(default_factory=list)

    # 错误
    error: str | None = None

    # 扩展
    metadata: dict[str, Any] = field(default_factory=dict)
