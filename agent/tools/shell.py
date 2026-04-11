"""
Shell 工具（Bash 命令执行）
设计参考 OpenCode internal/llm/tools/bash.go：
- 禁止高风险命令黑名单（nc、telnet、浏览器等）
- 超时：默认 60s，最大 600s（10 分钟）
- 输出截断：超过 30000 字符时首尾各取一半，中间注明省略行数
- 记录执行时长
- 结构化 JSON 输出（command / exit_code / duration_ms / output）
"""

import asyncio
import json
import logging
import os
import signal
import shlex
import ipaddress
import tempfile
from pathlib import Path
from urllib.parse import urlparse
import time
from typing import Any, Callable

from agent.tools.base import Tool

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60  # 秒（OpenCode 默认 1 分钟）
_MAX_TIMEOUT = 600  # 秒（OpenCode 最大 10 分钟）
_MAX_OUTPUT = 30_000  # 字符（与 OpenCode MaxOutputLength 一致）
_STREAM_CHUNK_SIZE = 4096

# 禁止命令（对应 OpenCode bannedCommands）
_BANNED = frozenset(
    {
        "curlie",
        "axel",
        "aria2c",
        "nc",
        "telnet",
        "lynx",
        "w3m",
        "links",
        "http-prompt",
        "chrome",
        "firefox",
        "safari",
    }
)

# 对网络命令启用额外安全限制
_NETWORK_CMDS = frozenset({"curl", "wget", "http", "httpie", "xh"})
_NET_WRITE_FLAGS = frozenset(
    {
        # curl
        "-o",
        "--output",
        "-O",
        "--remote-name",
        "-T",
        "--upload-file",
        "-F",
        "--form",
        "--form-string",
        # wget
        "-O",
        "--output-document",
        "--post-file",
        # httpie/xh
        "--download",
        "--output",
        "--offline",
        "@",
    }
)
_RESTRICTED_META_CHARS = ("|", ";", "&", ">", "<", "`", "$(")
_RESTRICTED_SHELL_RUNNERS = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "fish",
        "python",
        "python3",
        "node",
        "perl",
        "ruby",
        "php",
        "lua",
    }
)


class ShellTool(Tool):
    """在 bash 中执行命令，返回结构化结果"""

    name = "shell"

    def __init__(
        self,
        *,
        allow_network: bool = True,
        working_dir: Path | None = None,
        restricted_dir: Path | None = None,
        spawn_hook: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._allow_network = allow_network
        self._working_dir = working_dir
        self._restricted_dir = restricted_dir.resolve() if restricted_dir else None
        self._spawn_hook = spawn_hook

    @property
    def description(self) -> str:
        return (
            "在 bash 中执行命令并返回输出。\n"
            "注意：\n"
            "- 使用绝对路径，避免依赖 cd 切换目录\n"
            "- 多条命令用 ; 或 && 连接，不要用换行分隔\n"
            "- 网络命令（curl/wget/httpie/xh）仅允许访问公网 HTTP(S)，且禁止上传/写文件\n"
            "- 以下命令被禁止：nc、telnet、浏览器等高风险工具\n"
            "- 输出超过 30000 字符时自动截断\n"
            "- 超时默认 60 秒，最大 600 秒\n"
            "- 若命令是服务进程（如 python server.py、uvicorn、node app.js 等），必须用 `timeout 5 <命令> 2>&1` 包裹以快速获取启动日志，禁止直接运行导致阻塞\n"
            "禁止用途：不得用 shell 替代专用工具（read_file 读文件、web_fetch 抓网页、list_dir 列目录）。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 bash 命令",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "用 5-10 字描述这条命令的作用，便于用户审查和日志追踪。"
                        "示例：'列出当前目录文件' / '安装 Python 依赖' / '查看进程状态'"
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": f"超时秒数，默认 {_DEFAULT_TIMEOUT}，最大 {_MAX_TIMEOUT}",
                    "minimum": 1,
                    "maximum": _MAX_TIMEOUT,
                },
            },
            "required": ["command", "description"],
        }

    async def execute(self, **kwargs: Any) -> str:
        command: str = kwargs.get("command", "").strip()
        description: str = kwargs.get("description", "")
        timeout: int = min(int(kwargs.get("timeout", _DEFAULT_TIMEOUT)), _MAX_TIMEOUT)
        on_data = kwargs.get("_on_data")

        if not command:
            return _err("命令不能为空")
        cwd = self._working_dir
        env = os.environ.copy()
        if self._spawn_hook is not None:
            hooked = self._spawn_hook(
                {
                    "command": command,
                    "cwd": str(cwd) if cwd is not None else None,
                    "env": env,
                }
            )
            command = str(hooked.get("command", command)).strip()
            cwd_val = hooked.get("cwd")
            cwd = None if cwd_val in (None, "") else Path(str(cwd_val))
            env_val = hooked.get("env")
            if isinstance(env_val, dict):
                env = {str(k): str(v) for k, v in env_val.items()}

        if self._restricted_dir is not None and cwd is None:
            cwd = self._restricted_dir

        logger.info("shell [%s]: %s", description, command[:120])

        # 禁止命令检查（对应 OpenCode bannedCommands 逻辑）
        base_cmd = command.split()[0].lower()
        if base_cmd in _BANNED:
            return _err(f"命令 '{base_cmd}' 不被允许（安全限制）")
        cmd_err = _validate_command(
            command,
            allow_network=self._allow_network,
            restricted_dir=self._restricted_dir,
            cwd=cwd,
        )
        if cmd_err:
            return _err(cmd_err)

        start_ms = int(time.monotonic() * 1000)
        stdout, stderr, exit_code, interrupted = await _run(
            command,
            timeout,
            cwd=cwd,
            env=env,
            on_data=on_data if callable(on_data) else None,
        )
        duration_ms = int(time.monotonic() * 1000) - start_ms

        # 合并输出（对应 OpenCode hasBothOutputs 逻辑）
        full_parts = []
        if stdout:
            full_parts.append(stdout)
        if stderr:
            if stdout:
                full_parts.append("")  # 两段之间空一行
            full_parts.append(stderr)
        if interrupted:
            full_parts.append("命令在完成前被中止")
        elif exit_code != 0:
            full_parts.append(f"Exit code {exit_code}")

        full_output = "\n".join(full_parts) if full_parts else "（无输出）"
        output_meta = _truncate(full_output)
        full_output_path = (
            _write_full_output(full_output) if output_meta["truncated"] else None
        )
        truncation = None
        if output_meta["truncated"]:
            truncation = {
                "strategy": output_meta["strategy"],
                "full_length": output_meta["full_length"],
                "returned_length": output_meta["returned_length"],
                "omitted_lines": output_meta["omitted_lines"],
            }

        return json.dumps(
            {
                "command": command,
                "exit_code": exit_code,
                "interrupted": interrupted,
                "duration_ms": duration_ms,
                "output": output_meta["text"],
                "truncation": truncation,
                "full_output_path": full_output_path,
            },
            ensure_ascii=False,
        )


