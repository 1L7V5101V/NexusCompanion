"""TraceStore 双后端一致性：sqlite → postgres 切换后运行 turn，追踪记录均完整（C0 任务 3.3）。

spec 场景「后端切换后追踪连续」：同一 TurnState 口径下，两后端都产出完整且
跨 phase 关联的 TraceSpan（按 turn_id 聚合的 before_turn → after_turn 五段）。
PG 不可用时自动 skip（不 fail）；PG URL 可用 NEXUS_TEST_PG_URL 覆盖。
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import psycopg
import pytest

from agent.config_models import StorageConfig
from core.telemetry.trace_store import TraceStore
from scripts.load.driver import build_core, drive_turns
from scripts.load.result import LoadSpec

PG_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)

EXPECTED_PHASES = ["before_turn", "before_reasoning", "reasoner", "after_reasoning", "after_turn"]


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


def _make_spec(backend: str, workspace: Path, tenant: str) -> LoadSpec:
    return LoadSpec(
        scenario="simulate",
        backend=backend,
        tenants=(tenant,),
        channels=("cli",),
        rounds=1,
        concurrency=1,
        reasoner="script",
        seed=42,
        workspace=str(workspace),
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_trace_complete_and_correlated(backend: str, tmp_path: Path) -> None:
    if backend == "postgres" and not _pg_alive(PG_URL):
        pytest.skip(f"本地 PG 不可用（{PG_URL}），跳过 postgres trace parity")

    tenant = f"parity_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    spec = _make_spec(backend, tmp_path, tenant)
    store = TraceStore(max_entries=4096, dump_dir=tmp_path / "traces")
    core, _sm, runtime = build_core(
        workspace=tmp_path,
        storage=StorageConfig(backend=backend),
        spec=spec,
        trace_store=store,
    )
    try:
        stats = asyncio.run(drive_turns(core, spec))
    finally:
        runtime.close()

    assert stats.overall.attempted == 1
    assert stats.overall.succeeded == 1

    turns = sorted({s.turn_id for s in store.all_spans()})
    assert len(turns) == 1, "单 turn 应只有一个 turn_id"
    turn_id = turns[0]
    spans = store.spans_for(turn_id)

    assert [s.phase for s in spans] == EXPECTED_PHASES, (
        f"后端 {backend} 的 phase 序列不完整"
    )
    assert all(s.status == "ok" for s in spans)
    assert all(s.duration_ms >= 0 for s in spans)
    # 同一 turn_id 跨 phase 可关联：turn_id 字段一致、按进入顺序可复现。
    assert {s.turn_id for s in spans} == {turn_id}
