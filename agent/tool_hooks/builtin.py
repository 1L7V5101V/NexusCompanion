from __future__ import annotations

import shlex
from pathlib import Path

from agent.tool_hooks.base import ToolHook
from agent.tool_hooks.types import HookContext, HookOutcome


class ShellRmToRestoreHook(ToolHook):
    name = "shell_rm_to_restore"
    event = "pre_tool_use"

    def __init__(self, restore_dir: Path | None = None) -> None:
        self._restore_dir = (restore_dir or (Path.home() / "restore")).expanduser()

    def matches(self, ctx: HookContext) -> bool:
        if ctx.request.tool_name != "shell":
            return False
        command = str(ctx.current_arguments.get("command", "") or "").strip()
        return bool(self._rewrite_command(command))

    async def run(self, ctx: HookContext) -> HookOutcome:
        command = str(ctx.current_arguments.get("command", "") or "").strip()
        rewritten = self._rewrite_command(command)
        if not rewritten:
            return HookOutcome()
        self._restore_dir.mkdir(parents=True, exist_ok=True)
        updated = dict(ctx.current_arguments)
        updated["command"] = rewritten
        return HookOutcome(updated_input=updated)

    def _rewrite_command(self, command: str) -> str | None:
        # 例子先固定成：rm -rf foo bar
        # 最终要把它从“rm + 选项 + 目标”改写成“mv -- 目标... restore_dir”。
        try:
            # 第一步：把命令行字符串拆成 token。
            # 例子从 "rm -rf foo bar" 变成 ["rm", "-rf", "foo", "bar"]。
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return None
        if not tokens:
            return None

        prefix: list[str] = []
        i = 0
        # 第二步：读取 rm 前面的前缀。
        # 例子里没有前缀，所以 prefix 还是 []；
        # 如果是 "sudo rm -f /tmp/a"，这里会先得到 prefix=["sudo"]。
        while i < len(tokens):
            token = tokens[i]
            if Path(token).name == "rm":
                break
            if token == "sudo" or token == "env" or "=" in token:
                prefix.append(token)
                i += 1
                continue
            return None
        if i >= len(tokens) or Path(tokens[i]).name != "rm":
            return None

        i += 1
        targets: list[str] = []
        parsing_options = True
        # 第三步：跳过 rm 的选项，只保留真正的目标路径。
        # 例子从 ["rm", "-rf", "foo", "bar"] 继续处理后，得到 targets=["foo", "bar"]。
        # 普通 rm a.txt 则会得到 targets=["a.txt"]。
        while i < len(tokens):
            token = tokens[i]
            i += 1
            if parsing_options and token == "--":
                parsing_options = False
                continue
            if parsing_options and token.startswith("-") and token != "-":
                continue
            parsing_options = False
            targets.append(token)
        if not targets:
            return None

        # 第四步：把“删除目标”改写成“移动到恢复目录”。
        # 例子从 targets=["foo", "bar"] 变成：
        # ["mv", "--", "foo", "bar", "<restore_dir>"]。
        parts = [*prefix, "mv", "--"]
        parts.extend(targets)
        parts.append(str(self._restore_dir))
        return shlex.join(parts)
