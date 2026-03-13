"""异步 TraceWriter：把 TurnTrace / RagTrace 写入 SQLite。

非阻塞：调用方用 emit() put_nowait，后台 task 消费队列写 DB。
Queue 满时 drop + 计数，不崩溃主循环。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from core.observe.db import open_db
from core.observe.events import RagItemTrace, RagTrace, TurnTrace

logger = logging.getLogger("observe.writer")

_QUEUE_MAX = 500
_ARG_MAX = 300
_RESULT_MAX = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_tool_calls(tool_calls: list[dict]) -> str | None:
    if not tool_calls:
        return None
    slim = [
        {
            "name": c.get("name", ""),
            "args": str(c.get("args", c.get("arguments", "")))[:_ARG_MAX],
            "result": str(c.get("result", ""))[:_RESULT_MAX],
        }
        for c in tool_calls
    ]
    return json.dumps(slim, ensure_ascii=False)


class TraceWriter:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._queue: asyncio.Queue[TurnTrace | RagTrace] = asyncio.Queue(
            maxsize=_QUEUE_MAX
        )
        self._dropped = 0

    # ── 公共接口 ─────────────────────────────────

    def emit(self, event: TurnTrace | RagTrace) -> None:
        """非阻塞 emit。Queue 满时 drop 并记录计数。"""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.warning("observe queue full, total_dropped=%d", self._dropped)

    async def run(self) -> None:
        """后台循环，持续消费队列写 DB。作为 asyncio task 运行。"""
        conn = open_db(self._db_path)
        logger.info("observe writer started: %s", self._db_path)
        try:
            while True:
                event = await self._queue.get()
                try:
                    self._write_one(conn, event)
                except Exception:
                    logger.exception("observe write failed for %s", type(event).__name__)
        finally:
            # flush remaining on shutdown
            while not self._queue.empty():
                try:
                    e = self._queue.get_nowait()
                    self._write_one(conn, e)
                except Exception:
                    pass
            conn.close()
            logger.info("observe writer stopped")

    # ── 内部写入 ─────────────────────────────────

    def _write_one(self, conn, event: TurnTrace | RagTrace) -> None:
        ts = _now_iso()
        if isinstance(event, TurnTrace):
            _write_turn(conn, event, ts)
        elif isinstance(event, RagTrace):
            _write_rag(conn, event, ts)


# ── DB 写入函数 ───────────────────────────────────────────────────────────────


def _write_turn(conn, e: TurnTrace, ts: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO turns (ts, source, session_key, user_msg, llm_output, tool_calls, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                e.source,
                e.session_key,
                e.user_msg,
                e.llm_output,
                _serialize_tool_calls(e.tool_calls),
                e.error,
            ),
        )


def _write_rag(conn, e: RagTrace, ts: str) -> None:
    with conn:
        cur = conn.execute(
            """
            INSERT INTO rag_events (
                ts, source, session_key,
                original_query, query,
                route_decision, route_latency_ms,
                hyde_hypothesis,
                history_scope_mode, history_gate_reason,
                injected_block, preference_block, preference_query,
                fallback_reason, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                e.source,
                e.session_key,
                e.original_query,
                e.query,
                e.route_decision,
                e.route_latency_ms,
                e.hyde_hypothesis,
                e.history_scope_mode,
                e.history_gate_reason,
                e.injected_block or None,
                e.preference_block or None,
                e.preference_query or None,
                e.fallback_reason or None,
                e.error,
            ),
        )
        rag_event_id = cur.lastrowid
        if e.items:
            conn.executemany(
                """
                INSERT INTO rag_items (
                    rag_event_id, item_id, memory_type, score, summary,
                    happened_at, extra_json, retrieval_path, injected
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        rag_event_id,
                        item.item_id,
                        item.memory_type,
                        item.score,
                        item.summary,
                        item.happened_at,
                        item.extra_json,
                        item.retrieval_path,
                        1 if item.injected else 0,
                    )
                    for item in e.items
                ],
            )
