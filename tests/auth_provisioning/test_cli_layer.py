"""pilot-admin CLI 层单元测试（TTY 门禁、命令面、明文回显一次、不进 argv/env）。

不依赖 PG：通过 monkeypatch 注入 fake AuthRuntime（admin 方法与 aclose 均为
内存 stub），验证 dispatch 逻辑、退出码与 TTY 门禁；明文只出现在 print_fn
输出中、绝不出现在 argv。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

import bootstrap.auth.cli as cli_mod


@dataclass
class _FakeAdmin:
    stopped: list[str] = field(default_factory=list)
    rotated: list[tuple[str | None, bool]] = field(default_factory=list)

    async def bootstrap(self) -> str:
        return "nad_fake_bootstrap_token"

    async def status(self) -> dict:
        return {"bootstrapped": True, "enabled": True, "revision": 3, "rotated_at": "2026-09-01T00:00:00Z", "active_sessions": 2}

    async def rotate_recovery_token(self, current: str | None, *, force_local: bool) -> str:
        self.rotated.append((current, force_local))
        return "nad_fake_rotated_token"

    async def revoke_sessions_all(self) -> int:
        return 5

    async def disable(self) -> None:
        self.stopped.append("disable")

    async def enable(self) -> None:
        self.stopped.append("enable")


@dataclass
class _FakeRuntime:
    admin: _FakeAdmin = field(default_factory=_FakeAdmin)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_runtime(monkeypatch) -> _FakeRuntime:
    rt = _FakeRuntime()
    # 拒绝真实 DB 路径：Config.load 与 create_auth_runtime 都被替换。
    monkeypatch.setattr(cli_mod.Config, "load", classmethod(lambda cls, p="": object()))
    monkeypatch.setattr(
        cli_mod,
        "create_auth_runtime",
        lambda config, workspace=None: rt,
    )
    return rt


def _run(args: list[str], *, tty: bool = True, monkeypatch=None, current_token: str = "current-token"):
    printed: list[str] = []
    getpass_calls: list[str] = []

    def _getpass(prompt: str) -> str:
        getpass_calls.append(prompt)
        return current_token

    if monkeypatch is not None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: tty)

    code = cli_mod.run_pilot_admin(
        args,
        config_path="config.toml",
        workspace=Path("."),
        print_fn=printed.append,
        getpass_fn=_getpass,
    )
    return code, printed, getpass_calls


def test_unknown_command_usage(fake_runtime, monkeypatch) -> None:
    code, printed, _ = _run(["nonsense"], monkeypatch=monkeypatch)
    assert code == 2
    assert any("未知命令" in line for line in printed)
    assert "nonsense" not in [line for line in printed if line.startswith("python")]


def test_rotate_force_local_reads_tty_and_rotates(fake_runtime, monkeypatch) -> None:
    code, printed, getpass_calls = _run(
        ["rotate-recovery-token", "--force-local"], tty=True, monkeypatch=monkeypatch
    )
    assert code == 0
    assert fake_runtime.admin.rotated == [(None, True)]
    assert getpass_calls == []
    # 明文只出现一次（回显行唯一）。
    echo_lines = [line for line in printed if line == "nad_fake_rotated_token"]
    assert len(echo_lines) == 1


def test_rotate_force_local_non_tty_rejected(fake_runtime, monkeypatch) -> None:
    code, printed, _ = _run(
        ["rotate-recovery-token", "--force-local"], tty=False, monkeypatch=monkeypatch
    )
    assert code == 1
    assert fake_runtime.admin.rotated == []
    assert any("交互式 TTY" in line for line in printed)


def test_rotate_interactive_requires_current_token(fake_runtime, monkeypatch) -> None:
    code, printed, getpass_calls = _run(
        ["rotate-recovery-token"], tty=True, monkeypatch=monkeypatch, current_token="xyz"
    )
    assert code == 0
    assert fake_runtime.admin.rotated == [("xyz", False)]
    assert len(getpass_calls) == 1
    echo_lines = [line for line in printed if line == "nad_fake_rotated_token"]
    assert len(echo_lines) == 1


def test_rotate_extra_flag_rejected(fake_runtime, monkeypatch) -> None:
    code, printed, _ = _run(["rotate-recovery-token", "--force-local", "--extra"], monkeypatch=monkeypatch)
    assert code == 2
    assert fake_runtime.admin.rotated == []


def test_bootstrap_requires_tty_and_echoes_once(fake_runtime, monkeypatch) -> None:
    code, printed, _ = _run(["bootstrap"], tty=True, monkeypatch=monkeypatch)
    assert code == 0
    echo_lines = [line for line in printed if line == "nad_fake_bootstrap_token"]
    assert len(echo_lines) == 1


def test_bootstrap_non_tty_rejected(fake_runtime, monkeypatch) -> None:
    code, printed, _ = _run(["bootstrap"], tty=False, monkeypatch=monkeypatch)
    assert code == 1
    assert any("交互式 TTY" in line for line in printed)


def test_status_prints_non_secret_summary(fake_runtime, monkeypatch) -> None:
    code, printed, _ = _run(["status"], monkeypatch=monkeypatch)
    assert code == 0
    assert any(line.startswith("admin: enabled") for line in printed)
    assert any("revision: 3" in line for line in printed)
    assert any("active_sessions: 2" in line for line in printed)
    # 摘要不输出 digest / token 明文。
    assert "nad_" not in "\n".join(printed)


def test_revoke_sessions_requires_all(fake_runtime, monkeypatch) -> None:
    code, printed, _ = _run(["revoke-sessions"], monkeypatch=monkeypatch)
    assert code == 2
    assert any("--all" in line for line in printed)
    code, printed, _ = _run(["revoke-sessions", "--all"], monkeypatch=monkeypatch)
    assert code == 0
    assert any("5 个" in line for line in printed)


def test_disable_enable(fake_runtime, monkeypatch) -> None:
    code, _, _ = _run(["disable"], monkeypatch=monkeypatch)
    assert code == 0
    assert fake_runtime.admin.stopped == ["disable"]
    code, _, _ = _run(["enable"], monkeypatch=monkeypatch)
    assert code == 0
    assert fake_runtime.admin.stopped == ["disable", "enable"]