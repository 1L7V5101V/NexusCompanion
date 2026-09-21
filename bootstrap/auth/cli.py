"""C5 ``pilot-admin`` CLI（§5.9.3 / §10 DECIDED Admin bootstrap）。

命令面冻结：
``bootstrap | status | rotate-recovery-token [--force-local] | revoke-sessions
--all | disable | enable``

安全属性（service 层已内置，本层只补 CLI 门禁与回显）：
- 明文（recovery token）只在交互式 TTY 回显一次：禁止经过 argv/env/config/
  stdin 重定向（``bootstrap`` 与 ``--force-local`` 均要求 ``isatty()``，且不读取
  重定向的 stdin）。默认轮换路径经 ``getpass`` 读取当前 token（不回显）。
- 每次写操作产生 admin audit（service 层），本层不写日志。
- 命令共享一个 AuthRuntime（``config.storage.postgres_url`` 控制面库），结束时
  ``aclose``。

测试接线：``print_fn`` / ``getpass_fn`` 可注入（tests/auth_provisioning/test_cli_layer.py
用 fake runtime 断言回显次数、TTY 门禁与命令面）。
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path
from typing import Callable

from agent.config_models import Config

from bootstrap.auth import create_auth_runtime
from bootstrap.auth.runtime import AuthRuntime
from bootstrap.db.repository.auth_repo import (
    AdminBootstrapError,
    CredentialExchangeError,
)

__all__ = ["run_pilot_admin"]


def run_pilot_admin(
    args: list[str],
    *,
    config_path: str = "config.toml",
    workspace: Path,
    print_fn: Callable[[str], None] = print,
    getpass_fn: Callable[[str], str] = getpass.getpass,
) -> int:
    """执行一个 ``pilot-admin`` 子命令；返回进程退出码（0=成功，2=用法错误）。"""
    if not args:
        _usage(print_fn)
        return 2
    cmd, rest = args[0], args[1:]

    if cmd == "rotate-recovery-token":
        force_local = "--force-local" in rest
        if any(a != "--force-local" for a in rest):
            print_fn("rotate-recovery-token 只接受 --force-local")
            return 2
        return _run(
            workspace,
            _rotate,
            config_path=config_path,
            force_local=force_local,
            print_fn=print_fn,
            getpass_fn=getpass_fn,
        )
    if cmd == "revoke-sessions":
        if rest != ["--all"]:
            print_fn("revoke-sessions 只接受 --all")
            return 2
        return _run(
            workspace, _revoke_sessions, config_path=config_path, print_fn=print_fn
        )
    if cmd in ("bootstrap", "status", "disable", "enable"):
        if rest:
            print_fn(f"{cmd} 不接受额外参数")
            return 2
        return _run(
            workspace, _dispatch_simple, config_path=config_path, cmd=cmd, print_fn=print_fn
        )
    if cmd in ("-h", "--help"):
        _usage(print_fn)
        return 0
    print_fn(f"未知命令: {cmd!r}")
    _usage(print_fn)
    return 2


def _usage(print_fn: Callable[[str], None]) -> None:
    print_fn("pilot-admin 子命令：")
    print_fn("  bootstrap                       首次初始化 admin（仅受信主机 TTY）")
    print_fn("  status                          查看 admin 状态摘要")
    print_fn("  rotate-recovery-token           轮换 recovery token（交互输入当前值）")
    print_fn("  rotate-recovery-token --force-local 受信主机强制轮换（丢失后恢复）")
    print_fn("  revoke-sessions --all           撤销全部 admin 浏览器会话")
    print_fn("  disable / enable                关闭 / 重开 admin 网页登录")


# ── 子命令实现（统一接收 runtime）───────────────────────────────


async def _dispatch_simple(
    runtime: AuthRuntime, *, cmd: str, print_fn: Callable[[str], None]
) -> int:
    if cmd == "bootstrap":
        return await _bootstrap(runtime, print_fn)
    if cmd == "status":
        return await _status(runtime, print_fn)
    if cmd == "disable":
        try:
            await runtime.admin.disable()
        except AdminBootstrapError as exc:
            print_fn(str(exc))
            return 1
        print_fn("admin 网页登录已关闭（现有 admin 会话已全部撤销）")
        return 0
    if cmd == "enable":
        try:
            await runtime.admin.enable()
        except AdminBootstrapError as exc:
            print_fn(str(exc))
            return 1
        print_fn("admin 网页登录已启用")
        return 0
    return 2


async def _bootstrap(
    runtime: AuthRuntime, print_fn: Callable[[str], None]
) -> int:
    """bootstrap：仅受信主机交互式 TTY（§5.9.3）；明文只回显一次。"""
    if not sys.stdin.isatty():
        print_fn("bootstrap 必须在受信主机的交互式 TTY 中执行")
        return 1
    try:
        raw = await runtime.admin.bootstrap()
    except AdminBootstrapError as exc:
        print_fn(str(exc))
        return 1
    print_fn("admin principal 已初始化。恢复令牌（只显示一次，请立即保存）：")
    print_fn(raw)
    print_fn("浏览器兑换请使用 `POST /api/admin/auth/exchange`。")
    return 0


async def _status(
    runtime: AuthRuntime, print_fn: Callable[[str], None]
) -> int:
    """status：只显示非敏感摘要（无 digest/token/CSRF secret，§5.9.3）。"""
    info = await runtime.admin.status()
    if not info.get("bootstrapped"):
        print_fn("admin principal 尚未 bootstrap（请先运行 bootstrap）")
        return 1
    enabled = "enabled" if info.get("enabled") else "disabled"
    print_fn(f"admin: {enabled}")
    print_fn(f"revision: {info.get('revision', '')}")
    print_fn(f"last_rotated_at: {info.get('rotated_at') or '-'}")
    print_fn(f"active_sessions: {info.get('active_sessions', 0)}")
    return 0


async def _rotate(
    runtime: AuthRuntime,
    *,
    force_local: bool,
    print_fn: Callable[[str], None],
    getpass_fn: Callable[[str], str],
) -> int:
    if force_local:
        # 丢失凭据后的受信主机强制恢复：不校验当前 token，仅要求 TTY。
        if not sys.stdin.isatty():
            print_fn("--force-local 必须在受信主机的交互式 TTY 中执行")
            return 1
        current = None
    else:
        current = getpass_fn("当前 recovery token: ").strip()
        if not current:
            print_fn("需要交互式输入当前 recovery token")
            return 1
    try:
        raw = await runtime.admin.rotate_recovery_token(
            current, force_local=force_local
        )
    except AdminBootstrapError as exc:
        print_fn(str(exc))
        return 1
    except CredentialExchangeError:
        print_fn("recovery token 校验失败")
        return 1
    print_fn("recovery token 已轮换。新令牌（只显示一次，请立即保存）：")
    print_fn(raw)
    print_fn("现有 admin 浏览器会话保持有效（轮换与 session 撤销为独立操作）。")
    return 0


async def _revoke_sessions(
    runtime: AuthRuntime, print_fn: Callable[[str], None]
) -> int:
    count = await runtime.admin.revoke_sessions_all()
    print_fn(f"已撤销全部 admin 浏览器会话（{count} 个）。recovery token 不变。")
    return 0


# ── 运行与装配 ─────────────────────────────────────────────────


def _run(workspace: Path, coro, *, config_path: str, **kwargs) -> int:
    """建一次性 AuthRuntime 执行子命令并关闭（TTY 门禁在半组实现内）。"""
    runtime = create_auth_runtime(
        config=Config.load(config_path), workspace=workspace
    )

    async def _go() -> int:
        try:
            return await coro(runtime, **kwargs)
        finally:
            await runtime.aclose()

    return asyncio.run(_go())