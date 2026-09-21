"""C5 runbook 路径 C 的用户凭据探针（服务层）。

canary 未启用 chat 通道，``/api/auth/*`` 未挂载（HTTP 404），故用户邀请 token
的兑换断言走服务层同一实现路径
``CredentialRepository.consume_token_for_session``（不经 HTTP transport，
transport 契约已由 PG 集成套件 test_http_contract 覆盖）。

只打印状态结论；明文只写文件，不进 stdout / argv。

子命令：
  exchange <tokenfile>   兑换邀请 token → 打印 status=200 / status=401
  issue    <tokenfile>   新建 active 账号并签发邀请（模拟「重发邀请」），
                         明文写入 <tokenfile>
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

from agent.config_models import Config

from bootstrap.auth import create_auth_runtime
from bootstrap.db.repository.auth_repo import CredentialExchangeError

CONFIG = "/app/config.toml"
WORKSPACE = pathlib.Path("/root/.nexus/workspace")


async def main() -> int:
    command = sys.argv[1]
    runtime = create_auth_runtime(config=Config.load(CONFIG), workspace=WORKSPACE)
    try:
        if command == "exchange":
            raw = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8").strip()
            try:
                await runtime.auth.exchange_invitation(raw)
            except CredentialExchangeError:
                print("service user-exchange status=401")
            else:
                print("service user-exchange status=200")
            return 0
        if command == "issue":
            out = pathlib.Path(sys.argv[2])
            created = await runtime.provisioning.create_account(
                display_name="DrillC-Svc"
            )
            await runtime.provisioning.run_pending(max_jobs=4)
            account = await runtime.provisioning.get_account(created["account"]["id"])
            if account is None or account["status"] != "active":
                status = account["status"] if account else "missing"
                print(f"service issue status=0 account_status={status}")
                return 0
            _, raw = await runtime.auth.issue_invitation(
                account["id"], issued_by="drill:service"
            )
            out.write_text(raw, encoding="utf-8")
            print(f"service issue status=200 token_len={len(raw)}")
            return 0
    finally:
        await runtime.aclose()
    print(f"unknown command {command!r}")
    return 2


raise SystemExit(asyncio.run(main()))
