#!/usr/bin/env python
"""本地 PG+auth 验证台：为多租户界面（onboarding / persona / 记忆面）起一个可登录的后端。

存在的理由有两条，都不是 config.toml 能解决的：

1. 默认 config.toml 是 dev 模式（无 [storage] 无 [auth]），而 /api/persona/* 这类
   路由要求 PG 存储后端与认证同时开启才注册（bootstrap/chat_api.py 里的
   `if durable_runtime is not None and auth_runtime is not None`）。只改配置不改
   装配会连带把 Telegram 和主动推送一起起来，消息会推到真实 chat_id。所以这里
   从 config.toml 派生一份独立配置：去掉 Telegram、关掉主动推送、补上存储与认证。

2. Windows 上 psycopg 的异步驱动不接受默认的 ProactorEventLoop，而项目自身不设
   事件循环策略（生产在 Linux）。所以启动前必须换 SelectorEventLoop。

用法：
    python scripts/local_chat_rig.py write-config   # 派生 config.local-chat.toml
    python scripts/local_chat_rig.py run            # 起后端（隐含先 write-config）
    python scripts/local_chat_rig.py check          # 只打印派生结果，不写文件

前置：便携版 PostgreSQL 在 5433 上跑着（D:\\Projects\\postgres\\data5433），
库与角色见 config.local-chat.toml 里的 postgres_url。

config.local-chat.toml 含真实 LLM 密钥，已在 .gitignore 中，不得入库。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SOURCE_CONFIG = REPO / "config.toml"
LOCAL_CONFIG = REPO / "config.local-chat.toml"
DEFAULT_WORKSPACE = Path.home() / ".nexus" / "workspace"

# 与 StorageConfig.postgres_url 的默认值一致：便携版 PG 的 5433 实例。
LOCAL_POSTGRES_URL = "postgresql+psycopg://nexus:nexus_dev@localhost:5433/nexus"
# vite dev 走 5173 代理，直连后端产物走 6322；两个都要在 Origin 白名单里。
LOCAL_ORIGINS = [
    "http://127.0.0.1:6322",
    "http://localhost:6322",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
]


def _derive() -> dict[str, Any]:
    """从真实 config.toml 派生本地验证台配置。"""
    if not SOURCE_CONFIG.exists():
        raise SystemExit(f"missing source config: {SOURCE_CONFIG}")

    raw: dict[str, Any] = tomllib.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))

    channels = raw.get("channels")
    if isinstance(channels, dict):
        # Telegram 一旦装配就会用真实 token 上线并回复消息，验证台不需要。
        channels.pop("telegram", None)
        channels["chat"] = {
            "enabled": True,
            "channel_name": "chat",
            "host": "127.0.0.1",
            "port": 6322,
        }

    # 主动推送会往真实 chat_id 发消息；drift 依附于它。两者一律关死。
    raw["proactive"] = {"enabled": False, "profile": "daily", "drift": {"enabled": False}}

    raw["storage"] = {
        "backend": "postgres",
        "postgres_url": LOCAL_POSTGRES_URL,
        "pool_size": 5,
    }
    # cookie_secure=false 是必须的：__Host- 前缀 Cookie 要求 Secure，
    # 而本地是 http，留着 true 浏览器根本不存会话 Cookie，登录后立刻掉线。
    raw["auth"] = {
        "enabled": True,
        "cookie_secure": False,
        "origin_allowlist": LOCAL_ORIGINS,
        "admin_allow_ips": ["127.0.0.1", "::1"],
    }
    return raw


def _to_toml(table: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[str]:
    """把嵌套 dict 打成 TOML；值用 json.dumps 转义（TOML 基本字符串兼容）。"""
    scalars: list[str] = []
    children: list[tuple[str, dict[str, Any]]] = []
    for key, value in table.items():
        if isinstance(value, dict):
            children.append((key, value))
        else:
            scalars.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")

    lines = [f"[{'.'.join(prefix)}]", *scalars, ""] if prefix else [*scalars, ""]
    for key, child in children:
        lines += _to_toml(child, prefix + (key,))
    return lines


def _render(raw: dict[str, Any]) -> str:
    header = (
        "# 由 scripts/local_chat_rig.py write-config 从 config.toml 派生。\n"
        "# 本地 PG+auth 验证台专用：无 Telegram、无主动推送。含密钥，勿入库。\n"
    )
    return header + "\n".join(_to_toml(raw)) + "\n"


def command_check(_: argparse.Namespace) -> int:
    text = _render(_derive())
    print(text)
    return 0


def command_write_config(_: argparse.Namespace) -> int:
    LOCAL_CONFIG.write_text(_render(_derive()), encoding="utf-8")
    # 回读一次，确保派生出来的 TOML 真的能被解析。
    tomllib.loads(LOCAL_CONFIG.read_text(encoding="utf-8"))
    print(f"wrote {LOCAL_CONFIG}")
    return 0


def _ensure_selector_loop() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def command_run(args: argparse.Namespace) -> int:
    if not LOCAL_CONFIG.exists():
        print("local config missing, deriving it first", flush=True)
        command_write_config(args)
    _ensure_selector_loop()

    sys.path.insert(0, str(REPO))
    from main import serve  # noqa: PLC0415  延迟导入：要先换事件循环策略

    # 用绝对路径：serve() 把 config_path 原样交给 Config.load，传相对路径就等于
    # 要求调用方必须站在仓库根目录启动。
    asyncio.run(serve(str(LOCAL_CONFIG), args.workspace))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workspace",
        type=Path,
        default=DEFAULT_WORKSPACE,
        help="agent workspace（默认 ~/.nexus/workspace）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="只打印派生出的配置，不写文件").set_defaults(
        func=command_check
    )
    sub.add_parser("write-config", help="派生 config.local-chat.toml").set_defaults(
        func=command_write_config
    )
    sub.add_parser("run", help="起后端（配置缺失时先自动派生）").set_defaults(func=command_run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
