from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from logging.models import TurnLogData

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS turn_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key     TEXT NOT NULL,
    channel         TEXT,
    chat_id         TEXT,
    turn_type       TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    skill_names     TEXT,
    retry_attempts  TEXT,
    messages        TEXT NOT NULL,
    tools_schema    TEXT,
    llm_model       TEXT,
    llm_response    TEXT,
    tool_calls      TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cache_hit_tokens INTEGER,
    turn_duration_ms INTEGER,
    error           TEXT,
    metadata        TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
)
"""

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_turn_logs_turn_type ON turn_logs(turn_type)",
    "CREATE INDEX IF NOT EXISTS idx_turn_logs_timestamp ON turn_logs(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_turn_logs_session ON turn_logs(session_key)",
]

_INSERT_SQL = """
INSERT INTO turn_logs (
    session_key, channel, chat_id, turn_type, timestamp,
    skill_names, retry_attempts, messages, tools_schema,
    llm_model, llm_response, tool_calls,
    input_tokens, output_tokens, cache_hit_tokens,
    turn_duration_ms, error, metadata
) VALUES (
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?,
    ?, ?, ?,
    ?, ?, ?
)
"""


@dataclass
class TurnLoggerConfig:
    db_path: Path
    flush_interval_s: float = 1.0
    max_batch_size: int = 50


class TurnLogger:
    """异步日志写入器。

    - 每条 turn 完成后调用 ``log()`` 入队。
    - 后台协程批量 flush 到 SQLite，避免阻塞主流程。
    - 调用 ``close()`` 确保所有待写入完成。
    """

    def __init__(self, config: TurnLoggerConfig) -> None:
        self._config = config
        self._queue: asyncio.Queue[TurnLogData] = asyncio.Queue()
        self._closed = False
        self._flush_task: asyncio.Task[None] | None = None

        # 初始化数据库连接（同步操作在建表时执行一次）
        self._db_path = config.db_path
        self._init_db()

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute(_CREATE_TABLE_SQL)
            for sql in _CREATE_INDEXES_SQL:
                conn.execute(sql)
            conn.commit()
        finally:
            conn.close()
        logger.info("日志库已初始化: %s", self._db_path)

    def start(self) -> None:
        """启动后台 flush 协程。"""
        if self._flush_task is not None:
            return
        self._flush_task = asyncio.create_task(
            self._flush_loop(),
            name="turn_logger_flush",
        )

    async def log(self, data: TurnLogData) -> None:
        """入队一条日志（不会阻塞）。"""
        if self._closed:
            logger.warning("TurnLogger 已关闭，丢弃日志: session=%s", data.session_key)
            return
        self._queue.put_nowait(data)

    async def _flush_loop(self) -> None:
        config = self._config
        while not self._closed:
            try:
                await asyncio.wait_for(
                    self._batch_flush(),
                    timeout=config.flush_interval_s,
                )
            except asyncio.TimeoutError:
                # 到间隔时间，没有足够数据也尝试 flush
                await self._batch_flush(force=False)
            except Exception:
                logger.exception("TurnLogger flush 异常")

    async def _batch_flush(self, force: bool = True) -> None:
        batch: list[TurnLogData] = []
        # 先拉一条（阻塞确保 flush 不会空转）
        if not self._queue.empty() or force:
            try:
                first = self._queue.get_nowait()
                batch.append(first)
            except asyncio.QueueEmpty:
                return

        # 再尽量多拉
        max_size = self._config.max_batch_size
        while len(batch) < max_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not batch:
            return

        self._write_batch(batch)

    def _write_batch(self, batch: list[TurnLogData]) -> None:
        conn = sqlite3.connect(str(self._db_path))
        try:
            rows = [_turn_log_row(data) for data in batch]
            conn.executemany(_INSERT_SQL, rows)
            conn.commit()
        except Exception:
            logger.exception("日志批量写入失败: %d 条", len(batch))
            raise
        finally:
            conn.close()

    async def close(self) -> None:
        """关闭日志器，确保所有待写入完成。"""
        self._closed = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        # 最后再 flush 一次
        remaining: list[TurnLogData] = []
        while not self._queue.empty():
            try:
                remaining.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if remaining:
            self._write_batch(remaining)


def _turn_log_row(data: TurnLogData) -> tuple:
    return (
        data.session_key,
        data.channel,
        data.chat_id,
        data.turn_type,
        data.timestamp,
        _json_or_none(data.skill_names),
        _json_or_none(data.retry_attempts),
        _json_or_none(data.messages),
        _json_or_none(data.tools_schema),
        data.llm_model or None,
        data.llm_response,
        _json_or_none(data.tool_calls),
        data.input_tokens or None,
        data.output_tokens or None,
        data.cache_hit_tokens or None,
        data.turn_duration_ms or None,
        data.error,
        _json_or_none(data.metadata) if data.metadata else None,
    )


class RoutingTurnLogger:
    """按 turn_type 路由到对应 SQLite 库的三合一日志器。

    使用方式:
        logger = RoutingTurnLogger(
            passive=TurnLoggerConfig(db_path=...),
            proactive=TurnLoggerConfig(db_path=...),
            drift=TurnLoggerConfig(db_path=...),
        )
        logger.start()
        await logger.log(turn_data)
        await logger.close()
    """

    def __init__(
        self,
        passive: TurnLoggerConfig,
        proactive: TurnLoggerConfig,
        drift: TurnLoggerConfig,
    ) -> None:
        self._loggers: dict[str, TurnLogger] = {
            "passive": TurnLogger(passive),
            "proactive": TurnLogger(proactive),
            "drift": TurnLogger(drift),
        }

    def start(self) -> None:
        for inst in self._loggers.values():
            inst.start()

    async def log(self, data: TurnLogData) -> None:
        target = self._loggers.get(data.turn_type)
        if target is None:
            logger.warning("未知 turn_type=%s，丢弃日志", data.turn_type)
            return
        await target.log(data)

    async def close(self) -> None:
        for inst in self._loggers.values():
            await inst.close()


def _json_or_none(obj: object) -> str | None:
    if not obj:
        return None
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(obj)
