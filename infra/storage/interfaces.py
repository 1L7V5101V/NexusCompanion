"""Storage interface seam between business code and the SQLite/PostgreSQL adapters.

M4.5 决策 A：调用方只依赖这里的 Protocol，不传播 `MemoryStore2 | PostgresMemoryStore`
等具体实现联合类型。SQLite 与 PostgreSQL 是两个 adapter，共同满足本文件定义的
Protocol（方法集 = 当前业务代码通过 seam 实际调用的共享方法）。

SQLite-only 能力不入 Protocol：`MemoryStore2.insert_query_log` /
`keyword_search_bm25`、`SessionStore` 的 turn 方法（create_turn 等）由调用方
`isinstance` 收窄使用，PG adapter 不承诺实现它们。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

__all__ = ["MemoryStorage", "SessionStorage", "TenantContext", "TenantResolver"]


@dataclass(frozen=True)
class TenantContext:
    """把可信 inbound identity 解析后的稳定租户身份，贯穿 storage 调用。

    字段由真实调用需求决定（M4H-2 定案），当前只承载稳定的 tenant_id。
    """

    tenant_id: str


_IdentityT = TypeVar("_IdentityT")


@runtime_checkable
class TenantResolver(Protocol[_IdentityT]):
    """可信 identity -> TenantContext 的单向解析 seam。

    tenant 只能由服务端可信 channel/auth identity 派生；调用方不得接受客户端
    任意 tenant_id。具体 identity 类型由 M4H-2 按通道定义。
    """

    def resolve(self, identity: _IdentityT) -> TenantContext: ...


@runtime_checkable
class MemoryStorage(Protocol):
    """memory 存储的共同接口（SQLite MemoryStore2 与 PostgresMemoryStore）。

    生命周期：factory/runtime 负责创建与 close；调用方不得自行持有长期连接。
    """

    def close(self) -> None: ...

    def upsert_item(
        self,
        memory_type: str,
        summary: str,
        embedding: list[float] | None,
        source_ref: str | None = None,
        extra: dict[str, object] | None = None,
        happened_at: str | None = None,
        emotional_weight: int = 0,
    ) -> str: ...

    def upsert_consolidation_event(
        self,
        *,
        source_ref: str,
        summary: str,
        embedding: list[float] | None,
        extra: dict[str, object] | None = None,
        happened_at: str | None = None,
        emotional_weight: int = 0,
    ) -> str: ...

    def has_consolidation_source_ref(self, source_ref: str) -> bool: ...

    def mark_superseded_batch(self, ids: list[str]) -> None: ...

    def merge_item_raw(
        self,
        item_id: str,
        new_summary: str,
        new_hash: str,
        new_embedding: list[float],
        new_extra: dict[str, object] | None = None,
    ) -> None: ...

    def reinforce_items_batch(self, ids: list[str], emotional_weight: int = 0) -> None: ...

    def vector_search(
        self,
        query_vec: list[float],
        top_k: int = 8,
        memory_types: list[str] | None = None,
        score_threshold: float = 0.0,
        include_superseded: bool = False,
        scope_channel: str | None = None,
        scope_chat_id: str | None = None,
        require_scope_match: bool = False,
        hotness_alpha: float = 0.0,
        hotness_half_life_days: float = 14.0,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
    ) -> list[dict[str, object]]: ...

    def vector_search_batch(
        self,
        query_vecs: list[list[float]],
        top_k: int = 8,
        memory_types: list[str] | None = None,
        score_threshold: float = 0.0,
        include_superseded: bool = False,
        scope_channel: str | None = None,
        scope_chat_id: str | None = None,
        require_scope_match: bool = False,
        hotness_alpha: float = 0.0,
        hotness_half_life_days: float = 14.0,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
    ) -> list[list[dict[str, object]]]: ...

    def keyword_search_summary(
        self,
        terms: list[str],
        memory_types: list[str] | None = None,
        limit: int = 20,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        scope_channel: str | None = None,
        scope_chat_id: str | None = None,
        require_scope_match: bool = False,
    ) -> list[dict[str, object]]: ...

    def keyword_match_procedures(self, action_tokens: list[str]) -> list[dict[str, object]]: ...

    def get_item_for_dashboard(
        self, item_id: str, *, include_embedding: bool = False
    ) -> dict[str, object] | None: ...

    def get_items_by_ids(self, ids: list[str]) -> list[dict[str, object]]: ...

    def list_items_for_dashboard(
        self,
        *,
        q: str = "",
        memory_type: str = "",
        status: str = "",
        source_ref: str = "",
        scope_channel: str = "",
        scope_chat_id: str = "",
        has_embedding: bool | None = None,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[dict[str, object]], int]: ...

    def list_events_by_time_range(
        self, time_start: datetime, time_end: datetime, limit: int = 200
    ) -> list[dict[str, object]]: ...

    def find_similar_recent_events(
        self,
        embedding: list[float],
        *,
        days_back: int = 7,
        threshold: float = 0.92,
        top_k: int = 3,
    ) -> list[str]: ...

    def find_similar_items_for_dashboard(
        self,
        item_id: str,
        *,
        top_k: int = 8,
        memory_type: str = "",
        score_threshold: float = 0.0,
        include_superseded: bool = False,
    ) -> list[dict[str, object]]: ...

    def delete_item(self, item_id: str) -> bool: ...

    def delete_items_batch(self, ids: list[str]) -> int: ...

    def update_item_for_dashboard(
        self,
        item_id: str,
        *,
        status: str | None = None,
        extra_json: dict[str, object] | None = None,
        source_ref: str | None = None,
        happened_at: str | None = None,
        emotional_weight: int | None = None,
    ) -> dict[str, object] | None: ...

    def undo_by_message_sources(
        self, message_ids: list[str], *, dry_run: bool = False
    ) -> dict[str, object]: ...


@runtime_checkable
class SessionStorage(Protocol):
    """session 存储的共同接口（SQLite SessionStore 与 PostgresSessionStore）。

    控制面 turn 持久化（create_turn/read_turn/list_turns/transition_turn/
    delete_thread_turns）是 SQLite-only 能力，由 `SessionManager.control_store`
    isinstance 收窄访问，不入本 Protocol。
    """

    def close(self) -> None: ...

    def upsert_session(
        self,
        key: str,
        *,
        created_at: str,
        updated_at: str,
        last_consolidated: int,
        metadata: dict[str, Any],
    ) -> None: ...

    def session_exists(self, key: str) -> bool: ...

    def get_session_meta(self, key: str) -> dict[str, Any] | None: ...

    def list_sessions(self) -> list[dict[str, Any]]: ...

    def list_sessions_for_dashboard(
        self,
        *,
        q: str = "",
        channel: str = "",
        updated_from: str = "",
        updated_to: str = "",
        has_proactive: bool | None = None,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
    ) -> tuple[list[dict[str, Any]], int]: ...

    def update_session(
        self,
        key: str,
        *,
        metadata: dict[str, Any] | None = None,
        last_consolidated: int | None = None,
        last_user_at: str | None = None,
        last_proactive_at: str | None = None,
    ) -> dict[str, Any] | None: ...

    def next_seq(self, session_key: str) -> int: ...

    def insert_message(
        self,
        session_key: str,
        *,
        role: str,
        content: str,
        ts: str,
        seq: int,
        tool_chain: Any | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def fetch_session_messages(self, session_key: str) -> list[dict[str, Any]]: ...

    def get_message(self, message_id: str) -> dict[str, Any] | None: ...

    def update_message(
        self,
        message_id: str,
        *,
        role: str | None = None,
        content: str | None = None,
        tool_chain: Any | None = None,
        extra: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> dict[str, Any] | None: ...

    def delete_message(self, message_id: str) -> bool: ...

    def delete_messages_batch(self, ids: list[str]) -> int: ...

    def count_messages(self, session_key: str) -> int: ...

    def list_messages_for_dashboard(
        self,
        *,
        session_key: str | None = None,
        q: str = "",
        role: str = "",
        page: int = 1,
        page_size: int = 25,
        sort_by: str = "ts",
        sort_order: str = "desc",
    ) -> tuple[list[dict[str, Any]], int]: ...

    def delete_session(self, key: str, *, cascade: bool = False) -> bool: ...

    def delete_sessions_batch(self, keys: list[str], *, cascade: bool = False) -> int: ...

    def get_channel_metadata(self, channel: str) -> list[dict[str, Any]]: ...

    def update_presence(
        self,
        key: str,
        *,
        last_user_at: str | None = None,
        last_proactive_at: str | None = None,
    ) -> None: ...

    def get_presence(self, key: str) -> dict[str, str | None] | None: ...

    def list_presence(self) -> dict[str, dict[str, str | None]]: ...

    def most_recent_user_at(self) -> str | None: ...
