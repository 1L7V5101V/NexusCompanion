"""sync 栈 URL 方言规范化回归（pg-durable-sot-cutover 部署踩坑）。

生产 config 的 postgres_url 为 asyncpg 方言（auth/work_queue 等 async 消费方
直用）；sync 栈（psycopg）必须剥成裸 libpq 串。此前四处只剥 +psycopg，
backend=postgres 首次生产启动即崩（missing "=" after URL）。
"""

from __future__ import annotations

from infra.storage.factory import _check_pg_schema
from infra.storage.pool import _normalize_url


def test_normalize_url_strips_asyncpg_dialect() -> None:
    assert (
        _normalize_url("postgresql+asyncpg://u:p@host:5432/db")
        == "postgresql://u:p@host:5432/db"
    )


def test_normalize_url_strips_psycopg_dialect() -> None:
    assert (
        _normalize_url("postgresql+psycopg://u:p@host:5432/db")
        == "postgresql://u:p@host:5432/db"
    )


def test_normalize_url_keeps_plain_url() -> None:
    assert (
        _normalize_url("postgresql://u:p@host:5432/db")
        == "postgresql://u:p@host:5432/db"
    )


def test_check_pg_schema_accepts_asyncpg_dialect_url(monkeypatch) -> None:
    """_check_pg_schema 收到 asyncpg 方言 URL 时剥成裸串再连接（不再传方言串给 libpq）。"""
    captured: dict[str, str] = {}

    class _FakeConn:
        def close(self) -> None:
            pass

        def execute(self, query: str, params: tuple) -> Any:
            _ = query, params

            class _Row:
                def fetchone(self) -> tuple:
                    return ("t",)

            return _Row()

    def _fake_connect(url: str, **kwargs: object) -> _FakeConn:
        captured["url"] = url
        return _FakeConn()

    import psycopg

    monkeypatch.setattr(psycopg, "connect", _fake_connect)
    _check_pg_schema("postgresql+asyncpg://u:p@host:5432/db", "sessions")
    assert captured["url"] == "postgresql://u:p@host:5432/db"

from typing import Any  # noqa: E402
