"""c18 task 4.3：后台写入拿不到可信租户归属时 fail-closed。

判据不是「别抛异常」而是三件事同时成立：**不发 LLM 调用**（省掉一次没有归属的提取）、
**不落任何画像**（既不落别人的桶，也不落 `default` 那个桶）、**留一条能定位的记录**。
最后一条容易被写成 `except: pass`——那种写法下故障表现为「画像不再更新」，日志里查不到
为什么，而画像静默失效正是本 change 要修的病症之一。

三条后台路径各一例：整理（`consolidate`）、压缩（`refresh_recent_turns`）、优化器消费
pending（`MemoryOptimizer.optimize`）。优化器那一例多断一件事——解析器在场但归属为空时
连 pending 都不读：「读了却不敢写」比「不读」更难查。

task 6.10 之后画像层没有文件后端，所以这里没有「没接解析器就回退写文件」那条例外——
回退本身已经不存在，构造签名直接要求解析器。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from core.memory.markdown import (
    ConsolidateRequest,
    MarkdownMemoryMaintenance,
    RefreshRecentTurnsRequest,
)
from tests.memory.profile_write_cases import (
    TENANT_A,
    TenantStores,
    build_maintenance,
    consolidation_provider,
    consolidation_session,
)

DEFAULT_TENANT_ID = "default"


@pytest.fixture()
def stores(tmp_path: Path) -> Iterator[TenantStores]:
    holder = TenantStores.create(tmp_path)
    try:
        yield holder
    finally:
        holder.close()


def _maintenance(resolver: Any, provider: Any) -> MarkdownMemoryMaintenance:
    return build_maintenance(resolver, provider)


async def test_consolidation_without_tenant_writes_nothing(
    tmp_path: Path, stores: TenantStores
) -> None:
    workspace = tmp_path / "ws"
    provider = consolidation_provider()
    maintenance = _maintenance(stores.resolve, provider)

    result = await maintenance.consolidate(
        ConsolidateRequest(
            session=consolidation_session(), archive_all=True, tenant_id=""
        )
    )

    assert result.trace.get("reason") == "fail_closed_no_tenant"
    assert result.consolidated_count == 0
    assert provider.chat.await_count == 0  # type: ignore[attr-defined]
    assert stores.count_rows(DEFAULT_TENANT_ID, "tenant_memory_profile_entries") == 0
    assert stores.count_rows(TENANT_A, "tenant_memory_profile_entries") == 0
    assert not (workspace / "memory" / "HISTORY.md").exists()


async def test_consolidation_with_unresolvable_tenant_logs_locatable_skip(
    tmp_path: Path, stores: TenantStores, caplog: pytest.LogCaptureFixture
) -> None:
    """解析器报错（PG 查不到该租户、SQLite 收到非固定租户）也只跳过，不降级写共享目录。"""
    workspace = tmp_path / "ws"

    def _resolver(tenant_id: str) -> Any:
        raise RuntimeError("该租户在 PG 里没有画像行")

    provider = consolidation_provider()
    maintenance = _maintenance(_resolver, provider)

    with caplog.at_level(logging.ERROR, logger="memory.markdown"):
        result = await maintenance.consolidate(
            ConsolidateRequest(
                session=consolidation_session(), archive_all=True, tenant_id="ghost"
            )
        )

    assert result.trace.get("reason") == "fail_closed_no_tenant"
    assert provider.chat.await_count == 0  # type: ignore[attr-defined]
    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "fail-closed" in joined, "跳过必须留下可定位的记录"
    assert "ghost" in joined, "记录要带上当时的归属，否则查不到是哪一轮"
    assert stores.count_rows("ghost", "tenant_memory_profile_entries") == 0


async def test_refresh_without_tenant_writes_nothing(
    tmp_path: Path, stores: TenantStores, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = tmp_path / "ws"
    provider = consolidation_provider()
    maintenance = _maintenance(stores.resolve, provider)

    with caplog.at_level(logging.ERROR, logger="memory.markdown"):
        await maintenance.refresh_recent_turns(
            RefreshRecentTurnsRequest(session=consolidation_session(), tenant_id="   ")
        )

    assert stores.count_rows(DEFAULT_TENANT_ID, "tenant_memory_profile_sections") == 0
    assert not (workspace / "memory" / "RECENT_CONTEXT.md").exists()
    assert "fail-closed" in "\n".join(r.getMessage() for r in caplog.records)


async def test_optimizer_without_tenant_writes_nothing(
    tmp_path: Path, stores: TenantStores, caplog: pytest.LogCaptureFixture
) -> None:
    """第三条后台路径：优化器消费 pending（task 6.11 之后走专表）。

    这里连一次读取都不该发生——归属为空时「读了 pending 却不敢写」比「不读」更难查。
    """
    from datetime import datetime, timezone

    from proactive_v2.memory_optimizer import MemoryOptimizer

    store = stores.resolve(TENANT_A)
    await store.append_entry(
        "pending",
        "- [identity] 不该被谁吃掉",
        happened_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
    )
    provider = consolidation_provider()
    optimizer = MemoryOptimizer(
        cast(Any, provider), "m", profile_stores=stores.resolve
    )

    with caplog.at_level(logging.ERROR, logger="proactive_v2.memory_optimizer"):
        await optimizer.optimize("")

    assert provider.chat.await_count == 0  # type: ignore[attr-defined]
    assert "不该被谁吃掉" in await store.read_entries("pending")
    assert any("fail-closed" in r.getMessage() for r in caplog.records)