# ── 模块级工具函数 ────────────────────────────────────────────────


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


async def _run(
    command: str,
    timeout: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    on_data: Callable[[str], None] | None = None,
) -> tuple[str, str, int, bool]:
    """执行命令，并发读取 stdout/stderr，返回 (stdout, stderr, exit_code, interrupted)"""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # 独立 process group，便于 killpg 杀整棵进程树
    )

    def _kill_tree() -> None:
        """杀掉整棵进程树（按 pgid）。"""
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # 进程已退出或无权限

    async def _pump(stream, chunks: list[str]) -> None:
        if stream is None:
            return
        while True:
            data = await stream.read(_STREAM_CHUNK_SIZE)
            if not data:
                break
            text = data.decode(errors="replace")
            chunks.append(text)
            if on_data is not None:
                on_data(text)

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_task = asyncio.create_task(_pump(proc.stdout, stdout_chunks))
    stderr_task = asyncio.create_task(_pump(proc.stderr, stderr_chunks))

    async def _wait_proc() -> int:
        if hasattr(proc, "wait"):
            return await proc.wait()
        await proc.communicate()
        return proc.returncode or 0

    try:
        await asyncio.wait_for(_wait_proc(), timeout=timeout)
        await asyncio.gather(stdout_task, stderr_task)
        return (
            "".join(stdout_chunks),
            "".join(stderr_chunks),
            proc.returncode or 0,
            False,
        )
    except asyncio.TimeoutError:
        _kill_tree()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return (
            "".join(stdout_chunks),
            "".join(stderr_chunks),
            -1,
            True,
        )
    except asyncio.CancelledError:
        _kill_tree()
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise


def _truncate(content: str) -> dict[str, Any]:
    """超过阈值时优先保留尾部，便于看到命令结果与错误摘要。"""
    if len(content) <= _MAX_OUTPUT:
        return {
            "text": content,
            "truncated": False,
            "strategy": "tail",
            "full_length": len(content),
            "returned_length": len(content),
            "omitted_lines": 0,
        }

    omitted = content[: len(content) - _MAX_OUTPUT]
    omitted_lines = omitted.count("\n")
    prefix = f"... [{omitted_lines} 行已省略] ...\n\n"
    tail_budget = max(0, _MAX_OUTPUT - len(prefix))
    tail = content[-tail_budget:] if tail_budget > 0 else ""
    text = prefix + tail
    return {
        "text": text,
        "truncated": True,
        "strategy": "tail",
        "full_length": len(content),
        "returned_length": len(text),
        "omitted_lines": omitted_lines,
    }


