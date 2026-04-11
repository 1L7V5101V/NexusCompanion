import asyncio
import json
import signal
from types import SimpleNamespace
from pathlib import Path

import pytest

from agent.tools.shell import ShellTool, _MAX_OUTPUT, _run


class _FakeProc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()
        self.returncode = returncode
        self.pid = 4321
        self.stdout = SimpleNamespace(read=self._read_stdout)
        self.stderr = SimpleNamespace(read=self._read_stderr)

    async def communicate(self):
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode

    async def _read_stdout(self, _size: int = -1):
        data = self._stdout
        self._stdout = b""
        return data

    async def _read_stderr(self, _size: int = -1):
        data = self._stderr
        self._stderr = b""
        return data

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
    result = json.loads(await tool.execute(command="printf ok", description="输出 ok"))

    assert observed["command"] == "printf ok"
    assert result["exit_code"] == 0
    assert result["output"] == "ok"
    assert "stdout" not in result
    assert "stderr" not in result
    assert result["truncation"] is None
    assert result["full_output_path"] is None


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
async def test_shell_tool_supports_spawn_hook_and_streaming(monkeypatch, tmp_path: Path):
    observed: dict[str, object] = {}
    streamed: list[str] = []

    async def _fake_create_subprocess_shell(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return _FakeProc(stdout="part1", stderr="part2", returncode=0)

    monkeypatch.setattr(
        "agent.tools.shell.asyncio.create_subprocess_shell",
        _fake_create_subprocess_shell,
    )

    def _hook(ctx):
        return {
            **ctx,
            "command": "printf hooked",
            "cwd": str(tmp_path),
            "env": {"TEST_FLAG": "1"},
        }

    tool = ShellTool(spawn_hook=_hook)
    result = json.loads(
        await tool.execute(
            command="printf raw",
            description="测试 hook",
            _on_data=streamed.append,
        )
    )

    assert observed["command"] == "printf hooked"
    assert observed["kwargs"]["cwd"] == str(tmp_path)
    assert observed["kwargs"]["env"]["TEST_FLAG"] == "1"
    assert streamed == ["part1", "part2"]
    assert result["output"] == "part1\n\npart2"
    assert "stdout" not in result
    assert "stderr" not in result


@pytest.mark.asyncio
async def test_restricted_shell_spawn_hook_cannot_escape_restricted_dir(tmp_path: Path):
    outside = tmp_path.parent

    def _hook(ctx):
        return {**ctx, "cwd": str(outside)}

    tool = ShellTool(
        working_dir=tmp_path,
        restricted_dir=tmp_path,
        spawn_hook=_hook,
    )
    result = json.loads(await tool.execute(command="ls .", description="越界 cwd"))

    assert "任务目录外" in result["error"]


@pytest.mark.asyncio
async def test_restricted_shell_spawn_hook_empty_cwd_falls_back_to_restricted_dir(
    monkeypatch, tmp_path: Path
):
    observed: dict[str, object] = {}

    async def _fake_create_subprocess_shell(command, **kwargs):
        observed["kwargs"] = kwargs
        return _FakeProc(stdout="ok")

    monkeypatch.setattr(
        "agent.tools.shell.asyncio.create_subprocess_shell",
        _fake_create_subprocess_shell,
    )

    def _hook(ctx):
        return {**ctx, "cwd": None}

    tool = ShellTool(
        working_dir=tmp_path,
        restricted_dir=tmp_path,
        spawn_hook=_hook,
    )
    result = json.loads(await tool.execute(command="ls .", description="清空 cwd"))

    assert result["exit_code"] == 0
    assert observed["kwargs"]["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_shell_tool_truncates_to_tail_and_persists_full_output(monkeypatch, tmp_path: Path):
    long_stdout = "HEAD\n" + ("x" * 31_000) + "\nTAIL\n"

    async def _fake_run(command: str, timeout: int, cwd=None, env=None, on_data=None):
        return long_stdout, "", 0, False

    monkeypatch.setattr("agent.tools.shell._run", _fake_run)
    monkeypatch.setattr("agent.tools.shell.tempfile.gettempdir", lambda: str(tmp_path))

    tool = ShellTool()
    result = json.loads(await tool.execute(command="echo long", description="长输出"))

    assert result["truncation"] is not None
    assert result["full_output_path"] is not None
    assert Path(result["full_output_path"]).read_text(encoding="utf-8") == long_stdout
    assert "HEAD" not in result["output"]
    assert "TAIL" in result["output"]
    assert result["truncation"]["strategy"] == "tail"
    assert result["truncation"]["full_length"] == len(long_stdout)
    assert len(result["output"]) <= _MAX_OUTPUT


@pytest.mark.asyncio
async def test_run_streams_stdout_and_stderr(monkeypatch):
    proc = _FakeProc(stdout="hello", stderr="world", returncode=0)

    async def _fake_create_subprocess_shell(command, **kwargs):
        return proc

    monkeypatch.setattr(
        "agent.tools.shell.asyncio.create_subprocess_shell",
        _fake_create_subprocess_shell,
    )

    chunks: list[str] = []
    stdout, stderr, exit_code, interrupted = await _run(
        "echo hi",
        5,
        on_data=chunks.append,
    )

    assert stdout == "hello"
    assert stderr == "world"
    assert exit_code == 0
    assert interrupted is False
    assert chunks == ["hello", "world"]


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


@pytest.mark.asyncio
async def test_shell_tool_cancel_kills_process_group(monkeypatch):
    proc = _FakeProc(stdout="", stderr="")
    observed: dict[str, object] = {}

    async def _fake_create_subprocess_shell(command, **kwargs):
        observed["kwargs"] = kwargs
        return proc

    async def _fake_wait_for(awaitable, timeout):
        coro = awaitable
        coro.close()
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "agent.tools.shell.asyncio.create_subprocess_shell",
        _fake_create_subprocess_shell,
    )
    monkeypatch.setattr("agent.tools.shell.asyncio.wait_for", _fake_wait_for)
    killpg_mock = []

    def _fake_killpg(pid, sig):
        killpg_mock.append((pid, sig))

    monkeypatch.setattr("agent.tools.shell.os.killpg", _fake_killpg)

    with pytest.raises(asyncio.CancelledError):
        await __import__("agent.tools.shell", fromlist=["_run"])._run("sleep 10", 5)

    assert observed["kwargs"]["start_new_session"] is True
    assert killpg_mock == [(proc.pid, signal.SIGKILL)]
