from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agent.tool_hooks import ShellRmToRestoreHook, ToolExecutionRequest, ToolExecutor


async def _invoke(tool_name: str, arguments: dict[str, Any]) -> Any:
    return {"tool": tool_name, "arguments": dict(arguments)}


def test_shell_rm_hook_rewrites_rm_and_creates_restore_dir(tmp_path: Path) -> None:
    restore_dir = tmp_path / "restore"
    executor = ToolExecutor([ShellRmToRestoreHook(restore_dir=restore_dir)])

    result = asyncio.run(
        executor.execute(
            ToolExecutionRequest(
                call_id="c1",
                tool_name="shell",
                arguments={
                    "command": "rm -rf foo bar",
                    "description": "删除文件",
                },
                source="passive",
            ),
            _invoke,
        )
    )

    assert result.status == "success"
    assert restore_dir.is_dir()
    assert result.final_arguments["command"] == f"mv -- foo bar {restore_dir}"
    assert result.output["arguments"]["command"] == f"mv -- foo bar {restore_dir}"


def test_shell_rm_hook_rewrites_sudo_rm(tmp_path: Path) -> None:
    restore_dir = tmp_path / "restore"
    executor = ToolExecutor([ShellRmToRestoreHook(restore_dir=restore_dir)])

    result = asyncio.run(
        executor.execute(
            ToolExecutionRequest(
                call_id="c1",
                tool_name="shell",
                arguments={
                    "command": "sudo rm -f /tmp/a",
                    "description": "删除文件",
                },
                source="passive",
            ),
            _invoke,
        )
    )

    assert result.status == "success"
    assert result.final_arguments["command"] == f"sudo mv -- /tmp/a {restore_dir}"


def test_shell_rm_hook_skips_non_rm_command(tmp_path: Path) -> None:
    restore_dir = tmp_path / "restore"
    executor = ToolExecutor([ShellRmToRestoreHook(restore_dir=restore_dir)])

    result = asyncio.run(
        executor.execute(
            ToolExecutionRequest(
                call_id="c1",
                tool_name="shell",
                arguments={
                    "command": "ls -la",
                    "description": "列目录",
                },
                source="passive",
            ),
            _invoke,
        )
    )

    assert result.status == "success"
    assert not restore_dir.exists()
    assert result.final_arguments["command"] == "ls -la"
