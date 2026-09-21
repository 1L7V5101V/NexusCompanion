"""C15 work queue 运行期接线验收（无需 PG：`create_async_engine` 是惰性的）。

对应 tasks 4.1–4.3 与 design ADR-7：control plane async engine 的**首次生产构造点**、
SQLite 分支、未注册 handler 的 fail-fast、以及 `[agent.work_queue]` 的解析与校验。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.config import _load_work_queue_config
from agent.config_models import Config, StorageConfig, WorkQueueConfig
from bootstrap.work_queue import async_pg_url, build_work_queue_runtime


def _config(*, enabled: bool, backend: str = "postgres") -> Config:
    return Config(
        provider="openai",
        model="m",
        api_key="k",
        system_prompt="s",
        storage=StorageConfig(backend=backend),
        work_queue=WorkQueueConfig(enabled=enabled),
    )


class _Handler:
    """满足 `WorkHandler` 协议的最小实现（接线测试不执行 handler 逻辑）。"""

    async def execute(self, envelope: Any) -> object:
        return None

    async def persist(self, session: Any, envelope: Any, result: object) -> None:
        return None


# ── 连接串驱动转换 ──


@pytest.mark.parametrize(
    ("sync_url", "expected"),
    [
        (
            "postgresql+psycopg://nexus:pw@localhost:5433/nexus",
            "postgresql+asyncpg://nexus:pw@localhost:5433/nexus",
        ),
        ("postgresql://nexus:pw@db:5432/nexus", "postgresql+asyncpg://nexus:pw@db:5432/nexus"),
        ("postgresql+psycopg2://nexus:pw@db/nexus", "postgresql+asyncpg://nexus:pw@db/nexus"),
        # 已是 async 驱动则保持不变（幂等）
        ("postgresql+asyncpg://nexus:pw@db/nexus", "postgresql+asyncpg://nexus:pw@db/nexus"),
    ],
)
def test_async_pg_url_converts_driver(sync_url: str, expected: str) -> None:
    assert async_pg_url(sync_url) == expected


# ── 分支：默认关闭 / 非 postgres ──


def test_disabled_returns_none_without_touching_db() -> None:
    assert build_work_queue_runtime(_config(enabled=False)) is None


def test_non_postgres_backend_skips(caplog: pytest.LogCaptureFixture) -> None:
    """SQLite 没有 control plane 表：启用也不启动（且不抛错）。"""
    assert build_work_queue_runtime(_config(enabled=True, backend="sqlite")) is None
    assert any("非 postgres" in r.message for r in caplog.records)


def test_enabled_without_handlers_fails_fast() -> None:
    """未注册 handler 时拒绝启动——否则所有 work item 会被判失败进死信。"""
    with pytest.raises(RuntimeError, match="未注册任何 flow handler"):
        build_work_queue_runtime(_config(enabled=True))


# ── 装配：engine + 仓储 + worker ──


async def test_enabled_with_handlers_builds_async_engine_and_worker() -> None:
    cfg = _config(enabled=True)
    cfg.work_queue = WorkQueueConfig(
        enabled=True,
        lease_ttl_seconds=90.0,
        heartbeat_interval_seconds=30.0,
        max_attempts=3,
        batch_size=7,
    )
    runtime = build_work_queue_runtime(
        cfg, handlers={"consolidation": _Handler()}, worker_id="w-test"
    )
    assert runtime is not None
    try:
        # 首次在生产构造 control plane 的 async engine（此前只有 import_to_pg.py 构造）
        assert runtime.engine.url.drivername == "postgresql+asyncpg"
        # 配置映射到 worker
        assert runtime.worker.owner == "w-test"
        assert runtime.worker.config.lease_ttl_seconds == 90.0
        assert runtime.worker.config.heartbeat_interval_seconds == 30.0
        assert runtime.worker.config.max_attempts == 3
        assert runtime.worker.config.batch_size == 7
        # 仓储接的是同一个 session factory
        assert runtime.session_factory is not None
    finally:
        await runtime.aclose()


# ── 配置解析与校验（tasks 4.3） ──


def test_load_work_queue_defaults_when_absent() -> None:
    cfg = _load_work_queue_config({})
    assert cfg.enabled is False
    assert cfg.lease_ttl_seconds == 60.0
    assert cfg.heartbeat_interval_seconds == 20.0
    assert cfg.max_attempts == 5
    assert cfg.batch_size == 10


def test_load_work_queue_overrides() -> None:
    cfg = _load_work_queue_config(
        {"agent": {"work_queue": {"enabled": True, "batch_size": 3, "poll_interval_seconds": 0.5}}}
    )
    assert cfg.enabled is True
    assert cfg.batch_size == 3
    assert cfg.poll_interval_seconds == 0.5


def test_load_work_queue_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="必须大于 heartbeat_interval_seconds"):
        _load_work_queue_config(
            {"agent": {"work_queue": {"lease_ttl_seconds": 10, "heartbeat_interval_seconds": 20}}}
        )
    with pytest.raises(ValueError, match="最多 5"):
        _load_work_queue_config({"agent": {"work_queue": {"max_attempts": 6}}})
    with pytest.raises(ValueError, match="必须为正整数"):
        _load_work_queue_config({"agent": {"work_queue": {"batch_size": 0}}})
    with pytest.raises(ValueError, match="max_error_backoff_seconds 必须"):
        _load_work_queue_config(
            {
                "agent": {
                    "work_queue": {
                        "error_backoff_seconds": 10,
                        "max_error_backoff_seconds": 1,
                    }
                }
            }
        )


def test_config_default_has_work_queue_section() -> None:
    """`Config` 默认自带 `WorkQueueConfig`（enabled=False），无需配置即安全。"""
    cfg = Config(provider="p", model="m", api_key="k", system_prompt="s")
    assert isinstance(cfg.work_queue, WorkQueueConfig)
    assert cfg.work_queue.enabled is False
