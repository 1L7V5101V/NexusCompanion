"""C5 auth service 层：凭据签发/兑换、admin 生命周期、provisioning 执行。

职责（design.md §2 ADR）：

- ``AuthService``：邀请 Token 签发（active 账号）、一次性兑换 → 会话原值
  （只在响应中出现一次）、会话校验/touch、logout、CSRF 派生。
- ``AdminAuthService``：单一 admin principal 的 bootstrap/status/rotate/
  revoke-sessions/disable/enable；每步写 admin audit（无明文）。
- ``ProvisioningService``：开户（provisioning+pending job）、认领执行（executor
  seam）、启动恢复、幂等 retry、suspend/unsuspend/revoke。

时间参数在会话创建时固化（§design §1）；明文凭据只出现在方法返回值中，
调用方（HTTP/CLI）负责一次性展示且不得写日志。
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from agent.config_models import AuthConfig

from bootstrap.auth.crypto import (
    PepperProvider,
    TOKEN_PREFIX_INVITATION,
    TOKEN_PREFIX_RECOVERY,
    SESSION_COOKIE_VALUE_PREFIX,
    csrf_for_session,
    digest_value,
    new_token,
)
from bootstrap.db.repository.auth_repo import (
    AdminBootstrapError,
    AdminRepository,
    CredentialExchangeError,
    CredentialRepository,
    SessionForbiddenError,
    SessionInvalidError,
)
from bootstrap.db.repository._ids import to_uuid
from bootstrap.db.repository.provisioning_repo import (
    ProvisioningRepository,
    ProvisioningStateError,
)

__all__ = [
    "AdminAuthService",
    "AuthService",
    "AuthConfig",
    "ProvisioningExecutor",
    "ProvisioningService",
    "CanonicalAgentExecutor",
]

logger = logging.getLogger(__name__)

USER_COOKIE_NAME = "__Host-nexus_session"
ADMIN_COOKIE_NAME = "__Host-nexus_admin"
"""§5.9.3 冻结 Cookie 名。dev HTTP（cookie_secure=false）时退化为无前缀名
（浏览器拒绝在非 Secure 连接上设置 `__Host-` Cookie），由 cookie_name() 解析。"""


def cookie_name(admin: bool, *, secure: bool) -> str:
    """Cookie 名：`__Host-` 前缀要求 Secure；dev HTTP 显式退化为无前缀名。"""
    if admin:
        return ADMIN_COOKIE_NAME if secure else "nexus_admin"
    return USER_COOKIE_NAME if secure else "nexus_session"


# ── 密码哈希（D3：argon2，仅存哈希，不落明文） ──────────────────────

_PASSWORD_HASHER = PasswordHasher()
"""argon2id 默认参数（memory=64MiB, time=3, parallelism=4）。"""

# 用于邮箱不存在时的常量时间 burn（D3：杜绝 timing oracle 枚举邮箱）。
_DUMMY_PASSWORD_DIGEST = _PASSWORD_HASHER.hash("dummy-timing-burn")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(password: str) -> str:
    """argon2id 哈希（自含 salt/参数）。临时对象用于区分测试可替换。"""
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, digest: str) -> bool:
    """校验密码与 argon2 digest 是否匹配；digest 非法返回 False（不抛，统一语义）。"""
    try:
        return _PASSWORD_HASHER.verify(digest, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match((email or "").strip().lower()))


def _valid_password(password: str, *, min_length: int) -> bool:
    return isinstance(password, str) and len(password) >= max(1, int(min_length))


class AuthService:
    """普通用户凭据面（邀请 Token / 邮箱密码 / 登录会话 / CSRF）。"""

    def __init__(
        self,
        repo: CredentialRepository,
        pepper: PepperProvider,
        config: AuthConfig,
        provisioning: ProvisioningService | None = None,
    ):
        self._repo = repo
        self._pepper = pepper
        self._config = config
        self._provisioning = provisioning

    @property
    def config(self) -> AuthConfig:
        return self._config

    async def issue_invitation(
        self,
        account_id: UUID | str,
        *,
        display_note: str = "",
        issued_by: str = "admin",
    ) -> tuple[dict, str]:
        """签发邀请 Token；返回 (token 行, 明文)。明文只允许展示一次。"""
        raw = new_token(TOKEN_PREFIX_INVITATION)
        expires = datetime.now(UTC) + timedelta(
            hours=self._config.invitation_token_ttl_hours
        )
        row = await self._repo.issue_token(
            account_id=account_id,
            token_digest=digest_value(raw, self._pepper.get()),
            display_note=display_note,
            issued_by=issued_by,
            expires_at=expires,
        )
        return row, raw

    async def issue_tenant_invitation(
        self,
        tenant_name: str,
        *,
        display_note: str = "",
        issued_by: str = "admin",
    ) -> tuple[dict, str]:
        """签发租户邀请码（D1：不要求预存账号）；返回 (token 行, 明文)。

        明文只允许在响应中出现一次；调用方（HTTP/CLI）负责展示且不得写日志。
        """
        raw = new_token(TOKEN_PREFIX_INVITATION)
        expires = datetime.now(UTC) + timedelta(
            hours=self._config.invitation_token_ttl_hours
        )
        row = await self._repo.issue_tenant_invite(
            tenant_name=tenant_name,
            token_digest=digest_value(raw, self._pepper.get()),
            display_note=display_note,
            issued_by=issued_by,
            expires_at=expires,
        )
        return row, raw

    async def exchange_invitation(
        self, raw_token: str, *, user_agent: str = ""
    ) -> tuple[dict, str]:
        """一次性兑换 → (会话行, 会话 Cookie 原值)。失败统一 CredentialExchangeError。"""
        raw_session = new_token(SESSION_COOKIE_VALUE_PREFIX)
        session = await self._repo.consume_token_for_session(
            token_digest=digest_value(raw_token, self._pepper.get()),
            session_digest=digest_value(raw_session, self._pepper.get()),
            idle_timeout_s=self._config.session_idle_s,
            absolute_timeout_s=self._config.session_absolute_s,
            user_agent=user_agent,
        )
        return session, raw_session

    async def register_tenant(
        self,
        raw_invite_token: str,
        email: str,
        password: str,
        *,
        user_agent: str = "",
    ) -> tuple[dict, str]:
        """租户自助注册（D2）：消费邀请码 + 建号 + provisioning 收束 + 会话。

        成功返回 ``(session 行, session Cookie 原值)``；失败统一抛
        :class:`CredentialExchangeError`（token 无效 / email 重复 / 密码过短
        / provisioning 未收敛均走同一认证错误语义，不泄露具体原因）。
        """
        email = (email or "").strip().lower()
        if not _valid_email(email) or not _valid_password(
            password, min_length=self._config.password_min_length
        ):
            raise CredentialExchangeError("invalid registration payload")
        # 步骤 1：单事务消费邀请码 + 开户 + 持久 provisioning job（D2）。
        registered = await self._repo.register_from_invite(
            token_digest=digest_value(raw_invite_token, self._pepper.get()),
            email=email,
            password_digest=hash_password(password),
        )
        account = registered["account"]
        # 步骤 2：事务外同步收束 provisioning（与 admin create-account 同 seam）。
        if self._provisioning is None:
            raise CredentialExchangeError("provisioning unavailable")
        await self._provisioning.run_pending(max_jobs=16)
        refreshed = await self._provisioning.get_account(account["id"])
        if refreshed is None or refreshed["status"] != "active":
            # Provisioning 失败：账号进入 failed（spec：失败即消费邀请码 + 账号
            # failed，原因对管理员可见于 job.last_error；同一 job 可幂等 retry）。
            # 面向用户返回认证错误（统一语义，不泄露具体原因）。
            try:
                await self._provisioning.mark_account_failed(account["id"])
            except Exception:  # 状态机竞争等极端场景：以原有错误为准，仅告警。
                logger.warning(
                    "register_tenant: 账号 %s 标记 failed 失败", account["id"],
                    exc_info=True,
                )
            raise CredentialExchangeError("account provisioning pending")
        # 步骤 3：账号 active 后创建登录会话（§5.9.3 timeout 契约）。
        return await self._create_user_session(
            account_id=account["id"], user_agent=user_agent
        )

    async def login(
        self, email: str, password: str, *, user_agent: str = ""
    ) -> tuple[dict, str]:
        """邮箱+密码登录 → (会话行, 会话 Cookie 原值)。

        邮箱不存在 / 密码错误 / 账号 suspended·revoked → 同一个
        :class:`CredentialExchangeError`（不泄露存在性，D4/§AD）。邮箱不存在时
        也执行一次 verify（burn，恒定时间，杜绝 timing oracle）。
        """
        email = (email or "").strip().lower()
        account = await self._repo.find_account_by_email(email)
        if account is None:
            verify_password(password, _DUMMY_PASSWORD_DIGEST)  # burn: 恒定时间
            raise CredentialExchangeError("invalid credentials")
        if not verify_password(password, account["password_digest"] or ""):
            raise CredentialExchangeError("invalid credentials")
        if account["status"] != "active":
            # suspended/revoked（及理论上的 provisioning 残余）→ 同一语义。
            raise CredentialExchangeError("invalid credentials")
        return await self._create_user_session(
            account_id=account["id"], user_agent=user_agent
        )

    async def _create_user_session(
        self, *, account_id: UUID | str, user_agent: str = ""
    ) -> tuple[dict, str]:
        """登录会话创建 seam（D4）：register / login / exchange 共用。

        时间参数在创建时固化（design §1）；明文 session 只返回一次。
        """
        raw_session = new_token(SESSION_COOKIE_VALUE_PREFIX)
        session = await self._repo.create_user_session(
            account_id=account_id,
            session_digest=digest_value(raw_session, self._pepper.get()),
            idle_timeout_s=self._config.session_idle_s,
            absolute_timeout_s=self._config.session_absolute_s,
            user_agent=user_agent,
        )
        return session, raw_session

    async def validate_user_session(self, raw_cookie_value: str) -> dict:
        """普通会话校验（+touch）。401/403 语义映射由 API 层完成。"""
        return await self._repo.validate_session(
            session_digest=digest_value(raw_cookie_value, self._pepper.get()),
            expected_principal="user",
        )

    async def logout(self, raw_cookie_value: str) -> bool:
        """logout 只撤销当前会话（§5.9.3）。"""
        return await self._repo.revoke_session_by_digest(
            digest_value(raw_cookie_value, self._pepper.get()), reason="logout"
        )

    def csrf_token(self, session_id: UUID | str) -> str:
        return csrf_for_session(to_uuid(session_id).bytes, self._pepper.get())

    async def revoke_token(self, token_id: UUID | str, *, reason: str) -> dict | None:
        return await self._repo.revoke_token(token_id, reason=reason)


class AdminAuthService:
    """单一 admin principal 生命周期（§5.9.3 命令语义逐条落地）。"""

    def __init__(
        self,
        admin_repo: AdminRepository,
        cred_repo: CredentialRepository,
        pepper: PepperProvider,
        config: AuthConfig,
    ):
        self._admin = admin_repo
        self._cred = cred_repo
        self._pepper = pepper
        self._config = config

    async def bootstrap(self) -> str:
        """首次初始化：生成 recovery token，明文只向调用方 TTY 回显一次。"""
        raw = new_token(TOKEN_PREFIX_RECOVERY)
        await self._admin.bootstrap(digest_value(raw, self._pepper.get()))
        await self._admin.audit(actor="cli:bootstrap", action="admin.bootstrap")
        return raw

    async def status(self) -> dict:
        """status：enabled/revision/最近轮换时间/active session 数，无 digest。"""
        cred = await self._admin.get()
        if cred is None:
            return {"bootstrapped": False}
        active_sessions = await self._admin.count_active_admin_sessions()
        await self._admin.audit(
            actor="cli:status", action="admin.status", detail={"active_sessions": active_sessions}
        )
        return {"bootstrapped": True, "active_sessions": active_sessions, **cred}

    async def exchange_recovery(self, raw_token: str, *, user_agent: str = "") -> tuple[dict, str]:
        """admin exchange：校验 enabled + digest → 短期 admin 会话。"""
        await self._admin.verify_recovery_digest(
            digest_value(raw_token, self._pepper.get())
        )
        raw_session = new_token(SESSION_COOKIE_VALUE_PREFIX)
        session = await self._cred.create_admin_session(
            session_digest=digest_value(raw_session, self._pepper.get()),
            idle_timeout_s=self._config.admin_idle_s,
            absolute_timeout_s=self._config.admin_absolute_s,
            user_agent=user_agent,
        )
        return session, raw_session

    async def validate_admin_session(self, raw_cookie_value: str) -> dict:
        return await self._cred.validate_session(
            session_digest=digest_value(raw_cookie_value, self._pepper.get()),
            expected_principal="admin",
        )

    async def rotate_recovery_token(
        self, current_token_input: str | None, *, force_local: bool = False
    ) -> str:
        """轮换 recovery token（旧 token 立即失效；不动浏览器会话）。

        非 force 路径必须提供当前 token（交互式输入）；force 仅受信主机 TTY
        （门禁在 CLI 层）。返回新明文（只显示一次）。
        """
        verify: str | None = None
        if not force_local:
            if not current_token_input:
                raise CredentialExchangeError("需要交互式输入当前 recovery token")
            verify = digest_value(current_token_input, self._pepper.get())
        raw = new_token(TOKEN_PREFIX_RECOVERY)
        await self._admin.rotate_recovery(
            digest_value(raw, self._pepper.get()), verify_digest=verify
        )
        await self._admin.audit(
            actor="cli:rotate-recovery-token",
            action="admin.rotate_recovery",
            detail={"force_local": force_local},
        )
        return raw

    async def revoke_sessions_all(self) -> int:
        """紧急清除全部 admin 浏览器会话；不改变 recovery token。"""
        count = await self._admin.revoke_admin_sessions(reason="revoke_sessions_all")
        await self._admin.audit(
            actor="cli:revoke-sessions",
            action="admin.revoke_sessions",
            detail={"revoked": count},
        )
        return count

    async def disable(self) -> None:
        """临时关闭 admin 网页登录：新 exchange 拒绝 + 已登录浏览器立即退出。"""
        await self._admin.disable_admin_login()
        await self._admin.audit(actor="cli:disable", action="admin.disable")

    async def enable(self) -> None:
        await self._admin.enable_admin_login()
        await self._admin.audit(actor="cli:enable", action="admin.enable")


class ProvisioningExecutor(Protocol):
    """provisioning 执行器 seam（design ADR-6）。

    实现必须幂等（同 account+tenant retry 不产生第二个 agent/seed）；
    抛错即 job 置 failed（原因对管理员可见）。
    """

    async def provision(self, *, account_id: UUID, tenant_id: str) -> None: ...


class CanonicalAgentExecutor:
    """内置执行器：canonical agent（C1 表）+ 可选分区/binding provisioning hook。

    `partition_step` 为 None 时只建 canonical agent（测试/最小部署）；生产
    wiring 传入 ``lambda tid: partitions.request_provisioning(tid)``（入队，
    DDL 由既有分区 worker 异步完成；turn 入口 ``require_ready()`` 是纵深
    防御，§5.9.13 禁止第一轮 turn 隐式触发正常开户流程）。
    `binding_ensure`（C14 §5.9.16 规则 1/3）：tenant 创建时幂等建立 memory
    engine slot 初始绑定（``default``）；None 时由 work-start/GET 懒补齐兜底。
    """

    def __init__(
        self,
        canonical_repo,
        partition_step: Callable[[str], Awaitable[None]] | None = None,
        binding_ensure: Callable[[str], Awaitable[None]] | None = None,
    ):
        self._canonical = canonical_repo
        self._partition_step = partition_step
        self._binding_ensure = binding_ensure

    async def provision(self, *, account_id: UUID, tenant_id: str) -> None:
        if self._partition_step is not None:
            await self._partition_step(tenant_id)
        await self._canonical.create_agent(account_id, tenant_id)
        if self._binding_ensure is not None:
            await self._binding_ensure(tenant_id)


class ProvisioningService:
    """账号 provisioning/readiness 生命周期（§5.9.13）。"""

    def __init__(
        self,
        repo: ProvisioningRepository,
        executor: ProvisioningExecutor,
        *,
        admin_audit: AdminRepository | None = None,
    ):
        self._repo = repo
        self._executor = executor
        self._admin_audit = admin_audit

    async def create_account(
        self, *, display_name: str = ""
    ) -> dict:
        """开户入口：单事务 provisioning 账号 + pending job。"""
        result = await self._repo.create_provisioning_account(display_name=display_name)
        await self._audit("provisioning.account_created", target=str(result["account"]["id"]))
        return result

    async def recover(self) -> int:
        """启动扫描：崩溃残留 running 复位 pending（§5.9.13）。"""
        count = await self._repo.recover_stale_jobs()
        if count:
            await self._audit("provisioning.recovered_stale", detail={"count": count})
        return count

    async def run_pending(self, *, max_jobs: int = 32) -> int:
        """认领并执行 pending job 至收敛；单 job 失败置 failed 不中断批次。"""
        processed = 0
        while processed < max_jobs:
            job_ids = await self._repo.claim_jobs(limit=min(8, max_jobs - processed))
            if not job_ids:
                break
            for job_id in job_ids:
                await self._execute_job(job_id)
                processed += 1
        return processed

    async def retry(self, job_id: UUID | str) -> dict:
        """failed → pending 并立即执行一次（幂等，同 tenant）。"""
        await self._repo.retry_failed(job_id)
        return await self._execute_job(to_uuid(job_id))

    async def _execute_job(self, job_id: UUID) -> dict:
        running = await self._repo.mark_running(job_id)
        try:
            await self._executor.provision(
                account_id=running["account_id"], tenant_id=running["tenant_id"] or ""
            )
        except Exception as exc:  # executor 失败原因对管理员可见（§5.9.13）
            logger.warning("provisioning job %s 失败: %s", job_id, exc)
            failed = await self._repo.mark_failed(job_id, str(exc))
            await self._audit(
                "provisioning.job_failed",
                target=str(job_id),
                detail={"attempt": failed["attempt_count"]},
            )
            return failed
        ready = await self._repo.mark_ready(job_id)
        await self._audit("provisioning.job_ready", target=str(job_id))
        return ready

    async def suspend_account(self, account_id: UUID | str, *, reason: str = "suspended") -> dict:
        result = await self._repo.suspend_account(account_id, reason=reason)
        await self._audit(
            "account.suspended", target=str(result["id"]), detail={"reason": reason}
        )
        return result

    async def unsuspend_account(self, account_id: UUID | str) -> dict:
        result = await self._repo.set_account_status(
            account_id, "active", allowed_from=("suspended",)
        )
        await self._audit("account.unsuspended", target=str(result["id"]))
        return result

    async def revoke_account(self, account_id: UUID | str, *, reason: str = "revoked") -> dict:
        result = await self._repo.revoke_account(account_id, reason=reason)
        await self._audit(
            "account.revoked", target=str(result["id"]), detail={"reason": reason}
        )
        return result

    async def list_accounts(self) -> list[dict]:
        return await self._repo.list_accounts()

    async def list_jobs(self, *, status: str | None = None) -> list[dict]:
        return await self._repo.list_jobs(status=status)

    async def get_account(self, account_id: UUID | str) -> dict | None:
        return await self._repo.get_account(account_id)

    async def get_job(self, job_id: UUID | str) -> dict | None:
        """按 id 读 provisioning job。

        管理面失败可见性依赖它（§5.9.13 / design ADR-6「失败 → ``failed`` +
        ``last_error`` 对管理员可见」）：``bootstrap.auth.api`` 在建号未收敛时
        返回 job 状态与失败原因。
        """
        return await self._repo.get_job(job_id)

    async def mark_account_failed(
        self, account_id: UUID | str, *, reason: str = "provisioning failed"
    ) -> dict:
        """注册流程失败收束：账号 provisioning → failed（spec 场景）。

        `failed` 非终态：管理员对同一 job 幂等 retry 时 ``mark_ready`` 允许从
        failed 恢复 active（invite-code-tenant-registration）。
        """
        return await self._repo.mark_account_failed(account_id, reason=reason)

    async def _audit(
        self,
        action: str,
        *,
        target: str = "",
        detail: dict | None = None,
    ) -> None:
        if self._admin_audit is None:
            return
        await self._admin_audit.audit(
            actor="provisioning", action=action, target_type="account", target_id=target, detail=detail
        )
