"""webchat-auth-wiring 任务 5.2：部署后服务器实跑 e2e（exchange → handshake → hello → 收发一轮）。

在**已部署的生产容器内**运行（与真实 uvicorn 应用进程、真实控制面 PG 同环境）：

    docker cp scripts/e2e_deploy_webchat_auth.py <容器>:/tmp/
    docker exec <容器> python /tmp/e2e_deploy_webchat_auth.py

流程：service 层创建 canary 账号并完成 provisioning → 签发一次性邀请 token →
真实 HTTP ``POST /api/auth/exchange``（Origin 取自 config allowlist）→ 携带真实
Set-Cookie 与 Origin 的 WebSocket 握手 → hello 返回 PG 派生三元组 → 经真实
AgentLoop 收发一轮（流式 delta + 终态帧）。负向：无 Cookie、Origin 不在
allowlist 的握手均被拒。结束撤销 canary 账号（保留审计）。

退出码 0=验收通过；输出即 evidence（openspec/evidence/webchat-auth-wiring/deploy-e2e.txt）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, "/app")

import httpx
import websockets  # noqa: E402

from agent.config_models import Config  # noqa: E402
from bootstrap.auth import create_auth_runtime  # noqa: E402

HTTP_BASE = "http://127.0.0.1:6322"
WS_URL = "ws://127.0.0.1:6322/ws"
CHAT_ORIGIN = "https://nexus.il7510n.dpdns.org"
WORKSPACE = Path("/root/.nexus/workspace")
TERMINAL_TIMEOUT_S = 180.0


async def main() -> int:
    config = Config.load("/app/config.toml")
    runtime = create_auth_runtime(config=config, workspace=WORKSPACE)
    try:
        created = await runtime.provisioning.create_account(
            display_name="deploy-e2e canary"
        )
        account_id = created["account"]["id"]
        await runtime.provisioning.run_pending(max_jobs=4)
        account = await runtime.provisioning.get_account(account_id)
        if account is None or account["status"] != "active":
            print(f"[FATAL] canary 账号未就绪：{account}")
            return 1
        _, raw_token = await runtime.auth.issue_invitation(
            account_id, issued_by="deploy-e2e"
        )
        convs = await runtime.canonical_repo.list_conversations_by_account(account_id)
        if not convs:
            print("[FATAL] canary 账号无 canonical conversation")
            return 1
        conv = convs[0]
        # PG 主存储（backend=postgres）：turn 的 require_ready 是真实门禁——
        # canary 跨进程创建，须在此显式触发分区 provisioning 并等 READY
        #（app 进程内注册走 partition_step 桥接，无此需要）。
        tenant_id = str(conv["tenant_id"])
        from infra.storage.provisioning import (
            PartitionStatus,
            TenantProvisioningService,
            TenantProvisioningWorker,
        )
        from memory2.store import VEC_DIM
        from infra.storage.postgres_memory_store import PostgresMemoryBackend

        sync_url = str(config.storage.postgres_url)
        if sync_url.startswith("postgresql+asyncpg://"):
            sync_url = sync_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        backend = PostgresMemoryBackend(sync_url, vec_dim=VEC_DIM)

        async def _run_db(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        svc = TenantProvisioningService(backend, run_db=_run_db)
        worker = TenantProvisioningWorker(svc)
        await worker.start()
        _ = await svc.request_provisioning(tenant_id)
        for _ in range(100):
            state = svc._states.get(tenant_id)
            if state is PartitionStatus.READY:
                break
            await asyncio.sleep(0.1)
        await worker.stop()
        if state is not PartitionStatus.READY:
            print(f"[FATAL] canary 租户分区未就绪：{state}")
            return 1
        print(f"[1] canary 账号 ready：account={account_id}")
        print(f"    PG 派生预期：tenant={conv['tenant_id']} conv={conv['id']}")

        async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=15) as client:
            resp = await client.post(
                "/api/auth/exchange",
                json={"token": raw_token},
                headers={"Origin": CHAT_ORIGIN},
            )
        if resp.status_code != 200:
            print(f"[FATAL] exchange 失败：{resp.status_code} {resp.text[:200]}")
            return 1
        cookie_pair = resp.headers["set-cookie"].split(";")[0]
        cookie_name = cookie_pair.split("=", 1)[0]
        attrs = ";".join(
            part.strip() for part in resp.headers["set-cookie"].split(";")[1:]
        )
        print(f"[2] exchange 200：Set-Cookie name={cookie_name}；属性={attrs}")

        try:
            async with websockets.connect(
                WS_URL, origin=CHAT_ORIGIN, open_timeout=5
            ) as ws:
                _ = await asyncio.wait_for(ws.recv(), timeout=5)
            print("[FATAL] 无 Cookie 握手未被拒绝")
            return 1
        except Exception as exc:
            print(f"[3] 无 Cookie 握手被拒：{type(exc).__name__}")

        try:
            async with websockets.connect(
                WS_URL,
                origin="https://evil.example.com",
                additional_headers={"Cookie": cookie_pair},
                open_timeout=5,
            ) as ws:
                _ = await asyncio.wait_for(ws.recv(), timeout=5)
            print("[FATAL] 非法 Origin 握手未被拒绝")
            return 1
        except Exception as exc:
            print(f"[4] 非法 Origin 握手被拒：{type(exc).__name__}")

        async with websockets.connect(
            WS_URL,
            origin=CHAT_ORIGIN,
            additional_headers={"Cookie": cookie_pair},
            open_timeout=10,
        ) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if hello.get("type") != "hello":
                print(f"[FATAL] 首帧不是 hello：{hello}")
                return 1
            ok_triple = (
                hello["account_id"] == str(account_id)
                and hello["tenant_id"] == str(conv["tenant_id"])
                and hello["conversation_id"] == str(conv["id"])
            )
            if not ok_triple:
                print(f"[FATAL] hello 三元组与 PG 派生不符：{hello}")
                return 1
            print(
                f"[5] hello 三元组与 PG 派生一致（latest_seq={hello['latest_seq']}）"
            )

            cmid = str(uuid.uuid4())
            await ws.send(
                json.dumps(
                    {
                        "type": "send",
                        "client_message_id": cmid,
                        "content": "部署验收连通性测试：请只回复 OK。",
                        "media": [],
                    }
                )
            )
            deltas: list[str] = []
            completed: dict | None = None
            while True:
                frame = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=TERMINAL_TIMEOUT_S)
                )
                if frame["type"] == "message.accepted":
                    if frame.get("client_message_id") != cmid:
                        print(f"[FATAL] accepted 回执 cmid 不符：{frame}")
                        return 1
                    print(f"[6] message.accepted（seq={frame.get('seq')}）")
                elif frame["type"] == "message.delta":
                    deltas.append(frame.get("content_delta") or "")
                elif frame["type"] == "error":
                    print(f"[FATAL] 收到 error 帧：{frame}")
                    return 1
                elif frame["type"] in {"turn.completed", "turn.failed"}:
                    completed = frame
                    break
            if completed["type"] != "turn.completed":
                print(f"[FATAL] turn 失败：{completed}")
                return 1
            print(
                f"[7] 流式 delta {len(deltas)} 段（合计 {len(''.join(deltas))} 字符）"
                f"→ turn.completed（seq={completed.get('seq')}，"
                f"content[:60]={completed.get('content', '')[:60]!r}）"
            )
    finally:
        try:
            await runtime.provisioning.revoke_account(
                account_id, reason="deploy-e2e canary 验收完成"
            )
            print("[8] canary 账号已撤销（保留审计记录）")
        except Exception as exc:  # noqa: BLE001 —— 清理失败不掩盖验收结论
            print(f"[WARN] canary 账号撤销失败（需手工处理 {account_id}）：{exc}")
        await runtime.aclose()

    print("E2E PASS: exchange → handshake(负向×2) → hello → 收发一轮（流式+终态）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
