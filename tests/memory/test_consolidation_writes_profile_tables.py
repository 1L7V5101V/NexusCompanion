"""c18 task 6.12：整理与压缩的读写落在租户专表上，不再落在共享目录。

这个 change 之前，画像层的**读**侧已经切到专表（3.4/6.9），而写侧还整条留在
`MarkdownMemoryMaintenance` 的文件 store 上——6.10 一摘文件后端，运行时就没有任何东西
写这两张表了。本文件验的是写入侧改完之后的四件事：

1. 一轮整理把 history / pending / recent_context / journal 四类都落到**该租户**的行里；
2. 整理读的是那个租户的 `long_term`，不是进程共享的 `MEMORY.md`；
3. 同一个窗口重放一次不产生第二行（幂等键从 `app_configs` 的全局键换成
   `(tenant_id, dedup_key)`，ADR-13）；
4. 另一个租户的桶保持为空，且文件后端一个字节都不再多写。

装配件（解析器、假 provider、会话）在 `profile_write_cases.py`，与 4.3 那两个用例同源。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.memory.markdown import (
    ConsolidateRequest,
    MarkdownMemoryMaintenance,
    RefreshRecentTurnsRequest,
)
from agent.memory import MemoryStore
from tests.memory.profile_write_cases import (
    HISTORY_ENTRY,
    TENANT_A,
    TENANT_B,
    TenantStores,
    consolidation_provider,
    consolidation_session,
    file_backed_maintenance,
)


@pytest.fixture()
def stores(tmp_path: Path) -> Iterator[TenantStores]:
    holder = TenantStores.create(tmp_path)
    try:
        yield holder
    finally:
        # `-W error` 下未关的 sqlite 连接会由 GC 在后面的用例里报 ResourceWarning，
        # 并被算到一个不相干的用例头上（本 change 已经栽过三次）。
        holder.close()


def _maintenance(
    workspace: Path, stores: TenantStores, provider: Any = None
) -> MarkdownMemoryMaintenance:
    provider = provider or consolidation_provider()
    maintenance = file_backed_maintenance(workspace, provider)
    maintenance.bind_profile_stores(stores.resolve)
    return maintenance


async def test_consolidation_writes_the_four_profile_categories(
    tmp_path: Path, stores: TenantStores
) -> None:
    workspace = tmp_path / "ws"
    maintenance = _maintenance(workspace, stores)

    result = await maintenance.consolidate(
        ConsolidateRequest(session=consolidation_session(), archive_all=True, tenant_id=TENANT_A)
    )

    assert result.trace.get("mode") == "markdown"
    store = stores.resolve(TENANT_A)
    assert HISTORY_ENTRY in await store.read_entries("history")
    assert "用户偏好本地控制" in await store.read_entries("pending")
    assert "Zigbee" in await store.read_section("recent_context")
    # journal 的日期占 happened_at 列，正文不再拼 `**YYYY-MM-DD**` 前缀
    assert HISTORY_ENTRY in await store.read_entries("journal")

    memory_dir = workspace / "memory"
    assert not (memory_dir / "HISTORY.md").exists()
    assert not (memory_dir / "RECENT_CONTEXT.md").exists()
    assert not (memory_dir / "MEMORY.bak.md").exists()
    assert not (memory_dir / "journal" / "2026-03-15.md").exists()
    assert (memory_dir / "PENDING.md").read_text(encoding="utf-8") == ""

    assert stores.count_rows(TENANT_B, "tenant_memory_profile_entries") == 0
    assert stores.count_rows(TENANT_B, "tenant_memory_profile_sections") == 0


async def test_consolidation_reads_the_tenant_long_term(
    tmp_path: Path, stores: TenantStores
) -> None:
    """整理前那次「现有长期记忆」读的是专表里该租户那一行，不是共享的 MEMORY.md。"""
    workspace = tmp_path / "ws"
    await stores.resolve(TENANT_A).write_section("long_term", "该租户的长期画像正文")
    MemoryStore(workspace).write_long_term("文件里的旧画像")
    provider = consolidation_provider()

    await _maintenance(workspace, stores, provider).consolidate(
        ConsolidateRequest(
            session=consolidation_session(), archive_all=True, tenant_id=TENANT_A
        )
    )

    joined = "\n".join(provider.prompts)  # type: ignore[attr-defined]
    assert "该租户的长期画像正文" in joined
    assert "文件里的旧画像" not in joined


async def test_replaying_the_same_window_does_not_double_rows(
    tmp_path: Path, stores: TenantStores
) -> None:
    """同一窗口跑两次（重启后重放）不产生第二行：幂等键是 (tenant_id, dedup_key)。"""
    workspace = tmp_path / "ws"
    maintenance = _maintenance(workspace, stores)
    session = consolidation_session()

    await maintenance.consolidate(
        ConsolidateRequest(session=session, archive_all=True, tenant_id=TENANT_A)
    )
    await maintenance.consolidate(
        ConsolidateRequest(session=session, archive_all=True, tenant_id=TENANT_A)
    )

    assert stores.count_rows(TENANT_A, "tenant_memory_profile_entries", "history") == 1
    assert stores.count_rows(TENANT_A, "tenant_memory_profile_entries", "pending") == 1
    assert stores.count_rows(TENANT_A, "tenant_memory_profile_entries", "journal") == 1
    assert (
        stores.count_rows(TENANT_A, "tenant_memory_profile_sections", "recent_context")
        == 1
    )


async def test_refresh_recent_turns_writes_the_tenant_section(
    tmp_path: Path, stores: TenantStores
) -> None:
    """压缩路径（refresh recent turns）也走同一扇门：写的是该租户的 `recent_context` 行。"""
    workspace = tmp_path / "ws"
    maintenance = _maintenance(workspace, stores)
    await stores.resolve(TENANT_A).write_section(
        "recent_context", "topics:\n- 旧话题\n"
    )

    await maintenance.refresh_recent_turns(
        RefreshRecentTurnsRequest(session=consolidation_session(), tenant_id=TENANT_A)
    )

    written = await stores.resolve(TENANT_A).read_section("recent_context")
    assert "旧话题" in written, "覆写应当保留原有段落，而不是把整行换掉"
    assert "我买了 Zigbee 网关" in written
    assert (
        stores.count_rows(TENANT_A, "tenant_memory_profile_sections", "recent_context")
        == 1
    )
    assert not (workspace / "memory" / "RECENT_CONTEXT.md").exists()
    assert stores.count_rows(TENANT_B, "tenant_memory_profile_sections") == 0
