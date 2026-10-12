"""MemoryOptimizer 的当前行为（c18 task 6.11 之后：pending 消费走租户专表）。

原来这几条断言的是文件版那套两阶段骨架（`snapshot_pending` 把正文原子重命名藏起来，
成功走 `commit_pending_snapshot`、失败走 `rollback_pending_snapshot`）。表上没有那三步，
承重的是另一件事：**「删掉这轮吃进去的行」与「覆写 long_term」是同一个事务**，所以
合并没成功时 pending 一条都不会少。这条改完就把那个补偿结构整个删掉了。

`SELF` 那一路今天仍写文件（`relationship_io` 为 None 时）——`read_self` 归 ADR-9 的
persona 缝，删除动作在 task 6.3，不在这里。
"""

from typing import Any, cast
import asyncio
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from proactive_v2.memory_optimizer import (
    MemoryOptimizerBusy,
    MemoryOptimizer,
    MemoryOptimizerLoop,
)
from bootstrap.persona import SqliteRelationshipIO
from tests.memory.profile_write_cases import TenantStores

TENANT = "c18-optimizer"
_OTHER_TENANT = "c18-optimizer-bystander"
_TS = datetime(2026, 3, 3, 10, 0, 0, tzinfo=timezone.utc)


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


def _provider_with_responses(*responses: str) -> object:
    provider = types.SimpleNamespace()
    provider.chat = AsyncMock(side_effect=[_Resp(x) for x in responses])
    return provider


def _provider_raising(error: Exception) -> object:
    provider = types.SimpleNamespace()
    provider.chat = AsyncMock(side_effect=error)
    return provider


def _setup(tmp_path) -> TenantStores:
    """一个建好画像专表的单机库（task 6.10：optimizer 只有这一个画像入口）。"""
    return TenantStores.create(tmp_path)


def _optimizer(stores: TenantStores, provider: object) -> MemoryOptimizer:
    optimizer = MemoryOptimizer(
        cast(Any, provider),
        "test-model",
        profile_stores=stores.resolve,
        relationship_io=SqliteRelationshipIO(stores.resolve(TENANT)),
    )
    optimizer._STEP_DELAY_SECONDS = 0  # type: ignore[misc]
    return optimizer


def _seed(
    stores: TenantStores,
    *,
    long_term: str = "",
    pending: list[str] | None = None,
    history: list[str] | None = None,
) -> None:
    """往该租户的专表里播种本轮优化的输入。"""
    pending = pending or []
    history = history or []

    async def run() -> None:
        store = stores.resolve(TENANT)
        if long_term:
            await store.write_section("long_term", long_term)
        for index, text in enumerate(pending):
            await store.append_entry(
                "pending", text, happened_at=_TS.replace(microsecond=index + 1)
            )
        for text in history:
            await store.append_entry("history", text, happened_at=_TS)

    asyncio.run(run())


def _read(stores: TenantStores, category: str) -> str:
    async def run() -> str:
        store = stores.resolve(TENANT)
        if category in ("long_term", "recent_context", "relationship_state"):
            return await store.read_section(category)
        return await store.read_entries(cast(Any, category))

    return asyncio.run(run())


def test_optimize_skips_when_long_term_and_pending_are_empty(tmp_path) -> None:
    stores = _setup(tmp_path)
    provider = types.SimpleNamespace(chat=AsyncMock())
    try:
        asyncio.run(_optimizer(stores, provider).optimize(TENANT))
        provider.chat.assert_not_called()
    finally:
        stores.close()


def test_optimize_merges_pending_into_long_term_in_one_transaction(tmp_path) -> None:
    """合并成功后：long_term 换成合并结果、pending 行被删掉、不再产生 `MEMORY.bak.md`。

    备份文件那一层保护是文件时代「原子重命名 + 覆写」的产物；表上同一事务里覆写，
    中止即回滚，不需要也不该留一份旁路副本。
    """
    stores = _setup(tmp_path)
    _seed(stores, long_term="old profile", pending=["- [identity] 新身份"])
    provider = _provider_with_responses(
        "## 用户画像\n- 新版本\n",
        "# Rachael 的自我认知\n\n## 人格与形象\n- 保持\n",
    )
    try:
        asyncio.run(_optimizer(stores, provider).optimize(TENANT))

        assert _read(stores, "long_term").strip() == "## 用户画像\n- 新版本"
        assert _read(stores, "pending") == "", "合并吃进去的行必须被取走"
        assert not (tmp_path / "memory" / "MEMORY.bak.md").exists()
        assert stores.count_rows(_OTHER_TENANT, "tenant_memory_profile_entries") == 0
    finally:
        stores.close()


