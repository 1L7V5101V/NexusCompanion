"""C15 work queue 运行期接线：首次在生产构造 control plane async engine。

背景（design ADR-7）：C2 的 durable control plane 建在一条**独立的 async 栈**
（asyncpg + `async_sessionmaker`，`bootstrap/db/*`）上，而生产此前只构造
`infra/storage/*` 的 sync 栈。因此 `bootstrap/db/engine.py::create_session_factory`
的唯一调用方是 `scripts/import_to_pg.py` —— C2 的 7 张表在跑起来的进程里零写零读，
`OutboundDeliveryWorker` 从不启动。本模块补上这个缺失的构造点。

边界：

- 只接线 **work item 消费者**；`OutboundDeliveryWorker` 的接线单列后续 change
  （复用本模块同一套 engine 构造方式）。
- `[agent.work_queue].enabled` 默认 **false** ⇒ 默认返回 `None`，不建连接、不启 task。
- `storage.backend != "postgres"` 时跳过（SQLite 没有 control plane 表）。
- **未注册任何 handler 时拒绝启动**（fail-fast）：否则所有 work item 都会以
  「未注册 flow」计入失败并最终进死信——那比不启动更糟。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agent.config_models import Config
from bootstrap.db.config import DatabaseConfig
from bootstrap.db.engine import create_engine, create_session_factory
from bootstrap.db.repository.control_plane_repo import WorkItemRepository
from bootstrap.work_queue_worker import (
    WorkHandler,
    WorkQueueWorker,
    WorkQueueWorkerConfig,
)

logger = logging.getLogger(__name__)

__all__ = [
    "WorkQueueRuntime",
    "async_pg_url",
    "build_work_queue_runtime",
]

_SYNC_PG_PREFIXES = (
    "postgresql+psycopg://",
    "postgresql+psycopg2://",
    "postgresql://",
)


def async_pg_url(sync_url: str) -> str:
    """把配置里的 sync 连接串转成 async 驱动串（`+asyncpg`）。

    control plane 仓储用 `async_sessionmaker`（asyncpg），而 `[storage].postgres_url`
    是给 sync 栈（`run_db` + bounded executor，psycopg）用的——两者不能混用。
    """
    url = sync_url.strip()
    for prefix in _SYNC_PG_PREFIXES:
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


@dataclass
class WorkQueueRuntime:
    """control plane async engine + work item 消费者。"""

    engine: AsyncEngine
    session_factory: async_sessionmaker
    worker: WorkQueueWorker

    async def aclose(self) -> None:
        """释放连接池（停机 cleanup 步骤调用）。"""
        await self.engine.dispose()


def build_work_queue_runtime(
    config: Config,
    *,
    handlers: Mapping[str, WorkHandler] | None = None,
    worker_id: str | None = None,
) -> WorkQueueRuntime | None:
    """按 `[agent.work_queue]` 构造运行期；未启用或非 PostgreSQL 后端返回 `None`。

    `handlers` 是 `flow → WorkHandler` 映射（ADR-5）。各 feature change 注册自己的
    handler；本 change 只提供接缝与装配。
    """
    wq = config.work_queue
    if not wq.enabled:
        return None
    if config.storage.backend != "postgres":
        logger.warning(
            "work_queue.enabled=true 但 storage.backend=%r（非 postgres）：跳过启动",
            config.storage.backend,
        )
        return None
    if not handlers:
        raise RuntimeError(
            "work_queue.enabled=true 但未注册任何 flow handler：拒绝启动"
            "（否则所有 work item 会被判为「未注册 flow」并最终进死信）"
        )

    db_cfg = DatabaseConfig(
        url=async_pg_url(config.storage.postgres_url),
        pool_size=config.storage.pool_size,
    )
    engine = create_engine(db_cfg)
    session_factory = create_session_factory(engine)
    worker = WorkQueueWorker(
        WorkItemRepository(session_factory),
        dict(handlers),
        config=WorkQueueWorkerConfig(
            lease_ttl_seconds=wq.lease_ttl_seconds,
            heartbeat_interval_seconds=wq.heartbeat_interval_seconds,
            max_attempts=wq.max_attempts,
            poll_interval_seconds=wq.poll_interval_seconds,
            batch_size=wq.batch_size,
            maintenance_acquire_timeout_seconds=wq.maintenance_acquire_timeout_seconds,
            release_delay_seconds=wq.release_delay_seconds,
            error_backoff_seconds=wq.error_backoff_seconds,
            max_error_backoff_seconds=wq.max_error_backoff_seconds,
        ),
        worker_id=worker_id,
    )
    logger.info(
        "work queue 消费者已装配：lease=%.0fs heartbeat=%.0fs max_attempts=%s batch=%s",
        wq.lease_ttl_seconds,
        wq.heartbeat_interval_seconds,
        wq.max_attempts,
        wq.batch_size,
    )
    return WorkQueueRuntime(
        engine=engine, session_factory=session_factory, worker=worker
    )
