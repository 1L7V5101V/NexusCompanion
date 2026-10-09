"""6.10 / 5.1-5.2 admin misfire 查看与处置端点（`/api/admin/schedules*`）。

判据两半：① 静默丢弃变成可查事实（`missed`/`skipped` 过滤）；② 门禁与既有 admin 面
一致——非 admin 401、缺 CSRF 403、能力未装配时路由不挂载（404，不泄露存在性）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bootstrap.db.repository.schedule_repo import ScheduleRepository
from tests.auth_provisioning.test_http_contract import (
    DEV_ORIGIN,
    admin_authed,
    jar,
    make_client,
    make_runtime,
    seed_user_credentials,
    user_exchange,
)

pytestmark = pytest.mark.postgres


def test_admin_schedule_endpoints(c11_pg_url, c11_factory, tmp_path) -> None:
    asyncio.run(_body(c11_pg_url, c11_factory, tmp_path))


async def _tenant_of(factory: Any, account_id: str) -> str:
    """provisioning 只回账号 id，租户键在 canonical conversation 上（每租户恰一个）。"""
    from sqlalchemy import select

    from bootstrap.db.models.canonical import CanonicalConversationModel

    async with factory() as sess:
        row = (
            await sess.execute(
                select(CanonicalConversationModel.tenant_id).where(
                    CanonicalConversationModel.account_id == account_id
                )
            )
        ).scalar_one()
    return str(row)


async def _body(pg_url: str, factory: Any, tmp_path: Any) -> None:
    runtime = make_runtime(pg_url, tmp_path / "c11-admin")
    try:
        repo = ScheduleRepository(factory, misfire_grace_seconds=300)
        account, invite_raw = await seed_user_credentials(runtime)
        tenant_id = await _tenant_of(factory, account["id"])
        fired_at = datetime.now(UTC) - timedelta(seconds=400)
        job = await repo.create_job(
            tenant_id=tenant_id,
            trigger="at",
            tier="instant",
            when="14:30",
            fire_at=fired_at,
            timezone="UTC",
            delivery_channel="chat",
            delivery_target=tenant_id,
            message="喝水",
            name="admin 用例",
        )
        # 让到期扫描把这行判成 missed（超宽限），admin 面才有内容可查。
        await repo.claim_due_jobs(datetime.now(UTC), advance=lambda _job, _after: None)

        runtime.schedule_admin = repo
        # 门禁探针用独立 client：TestClient 自带 cookie jar，admin exchange 之后
        # 同一个实例会自己带上 nexus_admin，「无凭据」就测不到了。
        anon = make_client(runtime)
        assert anon.get("/api/admin/schedules").status_code == 401
        _, _data, user_cookie = user_exchange(anon, invite_raw)
        assert (
            anon.get("/api/admin/schedules", headers={"cookie": user_cookie}).status_code
            == 401
        )

        client = make_client(runtime)
        admin_session, admin_cookies = await admin_authed(client, runtime)
        admin_cookie = jar(nexus_admin=admin_cookies["nexus_admin"])
        csrf = runtime.auth.csrf_token(admin_session["session_id"])
        mutation = {"cookie": admin_cookie, "origin": DEV_ORIGIN, "x-csrf-token": csrf}

        # ── 查看面 ──
        listed = client.get("/api/admin/schedules", headers={"cookie": admin_cookie})
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert [i["id"] for i in items] == [job["id"]]
        assert items[0]["delivery_channel"] == "chat"
        assert items[0]["last_outcome"] == "missed"

        filtered = client.get(
            "/api/admin/schedules?status=revoked", headers={"cookie": admin_cookie}
        )
        assert filtered.json()["items"] == []

        missed = client.get(
            "/api/admin/schedules/executions?status=missed",
            headers={"cookie": admin_cookie},
        )
        assert missed.status_code == 200
        executions = missed.json()["items"]
        assert len(executions) == 1
        assert executions[0]["skip_reason"] == "misfire_grace_exceeded"
        assert executions[0]["schedule_revision"] == 1
        assert executions[0]["schedule_timezone"] == "UTC"
        # 只记账，不投递。
        succeeded = client.get(
            "/api/admin/schedules/executions?status=succeeded",
            headers={"cookie": admin_cookie},
        )
        assert succeeded.json()["items"] == []

        # ── 处置面 ──
        base = f"/api/admin/schedules/{job['id']}"
        assert anon.post(f"{base}/suspend").status_code == 401
        assert (
            client.post(
                f"{base}/suspend", headers={"cookie": admin_cookie, "origin": DEV_ORIGIN}
            ).status_code
            == 403
        )
        suspended = client.post(f"{base}/suspend", headers=mutation)
        assert suspended.status_code == 200
        assert suspended.json()["schedule"]["status"] == "suspended"
        assert suspended.json()["schedule"]["revision"] == 2

        resumed = client.post(f"{base}/resume", headers=mutation)
        assert resumed.json()["schedule"]["status"] == "active"
        assert resumed.json()["schedule"]["revision"] == 3

        revoked = client.post(f"{base}/revoke", headers=mutation)
        assert revoked.json()["schedule"]["status"] == "revoked"
        # revoked 是终态：resume 被拒（409），不静默改回 active。
        assert client.post(f"{base}/resume", headers=mutation).status_code == 409

        unknown = client.post(
            "/api/admin/schedules/00000000-0000-0000-0000-0000000000ff/suspend",
            headers=mutation,
        )
        assert unknown.status_code == 404

        # ── 能力未装配（非 PG 后端）→ 路由不挂载，404 不泄露存在性 ──
        runtime.schedule_admin = None
        bare = make_client(runtime)
        assert (
            bare.get("/api/admin/schedules", headers={"cookie": admin_cookie}).status_code
            == 404
        )
    finally:
        try:
            await runtime.aclose()
        except RuntimeError:
            pass
