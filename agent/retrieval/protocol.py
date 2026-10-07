from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from agent.core.types import HistoryMessage, RetrievalTrace
from infra.storage.interfaces import TenantContext


@dataclass
class RetrievalRequest:
    message: str
    tenant: TenantContext
    session_key: str
    channel: str
    chat_id: str
    history: list[HistoryMessage]  # 完整会话历史，无截窗。pipeline 实现负责自行决定使用范围。
    # DefaultMemoryRetrievalPipeline 内部截取末尾 MemoryConfig.window 条后使用。
    session_metadata: dict[str, object]
    timestamp: datetime | None = None
    extra: dict[str, object] = field(default_factory=dict[str, object])
    # C14：本 work 冻结的 active memory engine（work-start 从 PG binding 解析）。
    # 非空时管线直查该引擎（租户单 active engine）；空串回退既有选择行为。
    engine_binding: str = ""


@dataclass
class RetrievalResult:
    block: str
    trace: RetrievalTrace | None = None
    verified: bool = False
    metadata: dict[str, object] = field(default_factory=dict[str, object])


@runtime_checkable
class MemoryRetrievalPipeline(Protocol):
    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult: ...