def _write_full_output(content: str) -> str:
    fd, path = tempfile.mkstemp(prefix="akasic-shell-", suffix=".log")
    os.close(fd)
    Path(path).write_text(content, encoding="utf-8")
    return path


def _validate_command(
    command: str,
    *,
    allow_network: bool,
    restricted_dir: Path | None,
    cwd: Path | None = None,
) -> str | None:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "命令解析失败，请检查引号是否匹配"
    if not tokens:
        return None

    cmd = tokens[0].lower()
    if not allow_network and cmd in _NETWORK_CMDS:
        return "当前 shell 配置禁止网络访问"

    if restricted_dir is not None:
        cwd_err = _validate_restricted_cwd(cwd, restricted_dir)
        if cwd_err:
            return cwd_err
        restricted_err = _validate_restricted_command(tokens, restricted_dir)
        if restricted_err:
            return restricted_err

    return _validate_network_command(command)


def _validate_network_command(command: str) -> str | None:
    """网络命令护栏：仅允许 HTTP(S) 且禁止内网目标与写入类参数。"""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "命令解析失败，请检查引号是否匹配"
    if not tokens:
        return None

    cmd = tokens[0].lower()
    if cmd not in _NETWORK_CMDS:
        return None

    # 阻止文件写入/上传相关参数
    for t in tokens[1:]:
        low = t.lower()
        if low in _NET_WRITE_FLAGS:
            return f"网络命令参数 '{t}' 不被允许（禁止上传/写文件）"
        if any(low.startswith(flag + "=") for flag in _NET_WRITE_FLAGS):
            return f"网络命令参数 '{t}' 不被允许（禁止上传/写文件）"
        # httpie/xh 支持 field=@file 语法上传文件
        if "=@" in t or t.startswith("@"):
            return f"网络命令参数 '{t}' 不被允许（禁止本地文件上传）"

    # 提取 URL 并校验
    urls = [t for t in tokens[1:] if t.startswith(("http://", "https://"))]
    if not urls:
        return "网络命令必须显式提供 http:// 或 https:// URL"

    for u in urls:
        err = _validate_url_target(u)
        if err:
            return err
    return None


def _validate_url_target(url: str) -> str | None:
    """校验 URL 目标是否为合法的公网地址。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "仅允许 http:// 或 https:// URL"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return "URL 缺少主机名"

    try:
        # IP 地址：禁止回环、私有、链路本地、保留地址
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            return f"禁止访问内网/本地地址：{host}"
    except ValueError:
        # 域名：阻断常见本地域名后缀
        if host.endswith(".local") or host.endswith(".localhost"):
            return f"禁止访问本地域名：{host}"
    return None


def _validate_restricted_command(tokens: list[str], restricted_dir: Path) -> str | None:
    command = " ".join(tokens)
    if any(marker in command for marker in _RESTRICTED_META_CHARS):
        return "受限 shell 禁止管道、重定向或串联命令"

    base_cmd = tokens[0].lower()
    if base_cmd in _RESTRICTED_SHELL_RUNNERS:
        return f"受限 shell 禁止启动解释器或二级 shell：{base_cmd}"

    for token in tokens[1:]:
        if token.startswith("-") or token == "--":
            continue
        err = _validate_restricted_token(token, restricted_dir)
        if err:
            return err
    return None


def _validate_restricted_cwd(cwd: Path | None, restricted_dir: Path) -> str | None:
    if cwd is None:
        return None
    try:
        resolved = cwd.resolve()
    except OSError:
        resolved = cwd
    if resolved != restricted_dir and restricted_dir not in resolved.parents:
        return f"受限 shell 禁止使用任务目录外工作目录：{cwd}"
    return None


def _validate_restricted_token(token: str, restricted_dir: Path) -> str | None:
    if token.startswith("~"):
        return f"受限 shell 禁止访问任务目录外路径：{token}"

    if not _looks_like_path(token):
        return None

    path = Path(token)
    if any(part == ".." for part in path.parts):
        return f"受限 shell 禁止访问父级路径：{token}"

    if path.is_absolute():
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved != restricted_dir and restricted_dir not in resolved.parents:
            return f"受限 shell 禁止访问任务目录外路径：{token}"
    return None


def _looks_like_path(token: str) -> bool:
    if token in {".", ".."}:
        return True
    return "/" in token or token.startswith((".", "~"))