def test_optimize_keeps_pending_rows_when_merge_returns_empty(tmp_path) -> None:
    stores = _setup(tmp_path)
    _seed(stores, long_term="old profile", pending=["- pending fact"])
    provider = _provider_with_responses(
        "",
        "# Rachael 的自我认知\n\n## 人格与形象\n- 保持\n",
    )
    try:
        asyncio.run(_optimizer(stores, provider).optimize(TENANT))

        assert "- pending fact" in _read(stores, "pending")
        assert _read(stores, "long_term") == "old profile"
    finally:
        stores.close()


def test_optimize_keeps_pending_rows_when_merge_raises(tmp_path) -> None:
    """抛异常那一路也在同一个事务之外：没有任何写入落库，pending 原样在表里。"""
    stores = _setup(tmp_path)
    _seed(stores, long_term="old profile", pending=["- pending fact"])
    provider = _provider_raising(RuntimeError("llm down"))
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(_optimizer(stores, provider).optimize(TENANT))

        assert "- pending fact" in _read(stores, "pending")
        assert _read(stores, "long_term") == "old profile"
    finally:
        stores.close()


def test_optimize_updates_self_using_pending_only(tmp_path) -> None:
    stores = _setup(tmp_path)
    _seed(
        stores,
        long_term="old",
        pending=["- [preference] 回复保持简洁。"],
        history=["[2026-03-03 10:00] USER: 这段历史不该进入 SELF"],
    )
    provider = _provider_with_responses(
        "## 新记忆",
        "# Nexus 的自我认知\n\n## 人格与形象\n\n- 新版人格\n\n## 我对当前用户的理解\n\n- 新版理解\n\n## 我们关系的定义\n\n- 新版关系\n",
    )
    try:
        asyncio.run(_optimizer(stores, provider).optimize(TENANT))

        updated_self = _read(stores, "relationship_state")
        assert updated_self.strip().startswith("# Nexus 的自我认知")
        assert "新版理解" in updated_self

        self_prompt = provider.chat.await_args_list[1].kwargs["messages"][1]["content"]  # type: ignore[attr-defined]
        assert "- [preference] 回复保持简洁。" in self_prompt
        assert "这段历史不该进入 SELF" not in self_prompt
    finally:
        stores.close()


def test_merge_memory_ignores_history_and_only_uses_pending(tmp_path) -> None:
    stores = _setup(tmp_path)
    _seed(
        stores,
        long_term="old profile",
        pending=["- [identity] 新身份"],
        history=["[2026-03-03 10:00] USER: 这段历史不该进入长期记忆"],
    )
    provider = _provider_with_responses(
        "## 用户画像\n- 新版本\n",
        "# Rachael 的自我认知\n\n## 人格与形象\n- 保持\n",
    )
    try:
        asyncio.run(_optimizer(stores, provider).optimize(TENANT))

        call = provider.chat.await_args_list[0]  # type: ignore[attr-defined]
        prompt = call.kwargs["messages"][1]["content"]
        assert "这段历史不该进入长期记忆" not in prompt
        assert "- [identity] 新身份" in prompt
    finally:
        stores.close()


def test_request_text_response_uses_expected_chat_kwargs(tmp_path) -> None:
    stores = _setup(tmp_path)
    provider = _provider_with_responses("merged")
    optimizer = _optimizer(stores, provider)

    try:
        result = asyncio.run(
            optimizer._request_text_response(
                system_content="system",
                user_content="user",
                max_tokens=123,
            )
        )

        assert result == "merged"
        kwargs = provider.chat.await_args.kwargs  # type: ignore[attr-defined]
        assert kwargs["tools"] == []
        assert kwargs["model"] == "test-model"
        assert kwargs["max_tokens"] == 123
    finally:
        stores.close()


def test_optimize_reports_busy_instead_of_waiting(tmp_path) -> None:
    async def run_case() -> None:
        stores = _setup(tmp_path)
        provider = types.SimpleNamespace(chat=AsyncMock())
        optimizer = _optimizer(stores, provider)
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_optimize(*args: Any, **kwargs: Any) -> None:
            started.set()
            await release.wait()

        optimizer._optimize = blocked_optimize  # type: ignore[method-assign]
        running = asyncio.create_task(optimizer.optimize())
        await started.wait()

        assert optimizer.is_running
        with pytest.raises(MemoryOptimizerBusy):
            await optimizer.optimize()

        release.set()
        await running
        stores.close()

    asyncio.run(run_case())


def test_seconds_until_next_tick_aligns_to_interval_boundary():
    now = datetime(2026, 2, 23, 12, 34, 56)
    loop = MemoryOptimizerLoop(None, interval_seconds=3600, _now_fn=lambda: now)

    secs = loop._seconds_until_next_tick()

    assert abs(secs - (25 * 60 + 4)) < 0.001


def test_seconds_until_next_tick_always_positive():
    for h in range(24):
        now = datetime(2026, 2, 23, h, 59, 59)
        loop = MemoryOptimizerLoop(None, interval_seconds=300, _now_fn=lambda n=now: n)
        assert loop._seconds_until_next_tick() > 0
