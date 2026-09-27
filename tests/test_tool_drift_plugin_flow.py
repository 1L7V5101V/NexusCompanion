"""C7 task 7.2：drift/插件工具走同一 context/effect 校验（design ADR-8）。

- drift 文件工具逃逸面（绝对路径 / ``..`` / 符号链接越界全拒，结构化错误）；
- drift 工具调用带同一 ToolExecutionContext（dev 回退身份 + tenant 派生）；
- 插件工具与普通工具同一 registry/catalog 源：effect 闸门一致。
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent.tools.base import Tool, ToolEffect
from agent.tools.context import ToolExecutionContext
from agent.tools.registry import ToolRegistry
from plugins.default_proactive.context import AgentTickContext
from plugins.drift_flow.runtime import _drift_tool_context
from plugins.drift_flow.tools import DriftListDirTool, DriftPathResolver, DriftReadFileTool
from plugins.drift_flow.state import DriftStateStore


def _store(tmp_path: Path) -> DriftStateStore:
    return DriftStateStore(tmp_path / "drift_state.json")


def _ctx(*, session_key: str = "telegram:42") -> AgentTickContext:
    return AgentTickContext(
        session_key=session_key,
        now_utc=datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc),
    )


# ── drift 路径逃逸面 ─────────────────────────────────────────────


def _escape_cases(drift_dir: Path) -> list[tuple[str, str]]:
    """(用例名, 路径) —— 全部应被拒绝。"""
    outside = drift_dir.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    cases = [
        ("dotdot", f"{os.path.relpath(outside, drift_dir)}"),  # ../secret.txt
        ("absolute_outside", str(outside)),
        (
            "absolute_system",
            str(Path(outside).parent / "system.ini"),
        ),
        ("dotdot_deep", "a/../../../secret.txt"),
    ]
    return cases


def test_drift_resolver_rejects_escapes(tmp_path: Path):
    drift_dir = tmp_path / "drift"
    drift_dir.mkdir()
    resolver = DriftPathResolver(drift_dir, _store(tmp_path))

    for name, path in _escape_cases(drift_dir):
        assert resolver.resolve(path) is None, name

    (drift_dir / "notes.md").write_text("x", encoding="utf-8")
    ok = resolver.resolve("notes.md")
    assert ok is not None
    assert ok.resolve() == (drift_dir / "notes.md").resolve()


def test_drift_resolver_rejects_symlink_escape(tmp_path: Path):
    if not hasattr(os, "symlink"):
        pytest.skip("平台不支持 symlink")
    drift_dir = tmp_path / "drift"
    drift_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (drift_dir / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink 创建失败")
    resolver = DriftPathResolver(drift_dir, _store(tmp_path))
    assert resolver.resolve("link/secret.txt") is None
    assert resolver.resolve(os.path.join("link", "..", "x")) is None


def test_drift_resolver_skills_scoped(tmp_path: Path):
    drift_dir = tmp_path / "drift"
    drift_dir.mkdir()
    store = _store(tmp_path)
    skill_root = store.skills_dir / "demo"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text("# demo", encoding="utf-8")
    (skill_root / "notes.md").write_text("skill note", encoding="utf-8")

    resolver = DriftPathResolver(drift_dir, store)
    ok = resolver.resolve("skills/demo/notes.md")
    assert ok is not None
    assert ok.resolve() == (skill_root / "notes.md").resolve()
    # skills 段本身不允许逃逸（.. 拒绝）。
    assert resolver.resolve("skills/demo/../secret") is None
    assert resolver.resolve("skills/not_exists/x.md") is None


# ── drift 文件工具拒绝越界 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_drift_read_file_rejects_escape(tmp_path: Path):
    drift_dir = tmp_path / "drift"
    drift_dir.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("topsecret", encoding="utf-8")

    tool = DriftReadFileTool(DriftPathResolver(drift_dir, _store(tmp_path)))
    result = await tool.execute(path=str(outside))
    assert "drift_path_forbidden" in str(result)
    assert "topsecret" not in str(result)

    (drift_dir / "ok.md").write_text("hello", encoding="utf-8")
    ok = await tool.execute(path="ok.md")
    assert "hello" in str(ok)


@pytest.mark.asyncio
async def test_drift_list_dir_rejects_escape(tmp_path: Path):
    drift_dir = tmp_path / "drift"
    drift_dir.mkdir()
    tool = DriftListDirTool(DriftPathResolver(drift_dir, _store(tmp_path)))
    result = await tool.execute(path="../")
    assert "drift_path_forbidden" in str(result)


# ── drift 工具调用带同一 ToolExecutionContext ────────────────────


def test_drift_tool_context_dev_identity_and_tenant():
    ctx = _drift_tool_context(_ctx(session_key="telegram:42"))
    assert ctx.principal_type == "dev"
    assert ctx.tenant_id == "telegram:42"
    assert ctx.session_id == "telegram:42"
    assert ctx.channel == "telegram"
    assert ctx.chat_id == "42"
    assert ctx.request_id.startswith("drift-")


def test_drift_tool_context_empty_session_key_falls_back_dev():
    ctx = _drift_tool_context(_ctx(session_key=""))
    assert ctx.tenant_id == "dev"
    assert ctx.channel == ""


# ── 插件工具与普通工具同一 registry/effect 源（ADR-8） ────────────


class _PluginTool(Tool):
    name = "plugin_tool"
    description = "plugin-declared tool"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return "plugin ran"


def _user_ctx(tenant: str = "tenant:a") -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="req-1",
        account_id="acct-1",
        tenant_id=tenant,
        session_id="chat:tenant:a",
        turn_id="turn-1",
        channel="chat",
        chat_id=tenant,
        principal_type="user",
    )


@pytest.mark.asyncio
async def test_plugin_tool_gated_by_same_effect_policy():
    """插件声明工具 = 普通工具同一 registry/effect 源：process-exec 对普通租户拒绝。"""
    registry = ToolRegistry()
    registry.register(
        _PluginTool(),
        source_type="plugin",
        effect=ToolEffect.PROCESS_EXEC,
    )
    result = await registry.execute("plugin_tool", {}, context=_user_ctx())
    assert "tool_denied_effect" in str(result)
    # 同一 registry：白名单外工具同样经 schema 过滤层（task 2.3 既有契约）。


@pytest.mark.asyncio
async def test_plugin_tool_read_only_allowed_for_user():
    registry = ToolRegistry()
    registry.register(_PluginTool(), source_type="plugin", effect=ToolEffect.READ_ONLY)
    result = await registry.execute("plugin_tool", {}, context=_user_ctx())
    assert isinstance(result, str) and "plugin ran" in result