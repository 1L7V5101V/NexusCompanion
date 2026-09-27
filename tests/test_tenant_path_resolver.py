"""C7 task 5.1/5.2：TenantPathResolver——租户文件根与逃逸面矩阵。

对应 spec「租户路径解析与文件资源边界」：
- 多租户模式按租户解析 root，单机模式回退构造期 allowed_dir（owner 行为不变）；
- 绝对路径 / ``~`` / ``..`` / 越出资源根 / 层级超限一律拒绝；
- 跨租户文件互不可达（经 registry 执行链验证）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent.tools.base import Tool
from agent.tools.context import ToolExecutionContext
from agent.tools.path_resolver import PathResolveError, TenantPathResolver
from agent.tools.registry import ToolRegistry


def test_rejects_absolute_and_tilde() -> None:
    resolver = TenantPathResolver(Path("/ws"), multi_tenant=True)
    root = Path("/ws/tenants/t1/workspace")
    for bad in ("/etc/passwd", "~/secret", "C:\\x"):
        with pytest.raises(PathResolveError):
            resolver.resolve_relative(root, bad)


def test_rejects_dotdot_escape() -> None:
    resolver = TenantPathResolver(Path("/ws"), multi_tenant=True)
    root = Path("/ws/tenants/t1/workspace")
    for bad in ("../other/secret", "notes/../../system/config"):
        with pytest.raises(PathResolveError):
            resolver.resolve_relative(root, bad)


def test_rejects_depth_overflow_and_empty() -> None:
    resolver = TenantPathResolver(Path("/ws"), multi_tenant=True)
    root = Path("/ws/tenants/t1/workspace")
    with pytest.raises(PathResolveError):
        resolver.resolve_relative(root, "/".join(["d"] * 20))
    with pytest.raises(PathResolveError):
        resolver.resolve_relative(root, "")


def test_relative_path_resolves_inside_root(tmp_path: Path) -> None:
    resolver = TenantPathResolver(tmp_path, multi_tenant=True)
    root = tmp_path / "tenants" / "t1" / "workspace"
    resolved = resolver.resolve_relative(root, "notes/today.md")
    assert resolved == (root / "notes" / "today.md").resolve()


def test_multitenant_file_root_per_tenant(tmp_path: Path) -> None:
    """租户目录名经清洗（Windows 不允许 ":"），但彼此隔离且确定性一致。"""
    resolver = TenantPathResolver(tmp_path, multi_tenant=True)
    root_a = resolver.file_root(tenant_id="tenant:a", fallback=tmp_path)
    root_a2 = resolver.file_root(tenant_id="tenant:a", fallback=tmp_path)
    root_b = resolver.file_root(tenant_id="tenant:b", fallback=tmp_path)
    assert root_a == root_a2  # 确定性
    assert root_a != root_b
    assert root_a.parent.parent == tmp_path / "tenants"
    assert root_a.name == "workspace"


def test_single_tenant_mode_falls_back(tmp_path: Path) -> None:
    fallback = tmp_path / "existing"
    resolver = TenantPathResolver(tmp_path, multi_tenant=False)
    assert resolver.file_root(tenant_id="tenant:a", fallback=fallback) is fallback


def test_safe_segment_rejects_traversal(tmp_path: Path) -> None:
    resolver = TenantPathResolver(tmp_path, multi_tenant=True)
    ctx = _context()
    with pytest.raises(PathResolveError):
        resolver.exports_root(ctx, "../../escape")
    # 纯点段（清洗后以 . 开头）同样拒绝
    with pytest.raises(PathResolveError):
        resolver.exports_root(ctx, "...")


def _context(principal: str = "user") -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="req-1",
        account_id="acct-1",
        tenant_id="tenant:a",
        session_id="chat:tenant:a",
        turn_id="turn-1",
        channel="chat",
        chat_id="tenant:a",
        principal_type=principal,
    )


# ── task 5.2：经 registry 执行链的文件工具接线 ──────────────────────


def _make_file_tools(tmp_path: Path) -> ToolRegistry:
    from agent.tools.filesystem import (
        ListDirTool,
        ReadFileTool,
        WriteFileTool,
    )

    allowed = tmp_path / "legacy_root"
    allowed.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry()
    registry.register(ReadFileTool(allowed_dir=allowed), risk="read-only")
    registry.register(WriteFileTool(allowed_dir=allowed), risk="write")
    registry.register(ListDirTool(allowed_dir=allowed), risk="read-only")
    return registry


@pytest.mark.asyncio
async def test_single_tenant_mode_keeps_legacy_root(tmp_path: Path) -> None:
    """单机模式：写入与读取都落在 legacy root，行为与改动前一致。"""
    registry = _make_file_tools(tmp_path)
    resolver = TenantPathResolver(tmp_path, multi_tenant=False)
    registry.set_path_resolver(resolver)

    write = await registry.execute(
        "write_file", {"path": "notes/a.md", "content": "hello"}, context=None
    )
    assert "成功" in str(write) or "写入" in str(write)
    assert (tmp_path / "legacy_root" / "notes" / "a.md").exists()
    read = await registry.execute("read_file", {"path": "notes/a.md"}, context=None)
    assert "hello" in str(read)


@pytest.mark.asyncio
async def test_multitenant_roots_isolate_tenants(tmp_path: Path) -> None:
    """多租户模式：租户 A 写入的文件对租户 B 不可见（跨租户负向）。"""
    registry = _make_file_tools(tmp_path)
    resolver = TenantPathResolver(tmp_path, multi_tenant=True)
    registry.set_path_resolver(resolver)

    ctx_a = ToolExecutionContext(
        request_id="r1",
        account_id="a",
        tenant_id="tenant:a",
        session_id="chat:tenant:a",
        turn_id="t1",
        channel="chat",
        chat_id="tenant:a",
        principal_type="user",
    )
    ctx_b = ToolExecutionContext(
        request_id="r2",
        account_id="b",
        tenant_id="tenant:b",
        session_id="chat:tenant:b",
        turn_id="t2",
        channel="chat",
        chat_id="tenant:b",
        principal_type="user",
    )
    _ = await registry.execute(
        "write_file",
        {"path": "notes/secret.md", "content": "A 的秘密"},
        context=ctx_a,
    )
    written = list((tmp_path / "tenants").glob("*/workspace/notes/secret.md"))
    assert len(written) == 1

    read_b = await registry.execute("read_file", {"path": "notes/secret.md"}, context=ctx_b)
    assert "错误" in str(read_b)  # B 的 root 下不存在该文件
    assert "A 的秘密" not in str(read_b)


def _unused(*args: Any, **kwargs: Any) -> None:
    return None
