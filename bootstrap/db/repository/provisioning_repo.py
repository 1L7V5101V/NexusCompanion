"""C5 provisioning repository：账号状态机 + provisioning job 持久化。

事务边界（design.md ADR-6，PILOT_ROADMAP §5.9.13）：

- **开户**：`create_provisioning_account()` 单事务 = `test_accounts(provisioning)`
  + `tenant_provisioning_jobs(pending)`。
- **执行**：`claim_jobs()`（`FOR UPDATE SKIP LOCKED`，单进程内多协程安全）→
  `mark_running()`（首次原子分配 tenant_id）→ executor → `mark_ready()`（单
  事务 = job `ready` + 账号 `active`）或 `mark_failed()`。
- **恢复**：`recover_stale_jobs()` 把崩溃残留 `running` 复位 `pending`
  （单进程 Pilot：重启后不存在合法 running）。
- **封禁/撤销**：`suspend_account()` 单事务 = `suspended` + 全部 token/session
  软撤销；`unsuspend_account()` 只恢复状态（旧凭据不复活）；`revoke_account()`
  = 终态 `revoked` + 凭据撤销，不删任何数据行。
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.models.auth import (
    AccessTokenModel,
    AuthSessionModel,
    TenantProvisioningJobModel,
)
from bootstrap.db.models.canonical import TestAccountModel

__all__ = [
    "ProvisioningRepository",
    "ProvisioningStateError",
]

_UTC_NOW = lambda: datetime.now(UTC)

_TENANT_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


class ProvisioningStateError(Exception):
    """账号/job 状态机非法推进（调用方 bug 或并发竞争，fail-closed 上抛）。"""


def _job_to_dict(row: TenantProvisioningJobModel) -> dict:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "tenant_id": row.tenant_id,
        "operation": row.operation,
        "status": row.status,
        "attempt_count": row.attempt_count,
        "last_error": row.last_error,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "created_at": row.created_at,
    }


def _account_to_dict(row: TestAccountModel) -> dict:
    return {
        "id": row.id,
        "status": row.status,
        "display_name": row.display_name,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


class ProvisioningRepository:
    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    # ── 开户与 job 生命周期 ──────────────────────────────────────────

    async def create_provisioning_account(
        self, *, display_name: str = "", account_id: uuid.UUID | str | None = None
    ) -> dict:
        """单事务创建 provisioning 账号 + pending job（§5.9.13 正常开户入口）。

        返回 ``{"account": ..., "job": ...}``。
        """
        acc_id = _coerce(account_id) if account_id is not None else uuid.uuid4()
        async with self._sf() as sess, sess.begin():
            account = TestAccountModel(
                id=acc_id, status="provisioning", display_name=display_name
            )
            job = TenantProvisioningJobModel(account_id=acc_id, status="pending")
            sess.add(account)
            sess.add(job)
            await sess.flush()
            return {"account": _account_to_dict(account), "job": _job_to_dict(job)}

    async def get_job(self, job_id: uuid.UUID | str) -> dict | None:
        async with self._sf() as sess:
            row = await sess.get(TenantProvisioningJobModel, _coerce(job_id))
            return _job_to_dict(row) if row else None

    async def list_jobs(self, *, status: str | None = None) -> list[dict]:
        async with self._sf() as sess:
            stmt = select(TenantProvisioningJobModel).order_by(
                TenantProvisioningJobModel.created_at.asc()
            )
            if status is not None:
                stmt = stmt.where(TenantProvisioningJobModel.status == status)
            rows = (await sess.execute(stmt)).scalars().all()
            return [_job_to_dict(r) for r in rows]

    async def recover_stale_jobs(self) -> int:
        """启动扫描：崩溃残留 running 复位 pending（单进程语义，见文件头）。"""
        async with self._sf() as sess, sess.begin():
            result = await sess.execute(
                update(TenantProvisioningJobModel)
                .where(TenantProvisioningJobModel.status == "running")
                .values(status="pending")
            )
            return result.rowcount or 0

    async def claim_jobs(self, *, limit: int = 8) -> list[uuid.UUID]:
        """认领 pending job（SKIP LOCKED；只返回 id，执行态由 mark_running 固化）。"""
        async with self._sf() as sess, sess.begin():
            rows = (
                await sess.execute(
                    select(TenantProvisioningJobModel.id)
                    .where(TenantProvisioningJobModel.status == "pending")
                    .order_by(TenantProvisioningJobModel.created_at.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            return list(rows)

    async def mark_running(self, job_id: uuid.UUID | str) -> dict:
        """置 running；pending 首次执行时原子分配 tenant（`pilot-<12 位随机>`）。

        tenant 分配依赖 `uq_tenant_provisioning_jobs_tenant`：冲突（极小概率
        随机串碰撞）时 IntegrityError 上抛，重试即换新随机串。
        """
        async with self._sf() as sess, sess.begin():
            row = await sess.get(
                TenantProvisioningJobModel, _coerce(job_id), with_for_update=True
            )
            if row is None or row.status not in ("pending", "failed"):
                raise ProvisioningStateError(
                    f"job {job_id!r} 状态 {row.status if row else 'missing'} 不可执行"
                )
            if row.tenant_id is None:
                row.tenant_id = _new_tenant_id()
            row.status = "running"
            row.attempt_count += 1
            row.started_at = _UTC_NOW()
            row.finished_at = None
            row.last_error = None
            await sess.flush()
            return _job_to_dict(row)

    async def mark_ready(self, job_id: uuid.UUID | str) -> dict:
        """单事务：job `ready` + 账号 `active`（§5.9.13：ready 才可签发 Token）。"""
        async with self._sf() as sess, sess.begin():
            row = await sess.get(
                TenantProvisioningJobModel, _coerce(job_id), with_for_update=True
            )
            if row is None or row.status != "running":
                raise ProvisioningStateError(
                    f"job {job_id!r} 状态 {row.status if row else 'missing'} 不可置 ready"
                )
            account = await sess.get(
                TestAccountModel, row.account_id, with_for_update=True
            )
            if account is None or account.status != "provisioning":
                raise ProvisioningStateError(
                    f"账号 {row.account_id!r} 状态非法，不能进入 active"
                )
            row.status = "ready"
            row.finished_at = _UTC_NOW()
            account.status = "active"
            await sess.flush()
            return _job_to_dict(row)

    async def mark_failed(self, job_id: uuid.UUID | str, error: str) -> dict:
        async with self._sf() as sess, sess.begin():
            row = await sess.get(
                TenantProvisioningJobModel, _coerce(job_id), with_for_update=True
            )
            if row is None or row.status != "running":
                raise ProvisioningStateError(
                    f"job {job_id!r} 状态 {row.status if row else 'missing'} 不可置 failed"
                )
            row.status = "failed"
            row.last_error = error[:2000]
            row.finished_at = _UTC_NOW()
            await sess.flush()
            return _job_to_dict(row)

    async def retry_failed(self, job_id: uuid.UUID | str) -> dict:
        """failed → pending（同一 job 行、tenant 不变；幂等 retry 不产生第二个
        tenant/conversation/seed，§5.9.13）。"""
        async with self._sf() as sess, sess.begin():
            row = await sess.get(
                TenantProvisioningJobModel, _coerce(job_id), with_for_update=True
            )
            if row is None or row.status != "failed":
                raise ProvisioningStateError(
                    f"job {job_id!r} 状态 {row.status if row else 'missing'} 不可 retry"
                )
            row.status = "pending"
            row.finished_at = None
            await sess.flush()
            return _job_to_dict(row)

    # ── 账号状态机（suspend/unsuspend/revoke） ──────────────────────

    async def get_account(self, account_id: uuid.UUID | str) -> dict | None:
        async with self._sf() as sess:
            row = await sess.get(TestAccountModel, _coerce(account_id))
            return _account_to_dict(row) if row else None

    async def list_accounts(self) -> list[dict]:
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(TestAccountModel).order_by(
                        TestAccountModel.created_at.asc()
                    )
                )
            ).scalars().all()
            return [_account_to_dict(r) for r in rows]

    async def set_account_status(
        self,
        account_id: uuid.UUID | str,
        status: str,
        *,
        allowed_from: tuple[str, ...],
    ) -> dict:
        """受控状态推进（供 unsuspend/revoke 等；suspend 走级联方法）。"""
        async with self._sf() as sess, sess.begin():
            row = await sess.get(
                TestAccountModel, _coerce(account_id), with_for_update=True
            )
            if row is None or row.status not in allowed_from:
                raise ProvisioningStateError(
                    f"账号 {account_id!r} 状态 {row.status if row else 'missing'} "
                    f"不允许推进到 {status}"
                )
            row.status = status
            await sess.flush()
            return _account_to_dict(row)

    async def suspend_account(
        self, account_id: uuid.UUID | str, *, reason: str = "suspended"
    ) -> dict:
        """单事务封禁（§5.3）：账号 `suspended` + 名下全部 token/session 软撤销。

        只允许从 `active` 封禁（provisioning 账号无凭据，直接 revoke 走终态）。
        """
        now = _UTC_NOW()
        async with self._sf() as sess, sess.begin():
            row = await sess.get(
                TestAccountModel, _coerce(account_id), with_for_update=True
            )
            if row is None or row.status != "active":
                raise ProvisioningStateError(
                    f"账号 {account_id!r} 状态 {row.status if row else 'missing'} "
                    "不允许 suspend"
                )
            row.status = "suspended"
            await sess.execute(
                update(AccessTokenModel)
                .where(
                    AccessTokenModel.account_id == row.id,
                    AccessTokenModel.revoked_at.is_(None),
                )
                .values(revoked_at=now, revoked_reason=reason)
            )
            await sess.execute(
                update(AuthSessionModel)
                .where(
                    AuthSessionModel.account_id == row.id,
                    AuthSessionModel.revoked_at.is_(None),
                )
                .values(revoked_at=now, revoked_reason=reason)
            )
            await sess.flush()
            return _account_to_dict(row)

    async def revoke_account(
        self, account_id: uuid.UUID | str, *, reason: str = "revoked"
    ) -> dict:
        """终态撤销（§5.9.9）：`provisioning/active/suspended → revoked` + 凭据软撤销。

        不删除任何数据行（partition/历史/审计保留）；`revoked` 拒绝新凭据签发
        与新会话（CredentialRepository.issue_token / consume 均校验 active）。
        """
        now = _UTC_NOW()
        async with self._sf() as sess, sess.begin():
            row = await sess.get(
                TestAccountModel, _coerce(account_id), with_for_update=True
            )
            if row is None or row.status == "revoked":
                raise ProvisioningStateError(
                    f"账号 {account_id!r} 状态 {row.status if row else 'missing'} "
                    "不允许 revoke"
                )
            row.status = "revoked"
            await sess.execute(
                update(AccessTokenModel)
                .where(
                    AccessTokenModel.account_id == row.id,
                    AccessTokenModel.revoked_at.is_(None),
                )
                .values(revoked_at=now, revoked_reason=reason)
            )
            await sess.execute(
                update(AuthSessionModel)
                .where(
                    AuthSessionModel.account_id == row.id,
                    AuthSessionModel.revoked_at.is_(None),
                )
                .values(revoked_at=now, revoked_reason=reason)
            )
            await sess.flush()
            return _account_to_dict(row)

    async def count_jobs(self, *, status: str | None = None) -> int:
        async with self._sf() as sess:
            stmt = select(func.count()).select_from(TenantProvisioningJobModel)
            if status is not None:
                stmt = stmt.where(TenantProvisioningJobModel.status == status)
            value = await sess.scalar(stmt)
            return int(value or 0)


def _coerce(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _new_tenant_id() -> str:
    suffix = "".join(secrets.choice(_TENANT_ALPHABET) for _ in range(12))
    return f"pilot-{suffix}"
