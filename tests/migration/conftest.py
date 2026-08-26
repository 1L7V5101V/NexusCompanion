"""migration 集成测试 fixture：会话级独立 scratch DB + 样例 workspace。

- ``mig_pg_url``（session）：创建/迁移/销毁独立数据库 ``nexus_migtest``，
  避免与共享 ``nexus`` 库（其它 PG 测试并发使用）互相 TRUNCATE 冲突。
- ``sample_ws``（function）：确定性小样例（500 messages / 120 memory）。
- ``truncate_all``：清空全部迁移目标表，保证每个用例从空库开始。
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from alembic.config import Config

from scripts.migrate.alembic_util import upgrade_head
from scripts.migrate.config import MigrationConfig
from scripts.migrate.sample_workspace import generate_workspace

from tests.migration.helpers import MIGRATION_TABLES, truncate_tables

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)
SCRATCH_DB = "nexus_migtest"


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture(scope="session")
def mig_pg_url() -> Iterator[str]:
    if not _pg_alive(ADMIN_URL):
        pytest.skip(f"本地 PG 不可用（{ADMIN_URL}），跳过 migration 集成测试")
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.execute(f"CREATE DATABASE {SCRATCH_DB}")
    admin.close()
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + SCRATCH_DB
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.close()
    logging.getLogger("alembic").setLevel(logging.CRITICAL)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option(
        "sqlalchemy.url",
        url.replace("postgresql://", "postgresql+psycopg://"),
    )
    upgrade_head(cfg)
    yield url
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
    admin.close()


@pytest.fixture
def truncate_all(mig_pg_url) -> None:
    truncate_tables(mig_pg_url)


SAMPLE_MAPPING = {
    "telegram": "tenant_a",
    "discord": "tenant_a",
    "whatsapp": "tenant_b",
    "wechat": "tenant_b",
    "slack": "tenant_c",
    "memory": "tenant_mem",
}


@pytest.fixture
def sample_ws(tmp_path):
    ws = generate_workspace(
        tmp_path / "ws", n_messages=500, n_memory=120, n_ticks=20, seed=7
    )
    (ws / "mapping.json").write_text(
        json.dumps(SAMPLE_MAPPING), encoding="utf-8"
    )
    return ws


@pytest.fixture
def make_cfg(mig_pg_url, sample_ws, tmp_path):
    def _make(run_id: str, **kw) -> MigrationConfig:
        defaults: dict = dict(
            workspace=sample_ws,
            pg_url=mig_pg_url,
            mapping_path=sample_ws / "mapping.json",
            run_id=run_id,
            checkpoint_dir=tmp_path / "ckpt",
            results_dir=tmp_path / "results",
            state_path=tmp_path / "state.json",
        )
        defaults.update(kw)
        return MigrationConfig(**defaults)

    return _make
