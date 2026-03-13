from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class RagItemTrace:
    """一个从向量库检索到的 item，保留原始字段。"""

    item_id: str
    memory_type: str
    score: float
    summary: str
    happened_at: str | None
    extra_json: str | None          # 原始 extra_json 序列化为 JSON string
    retrieval_path: str             # procedure | history_raw | history_hyde | preference
    injected: bool                  # 是否最终注入到 context


@dataclass
class RagTrace:
    """一次完整的 memory 检索事件。agent 和 proactive 共用同一结构。"""

    source: Literal["agent", "proactive"]
    session_key: str
    original_query: str             # 改写前的原始 query（agent: user_msg）
    query: str                      # 实际用于检索的 query（route decision 改写后）
    route_decision: str | None      # 'RETRIEVE' | 'NO_RETRIEVE'（仅 agent）
    route_latency_ms: int | None
    hyde_hypothesis: str | None     # HyDE 生成的假设文本，None = 未使用
    history_scope_mode: str | None
    history_gate_reason: str | None
    items: list[RagItemTrace] = field(default_factory=list)
    injected_block: str = ""
    preference_block: str = ""
    preference_query: str | None = None
    fallback_reason: str = ""
    error: str | None = None


@dataclass
class TurnTrace:
    """一轮 agent 对话或 proactive tick 的完整记录。"""

    source: Literal["agent", "proactive"]
    session_key: str
    user_msg: str | None            # agent: 用户原文; proactive: None
    llm_output: str                 # LLM 最终输出完整文本
    tool_calls: list[dict] = field(default_factory=list)
    # 每个 tool call: {name, args, result}（args/result 会截断）
    error: str | None = None
