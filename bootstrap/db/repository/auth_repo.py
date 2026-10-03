"""C5 auth repository：凭据（token/session/admin）与 provisioning job。

事务边界（openspec/changes/2026-09-07-c5-auth-provisioning-admin/design.md
ADR-2/ADR-6，PILOT_ROADMAP §5.9.3/§5.9.13）：

- **一次性兑换**：`consume_token_for_session()` 单事务 = 行锁 token → 校验
  未消费/未撤销/未过期且账号 active → `UPDATE consumed_at` → `INSERT
  auth_sessions`；并发兑换在行锁上串行化，第二个事务看到 `consumed_at` 已
  置即失败（不区分原因，不泄露存在性）。
- **封禁级联**：`suspend_account()` 单事务 = 账号 `suspended` + 名下全部
  token/session 写 `revoked_at`（§5.3）；unsuspend 只恢复账号状态，不复活
  旧凭据（§5.9.9）。
- **admin**：bootstrap 仅当单行不存在；disable 单事务关登录入口并撤销全部
  admin 会话；rotate 只换 recovery digest，不动浏览器会话（§10 DECIDED）。

本模块只接受 digest（64 hex），不接受也不返回任何明文凭据。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.models.auth import (
    AccessTokenModel,
    AdminAuditEventModel,
    AdminCredentialModel,
    AuthSessionModel,
    TenantProvisioningJobModel,
)
from bootstrap.db.models.canonical import TestAccountModel
from bootstrap.db.repository._ids import to_uuid

__all__ = [
    "AdminBootstrapError",
    "AdminRepository",
    "CredentialExchangeError",
    "CredentialRepository",
    "SessionInvalidError",
    "SessionForbiddenError",
]

_UTC_NOW = lambda: datetime.now(UTC)


class CredentialExchangeError(Exception):
    """兑换失败（token 无效/已消费/已过期/账号不可用）。

    统一措辞：调用方不得向客户端区分具体原因（不泄露存在性，design ADR-3）。
    """


class SessionInvalidError(Exception):
    """会话缺失/已撤销/已过期（→ 401）。"""


class SessionForbiddenError(Exception):
    """principal 有效但被禁止：账号 suspended/revoked（→ 403）。"""


class AdminBootstrapError(Exception):
    """admin 单例已存在或尚未 bootstrap。"""


def _new_uuid() -> uuid.UUID:
    return uuid.uuid4()


def _session_to_dict(row: AuthSessionModel) -> dict[str, Any]:
    return {
        "id": row.id,
        "principal_type": row.principal_type,
        "account_id": row.account_id,
        "digest_version": row.digest_version,
        "idle_timeout_s": row.idle_timeout_s,
        "absolute_timeout_s": row.absolute_timeout_s,
        "user_agent": row.user_agent,
        "last_seen_at": row.last_seen_at,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "revoked_reason": row.revoked_reason,
        "created_at": row.created_at,
    }


def _token_to_dict(row: AccessTokenModel) -> dict[str, Any]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "tenant_name": row.tenant_name,
        "digest_version": row.digest_version,
        "display_note": row.display_note,
        "issued_by": row.issued_by,
        "expires_at": row.expires_at,
        "consumed_at": row.consumed_at,
        "revoked_at": row.revoked_at,
        "revoked_reason": row.revoked_reason,
        "created_at": row.created_at,
    }


class CredentialRepository:
    """邀请 Token / 登录会话 / admin 凭据的读写（digest-only）。"""

    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    # ── 邀请 Token ───────────────────────────────────────────────────

    async def issue_token(
        self,
        *,
        account_id: uuid.UUID | str,
        token_digest: str,
        display_note: str = "",
        issued_by: str = "admin",
        expires_at: datetime | None = None,
    ) -> dict:
        """为 active 账号签发邀请 Token（digest 入库；明文只在响应中出现一次）。

        账号非 `active`（provisioning/suspended/revoked）时拒绝签发
        （§5.9.13：ready 前不发 Token；revoked 拒绝新凭据）。
        """
        acc_id = to_uuid(account_id)
        async with self._sf() as sess, sess.begin():
            account = await sess.get(TestAccountModel, acc_id, with_for_update=True)
            if account is None or account.status != "active":
                raise CredentialExchangeError("invitation token issue rejected")
            row = AccessTokenModel(
                account_id=acc_id,
                token_digest=token_digest,
                display_note=display_note,
                issued_by=issued_by,
                expires_at=expires_at,
            )
            sess.add(row)
            await sess.flush()
            return _token_to_dict(row)

    async def issue_tenant_invite(
        self,
        *,
        tenant_name: str,
        token_digest: str,
        display_note: str = "",
        issued_by: str = "admin",
        expires_at: datetime | None = None,
    ) -> dict:
        """签发租户邀请码（invite-code-tenant-registration D1）。

        不依赖预先存在的账号：``account_id`` 为 NULL，携带管理员预指定租户名；
        注册消费后回填账号。`tenant_name` 非空（空白会被截断后拒绝）。
        """
        tenant_name = (tenant_name or "").strip()
        if not tenant_name:
            raise CredentialExchangeError("tenant name required")
        async with self._sf() as sess, sess.begin():
            row = AccessTokenModel(
                account_id=None,
                token_digest=token_digest,
                tenant_name=tenant_name[:255],
                display_note=display_note,
                issued_by=issued_by,
                expires_at=expires_at,
            )
            sess.add(row)
            await sess.flush()
            return _token_to_dict(row)

    async def find_account_by_email(self, email: str) -> dict | None:
        """按邮箱精确查找账号（登录校验用；内存态，永不进入 API 响应）。

        ``password_digest`` 只在此内部 dict 中携带供 verify（argon2 哈希，
        不落 API/审计）；邮箱不存在返回 None；状态（含 revoked）原样返回，
        错误语义由 service 层统一合并（不泄露存在性）。
        """
        email = (email or "").strip().lower()
        if not email:
            return None
        async with self._sf() as sess:
            row = (
                await sess.execute(
                    select(TestAccountModel).where(TestAccountModel.email == email)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": row.id,
                "status": row.status,
                "display_name": row.display_name,
                "email": row.email,
                "password_digest": row.password_digest,
            }

    async def register_from_invite(
        self,
        *,
        token_digest: str,
        email: str,
        password_digest: str,
        display_name: str = "",
    ) -> dict:
        """租户自助注册（invite-code-tenant-registration D2）：单事务消费邀请码。

        事务内容：

        1. `FOR UPDATE` 锁定邀请码行 → 校验未消费/未撤销/未过期；
        2. 校验邮箱全局唯一（`uq_test_accounts_email` 唯一索引兜底并发竞态，
           IntegrityError → :class:`CredentialExchangeError`，事务回滚零写入）；
        3. 创建 `provisioning` 账号（email/password_digest/display_name）；
        4. 创建 pending provisioning job；
        5. 回填 token.account_id + consumed_at（标记已消费）。

        provision 执行（executor）不在本事务内（D2：事务外 run_pending 收束，
        避免持有 token 行锁 + job 锁过长；失败时邀请码已消费但可幂等 retry）。

        返回 ``{"account": ..., "job": ..., "token": token_row}``；任何失败
        路径抛 :class:`CredentialExchangeError` 并整体回滚（不产生半消费 token /
        孤儿账号）。
        """
        email = (email or "").strip().lower()
        now = _UTC_NOW()
        async with self._sf() as sess, sess.begin():
            token = (
                await sess.execute(
                    select(AccessTokenModel)
                    .where(AccessTokenModel.token_digest == token_digest)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                token is None
                or token.consumed_at is not None
                or token.revoked_at is not None
                or (token.expires_at is not None and token.expires_at <= now)
            ):
                raise CredentialExchangeError("invitation token rejected")
            if not token.tenant_name:
                # 旧式绑定账号的 Token 不能用注册路径消费（无租户名）；
                # 走 exchange。统一错误语义不泄露差异。
                raise CredentialExchangeError("invitation token rejected")
            # 邮箱唯一性：应用内先查一次（快路径），唯一索引兜底并发竞态。
            existing = (
                await sess.execute(
                    select(TestAccountModel.id).where(TestAccountModel.email == email)
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise CredentialExchangeError("email already registered")
            acc_id = uuid.uuid4()
            default_name = (email.split("@")[0] if email and "@" in email else "") or (
                token.tenant_name or ""
            )
            account = TestAccountModel(
                id=acc_id,
                status="provisioning",
                display_name=display_name[:255] or default_name[:255],
                email=email or None,
                password_digest=password_digest,
            )
            job = TenantProvisioningJobModel(account_id=acc_id, status="pending")
            # 先 flush account 再插 job：PG FK 顺序要求（同 provisioning_repo 注记）。
            sess.add(account)
            try:
                await sess.flush()
            except IntegrityError:
                # 并发同邮箱注册：唯一索引冲突 → 统一认证错误（不泄露存在性）。
                raise CredentialExchangeError(
                    "email already registered"
                ) from None
            sess.add(job)
            token.consumed_at = now
            token.account_id = acc_id
            await sess.flush()
            return {
                "account": {
                    "id": account.id,
                    "status": account.status,
                    "display_name": account.display_name,
                    "email": account.email,
                    "created_at": account.created_at,
                    "updated_at": account.updated_at,
                },
                "job": {
                    "id": job.id,
                    "account_id": job.account_id,
                    "tenant_id": job.tenant_id,
                    "operation": job.operation,
                    "status": job.status,
                    "attempt_count": job.attempt_count,
                    "last_error": job.last_error,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                },
                "token": _token_to_dict(token),
            }

    async def get_token(self, token_id: uuid.UUID | str) -> dict | None:
        async with self._sf() as sess:
            row = await sess.get(AccessTokenModel, to_uuid(token_id))
            return _token_to_dict(row) if row else None

    async def list_tokens_for_account(
        self, account_id: uuid.UUID | str
    ) -> list[dict]:
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(AccessTokenModel)
                    .where(AccessTokenModel.account_id == to_uuid(account_id))
                    .order_by(AccessTokenModel.created_at.desc())
                )
            ).scalars().all()
            return [_token_to_dict(r) for r in rows]

    async def revoke_token(
        self, token_id: uuid.UUID | str, *, reason: str = ""
    ) -> dict | None:
        """撤销单个邀请 Token（软撤销，保留审计行）。不存在/已撤销返回 None。"""
        async with self._sf() as sess, sess.begin():
            row = await sess.get(AccessTokenModel, to_uuid(token_id), with_for_update=True)
            if row is None or row.revoked_at is not None:
                return None
            row.revoked_at = _UTC_NOW()
            row.revoked_reason = reason
            await sess.flush()
            return _token_to_dict(row)

    async def consume_token_for_session(
        self,
        *,
        token_digest: str,
        session_digest: str,
        idle_timeout_s: int,
        absolute_timeout_s: int,
        user_agent: str = "",
    ) -> dict:
        """一次性原子兑换（design ADR-2）：成功即返回新会话行。

        全部失败路径抛 :class:`CredentialExchangeError`（统一措辞）；事务回滚
        保证失败时零写入（不产生半消费 token / 孤儿会话）。
        """
        now = _UTC_NOW()
        async with self._sf() as sess, sess.begin():
            token = (
                await sess.execute(
                    select(AccessTokenModel)
                    .where(AccessTokenModel.token_digest == token_digest)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                token is None
                or token.consumed_at is not None
                or token.revoked_at is not None
                or (token.expires_at is not None and token.expires_at <= now)
            ):
                raise CredentialExchangeError("invitation token rejected")
            account = await sess.get(
                TestAccountModel, token.account_id, with_for_update=True
            )
            if account is None or account.status != "active":
                raise CredentialExchangeError("invitation token rejected")
            token.consumed_at = now
            session = AuthSessionModel(
                principal_type="user",
                account_id=account.id,
                session_digest=session_digest,
                idle_timeout_s=idle_timeout_s,
                absolute_timeout_s=absolute_timeout_s,
                user_agent=user_agent[:255],
                expires_at=now + timedelta(seconds=absolute_timeout_s),
            )
            sess.add(session)
            await sess.flush()
            return _session_to_dict(session)

    # ── 登录会话 ─────────────────────────────────────────────────────

    async def create_user_session(
        self,
        *,
        account_id: uuid.UUID | str,
        session_digest: str,
        idle_timeout_s: int,
        absolute_timeout_s: int,
        user_agent: str = "",
    ) -> dict:
        """登录会话创建 seam（D4）：register / login / exchange 共用。

        只接受已 active 的账号（主动调用方在 service 层已前置校验；此处
        仍是 fail-closed 兜底：provisioning/suspended/revoked 拒绝建会话）。
        """
        acc_id = to_uuid(account_id)
        now = _UTC_NOW()
        async with self._sf() as sess, sess.begin():
            account = await sess.get(TestAccountModel, acc_id, with_for_update=True)
            if account is None or account.status != "active":
                raise CredentialExchangeError("session issue rejected")
            row = AuthSessionModel(
                principal_type="user",
                account_id=acc_id,
                session_digest=session_digest,
                idle_timeout_s=idle_timeout_s,
                absolute_timeout_s=absolute_timeout_s,
                user_agent=user_agent[:255],
                expires_at=now + timedelta(seconds=absolute_timeout_s),
            )
            sess.add(row)
            await sess.flush()
            return _session_to_dict(row)

    async def validate_session(
        self,
        *,
        session_digest: str,
        expected_principal: str,
    ) -> dict:
        """按 digest 校验会话并 touch（成功路径）。

        判定次序（spec「浏览器安全边界」：403 = principal 有效但被禁止）：

        1. 未知会话、或 principal_type 与 Cookie 语义不符（`__Host-nexus_session`
           vs `__Host-nexus_admin`，Cookie 隔离）→ 401；
        2. 账号 `suspended`/`revoked` → 403。**优先于会话撤销判定**：封禁级联会把
           名下 session 写 `revoked_at` 作为审计痕迹（design §5.3），但不能因此把
           「主体被禁」降级为「无有效会话」——spec 场景要求「封禁账号请求返回
           403 而非 401」；
        3. 已撤销 / absolute 过期 / idle 超时 → 401。

        idle 判定使用 `now - last_seen_at <= idle_timeout_s`；touch 与校验同一
        事务（行锁 UPDATE），并发同会话请求不会重复续期出交错窗口。
        """
        now = _UTC_NOW()
        async with self._sf() as sess, sess.begin():
            row = (
                await sess.execute(
                    select(AuthSessionModel)
                    .where(AuthSessionModel.session_digest == session_digest)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.principal_type != expected_principal:
                raise SessionInvalidError("authentication required")
            # 账号级封禁先于会话级撤销判定：suspend/revoke 会把名下 session 一并
            # 写 revoked_at（审计痕迹），但响应语义必须是 403（主体被禁）而非 401。
            if row.account_id is not None:
                account = await sess.get(TestAccountModel, row.account_id)
                if account is None or account.status in ("suspended", "revoked"):
                    raise SessionForbiddenError("forbidden")
            if row.revoked_at is not None or row.expires_at <= now:
                raise SessionInvalidError("authentication required")
            if (now - row.last_seen_at) > timedelta(seconds=row.idle_timeout_s):
                raise SessionInvalidError("authentication required")
            row.last_seen_at = now
            await sess.flush()
            return _session_to_dict(row)

    async def create_admin_session(
        self,
        *,
        session_digest: str,
        idle_timeout_s: int,
        absolute_timeout_s: int,
        user_agent: str = "",
    ) -> dict:
        """admin exchange 成功后的会话创建（account_id 为空，CHECK 允许）。"""
        now = _UTC_NOW()
        async with self._sf() as sess, sess.begin():
            row = AuthSessionModel(
                principal_type="admin",
                account_id=None,
                session_digest=session_digest,
                idle_timeout_s=idle_timeout_s,
                absolute_timeout_s=absolute_timeout_s,
                user_agent=user_agent[:255],
                expires_at=now + timedelta(seconds=absolute_timeout_s),
            )
            sess.add(row)
            await sess.flush()
            return _session_to_dict(row)

    async def revoke_session_by_digest(
        self, session_digest: str, *, reason: str = "logout"
    ) -> bool:
        """logout：只撤销当前会话（§5.9.3）。不存在/已撤销返回 False。"""
        async with self._sf() as sess, sess.begin():
            row = (
                await sess.execute(
                    select(AuthSessionModel)
                    .where(AuthSessionModel.session_digest == session_digest)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.revoked_at is not None:
                return False
            row.revoked_at = _UTC_NOW()
            row.revoked_reason = reason
            return True

    async def list_sessions_for_account(
        self, account_id: uuid.UUID | str
    ) -> list[dict]:
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(AuthSessionModel)
                    .where(AuthSessionModel.account_id == to_uuid(account_id))
                    .order_by(AuthSessionModel.created_at.desc())
                )
            ).scalars().all()
            return [_session_to_dict(r) for r in rows]

    async def purge_expired_credential_digests(
        self, *, grace_s: int, batch_size: int
    ) -> int:
        """过期凭据摘要抹除（p0-retention-wiring ADR-4）：UPDATE 置 NULL，不删行。

        条件三者同时满足：revoked_at 非空 ∧ 已过期（expires_at < now()，token
        的 expires_at 为 NULL 视为不过期）∧ revoked_at 早于 purge_grace_s 宽限
        （以撤销时刻为宽限锚点：撤销即凭据失效，宽限是事后追查窗口）。
        已清除行（digest IS NULL）不再入选 → 幂等。行本身与归属/时间 metadata
        保留供审计核验（§5.9.12 不物理删除审计链）。
        """
        cleared = 0
        for table in ("auth_sessions", "access_tokens"):
            sql = text(f"""
                WITH target AS (
                    SELECT id
                    FROM {table}
                    WHERE revoked_at IS NOT NULL
                      AND {'expires_at IS NOT NULL AND ' if table == 'access_tokens' else ''}expires_at < now()
                      AND revoked_at < now() - (:grace_s * interval '1 second')
                      AND {'session_digest' if table == 'auth_sessions' else 'token_digest'} IS NOT NULL
                    ORDER BY revoked_at ASC
                    LIMIT :batch
                ), purged AS (
                    UPDATE {table} t
                    SET {'session_digest' if table == 'auth_sessions' else 'token_digest'} = NULL
                    WHERE t.id IN (SELECT id FROM target)
                    RETURNING t.id
                )
                SELECT count(*) FROM purged
                """)
            async with self._sf() as sess, sess.begin():
                value = await sess.scalar(
                    sql, {"grace_s": int(grace_s), "batch": int(batch_size)}
                )
                cleared += int(value or 0)
        return cleared

    async def count_purgeable_credentials(self, *, grace_s: int, cap: int) -> int:
        """满足清除条件的凭据行数（dry-run 演练用；cap 截断单轮上限），零变更。

        判据与 :meth:`purge_expired_credential_digests` 逐字一致（维护时两处同改）。
        """
        total = 0
        for table, digest_col, token_extra in (
            ("auth_sessions", "session_digest", ""),
            ("access_tokens", "token_digest", "AND expires_at IS NOT NULL"),
        ):
            sql = text(f"""
                SELECT count(*) FROM (
                    SELECT id FROM {table}
                    WHERE revoked_at IS NOT NULL
                      {token_extra} AND expires_at < now()
                      AND revoked_at < now() - (:grace_s * interval '1 second')
                      AND {digest_col} IS NOT NULL
                    ORDER BY revoked_at ASC
                    LIMIT :cap
                ) s
                """)
            async with self._sf() as sess:
                value = await sess.scalar(
                    sql, {"grace_s": int(grace_s), "cap": int(cap)}
                )
                total += int(value or 0)
        return total

    async def count_credential_rows(self) -> tuple[int, int]:
        """(auth_sessions 总行数, access_tokens 总行数)（报告 scanned 用）。"""
        async with self._sf() as sess:
            sessions = await sess.scalar(
                select(func.count()).select_from(AuthSessionModel)
            )
            tokens = await sess.scalar(
                select(func.count()).select_from(AccessTokenModel)
            )
            return int(sessions or 0), int(tokens or 0)

class AdminRepository:
    """单一 admin principal（bootstrap/rotate/enable/disable）+ 审计。"""

    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    async def bootstrap(self, recovery_digest: str) -> dict:
        """首次初始化 admin（仅当单行不存在；明文不进入本层）。"""
        async with self._sf() as sess, sess.begin():
            existing = await sess.get(AdminCredentialModel, 1)
            if existing is not None:
                raise AdminBootstrapError("admin principal 已存在")
            row = AdminCredentialModel(
                id=1, enabled=True, recovery_digest=recovery_digest, revision=1
            )
            sess.add(row)
            await sess.flush()
            return self._to_dict(row)

    async def get(self) -> dict | None:
        async with self._sf() as sess:
            row = await sess.get(AdminCredentialModel, 1)
            return self._to_dict(row) if row else None

    async def rotate_recovery(
        self, new_digest: str, *, verify_digest: str | None
    ) -> dict:
        """轮换 recovery token。

        `verify_digest` 非空时必须与当前 digest 匹配（交互式路径）；
        `--force-local` 传 None（受信主机强制轮换，校验归 CLI 层 TTY 门禁）。
        轮换只换 recovery digest + revision+1，不触碰浏览器会话（§10 DECIDED）。
        """
        async with self._sf() as sess, sess.begin():
            row = await sess.get(AdminCredentialModel, 1, with_for_update=True)
            if row is None:
                raise AdminBootstrapError("admin principal 尚未 bootstrap")
            if (
                verify_digest is not None
                and verify_digest != (row.recovery_digest or "")
            ):
                raise CredentialExchangeError("recovery token 校验失败")
            row.recovery_digest = new_digest
            row.revision += 1
            row.rotated_at = _UTC_NOW()
            await sess.flush()
            return self._to_dict(row)

    async def disable_admin_login(self) -> dict:
        """关闭 admin 网页登录：拒绝新 exchange + 立即撤销全部 admin 会话。

        单事务（§5.9.3：已经登录的管理员浏览器也立即退出；enable 不复活旧
        session）。
        """
        now = _UTC_NOW()
        async with self._sf() as sess, sess.begin():
            row = await sess.get(AdminCredentialModel, 1, with_for_update=True)
            if row is None:
                raise AdminBootstrapError("admin principal 尚未 bootstrap")
            row.enabled = False
            row.disabled_at = now
            await sess.execute(
                update(AuthSessionModel)
                .where(
                    AuthSessionModel.principal_type == "admin",
                    AuthSessionModel.revoked_at.is_(None),
                )
                .values(revoked_at=now, revoked_reason="admin_login_disabled")
            )
            await sess.flush()
            return self._to_dict(row)

    async def enable_admin_login(self) -> dict:
        async with self._sf() as sess, sess.begin():
            row = await sess.get(AdminCredentialModel, 1, with_for_update=True)
            if row is None:
                raise AdminBootstrapError("admin principal 尚未 bootstrap")
            row.enabled = True
            row.disabled_at = None
            await sess.flush()
            return self._to_dict(row)

    async def verify_recovery_digest(self, digest: str) -> dict:
        """admin exchange 校验：enabled + digest 匹配（不泄露差异原因）。"""
        async with self._sf() as sess:
            row = await sess.get(AdminCredentialModel, 1)
            if (
                row is None
                or not row.enabled
                or row.recovery_digest is None
                or row.recovery_digest != digest
            ):
                raise CredentialExchangeError("recovery token rejected")
            return self._to_dict(row)

    async def revoke_admin_sessions(self, *, reason: str = "revoked_by_admin") -> int:
        """`revoke-sessions --all`：清空 admin 浏览器会话，不动 recovery token。"""
        now = _UTC_NOW()
        async with self._sf() as sess, sess.begin():
            result = await sess.execute(
                update(AuthSessionModel)
                .where(
                    AuthSessionModel.principal_type == "admin",
                    AuthSessionModel.revoked_at.is_(None),
                )
                .values(revoked_at=now, revoked_reason=reason)
            )
            return result.rowcount or 0

    async def count_active_admin_sessions(self) -> int:
        async with self._sf() as sess:
            value = await sess.scalar(
                select(func.count())
                .select_from(AuthSessionModel)
                .where(
                    AuthSessionModel.principal_type == "admin",
                    AuthSessionModel.revoked_at.is_(None),
                    AuthSessionModel.expires_at > _UTC_NOW(),
                )
            )
            return int(value or 0)

    async def audit(
        self,
        *,
        actor: str,
        action: str,
        target_type: str = "",
        target_id: str = "",
        detail: dict | None = None,
    ) -> None:
        """admin 审计事件（禁止写入明文凭据；detail 只放计数/摘要）。"""
        async with self._sf() as sess, sess.begin():
            sess.add(
                AdminAuditEventModel(
                    actor=actor[:64],
                    action=action[:64],
                    target_type=target_type[:32],
                    target_id=target_id[:64],
                    detail=detail,
                )
            )

    async def count_admin_audit_rows(self) -> int:
        """表内总行数（retention 报告 scanned 用）。"""
        async with self._sf() as sess:
            value = await sess.scalar(
                select(func.count()).select_from(AdminAuditEventModel)
            )
            return int(value or 0)

    async def count_expired_admin_audit(
        self, older_than_s: int, cap: int
    ) -> tuple[int, int]:
        """将过期的 admin 审计行数与体量估算（dry-run 用；cap 截断单轮上限）。

        按现有列（actor/action/target_type/target_id/detail）裁剪，不统一字段
        形状（p0-retention-wiring ADR-6；AdminAccessAuditEvent 对齐归 C12 §8.2）。
        """
        async with self._sf() as sess:
            row = (
                await sess.execute(
                    text("""
                        SELECT count(*), COALESCE(sum(nbytes), 0)
                        FROM (
                            SELECT COALESCE(octet_length(detail::text), 0) AS nbytes
                            FROM admin_audit_events
                            WHERE created_at < now() - (:seconds * interval '1 second')
                            ORDER BY created_at ASC
                            LIMIT :cap
                        ) s
                        """),
                    {"seconds": int(older_than_s), "cap": int(cap)},
                )
            ).one()
            return int(row[0]), int(row[1])

    async def delete_expired_admin_audit_batch(
        self, older_than_s: int, batch_size: int
    ) -> tuple[int, int]:
        """按 created_at 升序删除一批过期 admin 审计行（ADR-5）。返回 (行数, 字节估算)。"""
        async with self._sf() as sess, sess.begin():
            row = (
                await sess.execute(
                    text("""
                        WITH target AS (
                            SELECT id
                            FROM admin_audit_events
                            WHERE created_at < now() - (:seconds * interval '1 second')
                            ORDER BY created_at ASC
                            LIMIT :batch
                        ), dead AS (
                            DELETE FROM admin_audit_events a
                            USING target
                            WHERE a.id = target.id
                            RETURNING COALESCE(octet_length(a.detail::text), 0) AS nbytes
                        )
                        SELECT count(*), COALESCE(sum(nbytes), 0) FROM dead
                        """),
                    {"seconds": int(older_than_s), "batch": int(batch_size)},
                )
            ).one()
            return int(row[0]), int(row[1])

    async def list_audit(self, *, limit: int = 100) -> list[dict]:
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(AdminAuditEventModel)
                    .order_by(AdminAuditEventModel.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [
                {
                    "id": r.id,
                    "actor": r.actor,
                    "action": r.action,
                    "target_type": r.target_type,
                    "target_id": r.target_id,
                    "detail": r.detail,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    @staticmethod
    def _to_dict(row: AdminCredentialModel) -> dict:
        # status 命令只显示 enabled/revision/rotated_at/active session 数，
        # 不显示 digest（§5.9.3）。
        return {
            "enabled": row.enabled,
            "revision": row.revision,
            "rotated_at": row.rotated_at,
            "disabled_at": row.disabled_at,
            "created_at": row.created_at,
        }
