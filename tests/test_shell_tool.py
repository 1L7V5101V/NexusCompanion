import json

import pytest

from agent.config import load_config
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
async def test_shell_tool_runs_via_sudo_when_user_configured(monkeypatch):
    observed: dict[str, object] = {}

    async def _fake_create_subprocess_exec(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return _FakeProc(stdout="nexus")

    monkeypatch.setattr(
        "agent.tools.shell.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    tool = ShellTool(run_as_user="nexus")
    result = json.loads(await tool.execute(command="whoami"))

    assert observed["args"] == (
        "/usr/bin/sudo",
        "-n",
        "-H",
        "-u",
        "nexus",
        "--",
        "/bin/bash",
        "-lc",
        "whoami",
    )
    assert result["exit_code"] == 0
    assert result["output"] == "nexus"


@pytest.mark.asyncio
async def test_shell_tool_reads_run_as_user_from_env(monkeypatch):
    observed: dict[str, object] = {}

    async def _fake_create_subprocess_exec(*args, **kwargs):
        observed["args"] = args
        return _FakeProc(stdout="nexus")

    monkeypatch.setenv("AKASIC_SHELL_RUN_AS_USER", "nexus")
    monkeypatch.setattr(
        "agent.tools.shell.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    tool = ShellTool()
    await tool.execute(command="whoami")

    assert observed["args"][4] == "nexus"


def test_config_loader_parses_shell_run_as_user(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "provider": "openai",
                "model": "test-model",
                "api_key": "test-key",
                "shell": {"run_as_user": "nexus"},
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_file))

    assert cfg.shell.run_as_user == "nexus"
