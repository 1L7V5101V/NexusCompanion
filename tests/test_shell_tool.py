import json
from pathlib import Path

import pytest

from agent.tools.shell import ShellTool


class _FakeProc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self) -> None:
        return None


@pytest.mark.asyncio
async def test_shell_tool_runs_directly_by_default(monkeypatch):
    observed: dict[str, object] = {}

    async def _fake_create_subprocess_shell(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return _FakeProc(stdout="ok")

    monkeypatch.setattr(
        "agent.tools.shell.asyncio.create_subprocess_shell",
        _fake_create_subprocess_shell,
    )

    tool = ShellTool()
    result = json.loads(await tool.execute(command="printf ok"))

    assert observed["command"] == "printf ok"
    assert result["exit_code"] == 0
    assert result["output"] == "ok"


@pytest.mark.asyncio
async def test_shell_tool_uses_configured_working_dir(monkeypatch, tmp_path: Path):
    observed: dict[str, object] = {}

    async def _fake_create_subprocess_shell(command, **kwargs):
        observed["kwargs"] = kwargs
        return _FakeProc(stdout="ok")

    monkeypatch.setattr(
        "agent.tools.shell.asyncio.create_subprocess_shell",
        _fake_create_subprocess_shell,
    )

    tool = ShellTool(working_dir=tmp_path, restricted_dir=tmp_path)
    await tool.execute(command="ls", description="列目录")

    assert observed["kwargs"]["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_restricted_shell_blocks_network_and_outside_paths(tmp_path: Path):
    tool = ShellTool(
        allow_network=False,
        working_dir=tmp_path,
        restricted_dir=tmp_path,
    )

    network_result = json.loads(
        await tool.execute(command="curl https://example.com", description="联网")
    )
    outside_result = json.loads(
        await tool.execute(command="cp a ../b", description="越界")
    )

    assert "禁止网络访问" in network_result["error"]
    assert "父级路径" in outside_result["error"]
