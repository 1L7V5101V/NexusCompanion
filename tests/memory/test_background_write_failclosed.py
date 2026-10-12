"""c18 task 4.3：后台写入拿不到可信租户归属时 fail-closed。

判据不是「别抛异常」而是三件事同时成立：**不发 LLM 调用**（省掉一次没有归属的提取）、
**不落任何画像**（既不落别人的桶，也不落 `default` 那个桶）、**留一条能定位的记录**。
最后一条容易被写成 `except: pass`——那种写法下故障表现为「画像不再更新」，日志里查不到
为什么，而画像静默失效正是本 change 要修的病症之一。

三条后台路径各一例：整理（`consolidate`）、压缩（`refresh_recent_turns`）、优化器消费
pending（`MemoryOptimizer.optimize`）。优化器那一例多断一件事——解析器在场但归属为空时
连 pending 都不读：「读了却不敢写」比「不读」更难查。
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
    MarkdownMemoryStore,
    RefreshRecentTurnsRequest,
)
from tests.memory.profile_write_cases import (
    TENANT_A,
    TenantStores,
    consolidation_provider,
    consolidation_session,
    file_backed_maintenance,
)

DEFAULT_TENANT_ID = "default"


@pytest.fixture()
def stores(tmp_path: Path) -> Iterator[TenantStores]:
    holder = TenantStores.create(tmp_path)
    try:
        yield holder
    finally:
        holder.close()


def _maintenance(workspace: Path, resolver: Any, provider: Any) -> MarkdownMemoryMaintenance:
    maintenance = file_backed_maintenance(workspace, provider)
    maintenance.bind_profile_stores(cast(Any, resolver))
    return maintenance


async def test_consolidation_without_tenant_writes_nothing(
    tmp_path: Path, stores: TenantStores
) -> None:
    workspace = tmp_path / "ws"
    provider = consolidation_provider()
    maintenance = _maintenance(workspace, stores.resolve, provider)

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
    maintenance = _maintenance(workspace, _resolver, provider)

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
    maintenance = _maintenance(workspace, stores.resolve, provider)

    with caplog.at_level(logging.ERROR, logger="memory.markdown"):
        await maintenance.refresh_recent_turns(
            RefreshRecentTurnsRequest(session=consolidation_session(), tenant_id="   ")
        )

    assert stores.count_rows(DEFAULT_TENANT_ID, "tenant_memory_profile_sections") == 0
    assert not (workspace / "memory" / "RECENT_CONTEXT.md").exists()
    assert "fail-closed" in "\n".join(r.getMessage() for r in caplog.records)


async def test_without_a_resolver_the_file_backend_still_writes(tmp_path: Path) -> None:
    """没接解析器时仍按旧形状写文件——这条回退是 6.10 要连着门面一起删掉的过渡期。

    把它写成用例而不是口头交代，是因为「装配根忘了绑解析器」与「文件后端被摘掉」得能区分
    开：前者的行为是本例的样子，后者会让构造签名直接不存在。
    """
    workspace = tmp_path / "ws"
    provider = consolidation_provider()
    maintenance = file_backed_maintenance(workspace, provider)

    result = await maintenance.consolidate(
        ConsolidateRequest(
            session=consolidation_session(), archive_all=True, tenant_id=""
        )
    )

    assert result.trace.get("mode") == "markdown"
    history = workspace / "memory" / "HISTORY.md"
    assert history.exists()
    assert "Zigbee" in history.read_text(encoding="utf-8")

async def test_optimizer_without_tenant_writes_nothing(
    tmp_path: Path, stores: TenantStores, caplog: pytest.LogCaptureFixture
) -> None:
    """第三条后台路径：优化器消费 pending（task 6.11 之后走专表）。"""
    from datetime import datetime, timezone

    from proactive_v2.memory_optimizer import MemoryOptimizer

    workspace = tmp_path / "ws"
    store = stores.resolve(TENANT_A)
    await store.append_entry(
        "pending",
        "- [identity] 不该被谁吃掉",
        happened_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
    )
    provider = consolidation_provider()
    optimizer = MemoryOptimizer(
        MarkdownMemoryStore(workspace),
        cast(Any, provider),
        "m",
        profile_stores=stores.resolve,
    )

    with caplog.at_level(logging.ERROR, logger="proactive_v2.memory_optimizer"):
        await optimizer.optimize("")

    assert provider.chat.await_count == 0  # type: ignore[attr-defined]
    assert "不该被谁吃掉" in await store.read_entries("pending")
    assert any("fail-closed" in r.getMessage() for r in caplog.records)

