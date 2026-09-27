"""C7 task 8.2/3.1：跨租户并发交错测试（共享 registry 无串租户，PG）。

使用真实 C5 provisioning 身份链（两个 active 账号 → 两个独立 tenant）：
- 双租户持久化目录互不可达（经 registry 执行链 + TenantPathResolver）；
- **并发交错**：两租户 turn 交替写/读同一共享注册表，断言无一跨租户泄漏；
- per-tenant 工具目录不含关闭工具（shell/mcp_add/task_output 等）。

验证要求：交错测试 ≥3 次重跑稳定通过。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent.tools.context import ToolExecutionContext
from agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from agent.tools.path_resolver import TenantPathResolver
from agent.tools.registry import ToolRegistry

pytestmark = pytest.mark.postgres

_INTERLEAVE_ROUNDS = 24


def _ctx(account: dict, *, turn_suffix: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id=f"req-{account['tenant_id']}-{turn_suffix}",
        account_id=account["account_id"],
        tenant_id=account["tenant_id"],
        session_id=f"chat:{account['tenant_id']}",
        turn_id=f"turn-{account['tenant_id']}-{turn_suffix}",
        channel="webchat",
        chat_id=account["conversation_id"],
        principal_type="user",
    )


def _make_file_registry(tmp_path: Path) -> ToolRegistry:
    allowed = tmp_path / "legacy_root"
    allowed.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry()
    registry.register(WriteFileTool(allowed_dir=allowed), risk="write")
    registry.register(ReadFileTool(allowed_dir=allowed), risk="read-only")
    registry.register(ListDirTool(allowed_dir=allowed), risk="read-only")
    registry.set_path_resolver(TenantPathResolver(tmp_path / "ws", multi_tenant=True))
    return registry


async def test_provisioned_tenants_isolated_roots(c7_tenants, tmp_path: Path) -> None:
    """真实身份链：账号 A 写入只落在 A 的 tenant root，B 读不到（跨租户负向）。"""
    tenants = await c7_tenants()
    registry = _make_file_registry(tmp_path)

    result = await registry.execute(
        "write_file",
        {"path": "notes/secret.md", "content": "A 的秘密"},
        context=_ctx(tenants["a"], turn_suffix="p1"),
    )
    assert isinstance(result, str) and ("成功" in result or "写入" in result)

    # 只存在一份（A 的 tenant root 内），B 的 root 下没有。
    written = list(
        (tmp_path / "ws" / "tenants").glob("*/workspace/notes/secret.md")
    )
    assert len(written) == 1
    tenant_a_root = written[0].parent.parent.parent

    read_b = await registry.execute(
        "read_file",
        {"path": "notes/secret.md"},
        context=_ctx(tenants["b"], turn_suffix="p2"),
    )
    assert isinstance(read_b, str)
    assert "A 的秘密" not in read_b and "错误" in read_b


async def test_interleaved_concurrent_turns_never_cross_tenants(
    c7_tenants, tmp_path: Path
) -> None:
    """8.2 核心：共享 registry 下两租户 turn 并发交错，无一跨租户泄漏。"""
    tenants = await c7_tenants()
    registry = _make_file_registry(tmp_path)

    async def _turn(label: str) -> list[str]:
        account = tenants[label]
        reads: list[str] = []
        for i in range(_INTERLEAVE_ROUNDS):
            ctx = _ctx(account, turn_suffix=f"t{i}")
            await registry.execute(
                "write_file",
                {
                    "path": f"notes/{label}-{i}.md",
                    "content": f"{label}-secret-{i}",
                },
                context=ctx,
            )
            listed = await registry.execute(
                "list_dir", {"path": "notes"}, context=ctx
            )
            assert label in str(listed)  # 自己的文件在列
            read = await registry.execute(
                "read_file", {"path": f"notes/{label}-{i}.md"}, context=ctx
            )
            assert isinstance(read, str) and f"{label}-secret-{i}" in read
            reads.append(str(read))
            await asyncio.sleep(0)  # 交替让出控制权，制造交错
        return reads

    results = await asyncio.gather(_turn("a"), _turn("b"))
    a_reads, b_reads = results

    # 交叉校验：A 的全部读取只含 A 内容，B 的只含 B 内容（无串租户）。
    assert all("a-secret-" in text for text in a_reads)
    assert all("b-secret-" in text for text in b_reads)
    assert all("b-secret-" not in text for text in a_reads)
    assert all("a-secret-" not in text for text in b_reads)


async def test_per_tenant_catalog_excludes_closed_tools(
    c7_tenants, tmp_path: Path
) -> None:
    """3.1：per-tenant 目录 = 白名单 ∩ 已启用 − 关闭清单（含 Provisioned 身份）。"""
    tenants = await c7_tenants()
    registry = _make_file_registry(tmp_path)
    closed = {"shell", "task_output", "task_stop", "spawn", "mcp_add", "mcp_remove"}

    for label in ("a", "b"):
        from agent.tools.catalog import tenant_visible_names

        visible = tenant_visible_names(
            registry, _ctx(tenants[label], turn_suffix="cat")
        )
        assert visible is not None
        assert closed & visible == set(), f"tenant={label} 泄露关闭工具"
        # 白名单工具在（谓词级验证目录来自同一 catalog 源）。
        assert {"read_file", "write_file", "list_dir"} <= visible