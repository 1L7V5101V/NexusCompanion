"""C5 auth 运行装配（design.md ADR-7）。

- engine/session_factory 来自 ``config.storage.postgres_url``（Phase 1 控制面库）；
  Pilot 单进程语义，连接池使用 NullPool（无跨 loop 残留）。
- 服务单例：``AuthService`` / ``AdminAuthService`` / ``ProvisioningService``。
- ``create_auth_runtime`` 只在 ``auth.enabled`` 时装配；默认关闭时 chat/dashboard
  行为与 P0.5 完全一致。

启动扫描（ADR-6）：``run_startup_provisioning`` 先复位崩溃残留 ``running`` 再
认领执行 pending job；账号 ready 前不签发邀请 Token（§5.9.13 在 repo/service 层
校验，此处只负责编排）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from agent.config_models import Config

from bootstrap.auth.crypto import PepperProvider
from bootstrap.auth.service import (
    AdminAuthService,
    AuthConfig,
    AuthService,
    CanonicalAgentExecutor,
    ProvisioningService,
)
from bootstrap.db.repository.auth_repo import AdminRepository, CredentialRepository
from bootstrap.db.repository.canonical_repo import CanonicalIdentityRepository
from bootstrap.db.repository.provisioning_repo import ProvisioningRepository

__all__ = [
    "AuthRuntime",
    "create_auth_runtime",
]

logger = logging.getLogger(__name__)


class AuthRuntime:
    """auth/provisioning/admin 服务单例容器（装配面，不含 transport）。

    ``session_factory`` 由调用方提供（测试复用 scratch engine；运行环境用
    ``create_auth_runtime`` 自建并在关闭时 ``aclose``）。
    """

    def __init__(
        self,
        *,
        config: AuthConfig,
        workspace: Path,
        session_factory: async_sessionmaker,
        canonical_repo: CanonicalIdentityRepository,
        partition_step: Callable[[str], Awaitable[None]] | None = None,
        engine: AsyncEngine | None = None,
    ) -> None:
        self.config = config
        pepper = PepperProvider(workspace / "secrets")
        cred_repo = CredentialRepository(session_factory)
        admin_repo = AdminRepository(session_factory)
        prov_repo = ProvisioningRepository(session_factory)

        self.auth = AuthService(cred_repo, pepper, config)
        self.admin = AdminAuthService(admin_repo, cred_repo, pepper, config)
        self.provisioning = ProvisioningService(
            prov_repo,
            executor=CanonicalAgentExecutor(canonical_repo, partition_step),
            admin_audit=admin_repo,
        )
        # 供 WS 入口按 session 派生 tenant/conversation 归属（§5.9.1）。
        # check_ws_handshake 返回 session dict 正是为此预留（其 docstring）。
        self.canonical_repo = canonical_repo
        self._session_factory = session_factory
        self._engine = engine

    @property
    def session_factory(self) -> async_sessionmaker:
        return self._session_factory

    async def run_startup_provisioning(self, *, max_jobs: int = 32) -> int:
        """启动扫描：复位崩溃残留 running → 认领执行 pending（ADR-6）。

        返回本次处理 job 总数；崩溃复位与失败置 failed 的审计由 service 层写。
        """
        recovered = await self.provisioning.recover()
        processed = await self.provisioning.run_pending(max_jobs=max_jobs)
        if recovered:
            logger.info("auth provisioning 启动恢复：%d 个崩溃残留 running 重新入队", recovered)
        if processed:
            logger.info("auth provisioning 启动扫描：执行 %d 个 pending job", processed)
        return processed

    async def aclose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()


def create_auth_runtime(
    *,
    config: Config,
    workspace: Path,
    canonical_repo: CanonicalIdentityRepository | None = None,
    partition_step: Callable[[str], Awaitable[None]] | None = None,
) -> AuthRuntime:
    """按 ``config.storage.postgres_url`` 自建引擎/会话工厂并装配服务单例。

    ``canonical_repo`` 缺省时用同一 ``async_sessionmaker`` 自建（复用 canonical
    表跨池读：同一 PG 库内不同连接池互不干扰）。调用方负责在关闭时调用
    ``await runtime.aclose()`` 释放 engine。
    """
    engine = create_async_engine(
        config.storage.postgres_url,
        poolclass=NullPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    if canonical_repo is None:
        canonical_repo = CanonicalIdentityRepository(session_factory)
    return AuthRuntime(
        config=config.auth,
        workspace=workspace,
        session_factory=session_factory,
        canonical_repo=canonical_repo,
        partition_step=partition_step,
        engine=engine,
    )