"""C5 provisioning 生命周期测试（design ADR-6，PILOT_ROADMAP §5.9.13）。

开户使用真实 canonical executor（建 agent = canonical conversation，tenant 幂等），
失败/恢复用可抛错的 fake executor 注入。验证：

- 开户状态机：provisioning+pending → running(首次分配 tenant) → ready + active；
- 崩溃恢复：残留 running 复位 pending，重跑不产生第二个 agent；
- 幂等 retry：failed → pending → 重跑同一 job 行，同 tenant 不产生第二个 agent；
- ready 前不签发 Token；active 账号才可签发；
- suspend 撤销名下全部凭据、unsuspend 不复活旧凭据、revoked 拒绝新凭据且不删数据。

（按「暂不跑 PG」决策只写不跑；PG 可用后取消 skip 即得集成证据。）
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, update

from bootstrap.auth.service import CanonicalAgentExecutor
from bootstrap.db.models.auth import (
    AccessTokenModel,
    AuthSessionModel,
    TenantProvisioningJobModel,
)
from bootstrap.db.models.canonical import (
    CanonicalConversationModel,
    TestAccountModel as AccountModel,  # 别名避免 pytest 按 Test* 前缀收集模型类
)
from bootstrap.db.repository.auth_repo import (
    CredentialExchangeError,
    SessionForbiddenError,
    SessionInvalidError,
)

pytestmark = pytest.mark.postgres


class _FailExecutor:
    """供失败场景注入：provision 必抛错，job 置 failed。"""

    def __init__(self, message: str = "simulated tenant DDL failure"):
        self._message = message

    async def provision(self, *, account_id, tenant_id):
        raise RuntimeError(self._message)


async def _force_job_status(c5_runtime, job_id, status: str) -> None:
    """测试 seam：只改动 job 状态，不触碰账号（模拟崩溃残留/历史失败入口）。

    - ``job_id`` 直接交给 SQLAlchemy 绑定（asyncpg pgproto.UUID 不需要 Python 侧
      转换，避免 ``uuid.UUID(pgproto.UUID)`` 抛 AttributeError）。
    - 置 ``running`` 时必须同时给出 tenant（真实路径 ``mark_running`` 首步原子
      分配）；否则违背 ``ck_tenant_provisioning_jobs_tenant_present``（CHECK
      status IN ('pending','failed') OR tenant_id IS NOT NULL）。
    """
    values: dict = {"status": status}
    if status == "running":
        values["tenant_id"] = f"pilot-{'-'.join(str(job_id).split('-')[0:4])}"
    async with c5_runtime.session_factory() as sess, sess.begin():
        await sess.execute(
            update(TenantProvisioningJobModel)
            .where(TenantProvisioningJobModel.id == job_id)
            .values(**values)
        )


async def _count_rows(c5_runtime, model) -> int:
    async with c5_runtime.session_factory() as sess:
        value = await sess.scalar(select(func.count()).select_from(model))
    return int(value or 0)


def _real_executor(c5_runtime) -> CanonicalAgentExecutor:
    from bootstrap.db.repository.canonical_repo import CanonicalIdentityRepository

    return CanonicalAgentExecutor(CanonicalIdentityRepository(c5_runtime.session_factory))


async def test_account_provisioning_state_machine(c5_runtime, c5_reset):
    """开户状态机完整流转：provisioning+pending → active+ready，tenant 已分配。"""
    c5_reset()
    created = await c5_runtime.provisioning.create_account(display_name="Lifecycle")
    account_id = created["account"]["id"]
    assert created["account"]["status"] == "provisioning"
    assert created["job"]["status"] == "pending"
    assert created["job"]["tenant_id"] is None

    # ready 前（provisioning）不签发邀请 Token。
    with pytest.raises(CredentialExchangeError):
        await c5_runtime.auth.issue_invitation(account_id, issued_by="test")

    assert await c5_runtime.provisioning.run_pending(max_jobs=4) == 1
    account = await c5_runtime.provisioning.get_account(account_id)
    assert account["status"] == "active"
    ready = await c5_runtime.provisioning.list_jobs(status="ready")
    assert len(ready) == 1 and ready[0]["tenant_id"].startswith("pilot-")

    # active 后才能签发。
    _, raw = await c5_runtime.auth.issue_invitation(account_id, issued_by="test")
    assert raw.startswith("nxt_")  # design §112 冻结前缀


async def test_idempotent_retry_no_second_agent(c5_runtime, c5_reset):
    """同 job retry（failed→pending→执行）用同一 tenant/job 行，不产生第二个 agent。"""
    c5_reset()
    created = await c5_runtime.provisioning.create_account(display_name="Retry")
    account_id = created["account"]["id"]
    job_id = created["job"]["id"]
    assert await c5_runtime.provisioning.run_pending(max_jobs=4) == 1
    tenant0 = (await c5_runtime.provisioning.list_jobs(status="ready"))[0]["tenant_id"]

    # 手工回退 failed 后 retry：证明幂等重跑用同一 job 行。
    await _force_job_status(c5_runtime, job_id, "failed")
    await c5_runtime.provisioning.retry(job_id)
    jobs = await c5_runtime.provisioning.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["tenant_id"] == tenant0 and jobs[0]["status"] == "ready"

    # canonical 只建了一个 agent（同 tenant 幂等，第二次执行命中已有行）。
    assert await _count_rows(c5_runtime, CanonicalConversationModel) == 1
    assert await _count_rows(c5_runtime, AccountModel) == 1


async def test_executor_failure_marks_failed_and_retry_recovers(c5_runtime, c5_reset):
    """executor 抛错 → job failed（原因可见）；随后 retry 用真实 executor 成功置 ready。"""
    c5_reset()
    created = await c5_runtime.provisioning.create_account(display_name="FailOnce")
    job_id = created["job"]["id"]
    account_id = created["account"]["id"]

    c5_runtime.provisioning._executor = _FailExecutor()
    assert await c5_runtime.provisioning.run_pending(max_jobs=4) == 1
    failed = await c5_runtime.provisioning.list_jobs(status="failed")
    assert len(failed) == 1
    assert "simulated tenant DDL failure" in failed[0]["last_error"]
    assert (await c5_runtime.provisioning.get_account(account_id))["status"] == "provisioning"

    # 换成真实 executor retry：同一 job 行 tenant 不变，成功进入 active。
    c5_runtime.provisioning._executor = _real_executor(c5_runtime)
    await c5_runtime.provisioning.retry(job_id)
    assert (await c5_runtime.provisioning.get_account(account_id))["status"] == "active"
    assert await _count_rows(c5_runtime, AccountModel) == 1


async def test_crash_recovery_resets_stale_running(c5_runtime, c5_reset):
    """启动扫描复位崩溃残留 running → 重新执行成功进入 active（ADR-6/§5.9.13）。"""
    c5_reset()
    created = await c5_runtime.provisioning.create_account(display_name="Crash")

    # 用永不成功的 executor + 手工把 job 置 running：模拟首次执行中途崩溃。
    c5_runtime.provisioning._executor = _FailExecutor("crashed mid-flight")
    await _force_job_status(c5_runtime, created["job"]["id"], "running")

    recovered = await c5_runtime.provisioning.recover()
    assert recovered == 1
    pending = await c5_runtime.provisioning.list_jobs(status="pending")
    assert len(pending) == 1

    # 换真实 executor 重跑（run_pending 不含 recover，直接执行 pending）。
    c5_runtime.provisioning._executor = _real_executor(c5_runtime)
    assert await c5_runtime.provisioning.run_pending(max_jobs=4) == 1
    assert (await c5_runtime.provisioning.get_account(created["account"]["id"]))["status"] == "active"
    assert await _count_rows(c5_runtime, CanonicalConversationModel) == 1


async def test_suspend_revokes_credentials_unsuspend_not_resurrect(c5_runtime, c5_active_account):
    """suspend 撤销名下 token+session；unsuspend 只恢复状态，旧凭据不复活（§5.9.9）。"""
    account, raw = await c5_active_account()
    _, raw_session = await c5_runtime.auth.exchange_invitation(raw, user_agent="t")
    # 再签一张未消费邀请，验证 suspend 后无法用其兑换新会话。
    _, fresh_raw = await c5_runtime.auth.issue_invitation(account["id"], issued_by="test")

    await c5_runtime.provisioning.suspend_account(account["id"], reason="trial abuse")

    # 旧 session 立即失效（suspend 级联撤销 → SessionInvalidError 401 语义，
    # §5.3：账号 suspended + 名下全部 token/session 写 revoked_at）。
    with pytest.raises(SessionInvalidError):
        await c5_runtime.auth.validate_user_session(raw_session)
    # suspended 账号拒绝签发与兑换新凭据（错误体不区分原因，ADR-3）。
    with pytest.raises(CredentialExchangeError):
        await c5_runtime.auth.issue_invitation(account["id"], issued_by="test")
    with pytest.raises(CredentialExchangeError):
        await c5_runtime.auth.exchange_invitation(fresh_raw, user_agent="t")

    await c5_runtime.provisioning.unsuspend_account(account["id"])
    assert (await c5_runtime.provisioning.get_account(account["id"]))["status"] == "active"
    # 旧 session 不复活。
    with pytest.raises(SessionInvalidError):
        await c5_runtime.auth.validate_user_session(raw_session)
    # unsuspend 后新签发可用。
    _, new_raw = await c5_runtime.auth.issue_invitation(account["id"], issued_by="test")
    assert new_raw.startswith("nxt_")  # design §112 冻结前缀


async def test_account_suspended_forbidden_branch(c5_runtime, c5_active_account):
    """账号 suspended 且既有 session 未被级联撤销时 → SessionForbiddenError（403 语义）。
    该分支只在非级联路径可达（默认 suspend 会同步撤销凭据）；ADR-3 明确 403=有主体验证。"""
    account, raw = await c5_active_account()
    _, raw_session = await c5_runtime.auth.exchange_invitation(raw, user_agent="t")

    from bootstrap.db.repository.provisioning_repo import ProvisioningRepository

    repo = ProvisioningRepository(c5_runtime.session_factory)
    await repo.set_account_status(account["id"], "suspended", allowed_from=("active",))
    with pytest.raises(SessionForbiddenError):
        await c5_runtime.auth.validate_user_session(raw_session)


async def test_revoke_terminal_no_credential_no_delete(c5_runtime, c5_active_account):
    """revoked 终态：拒绝新凭据签发与新兑换，且不删除任何数据行（终态保留）。"""
    account, raw = await c5_active_account()
    token_before = await _count_rows(c5_runtime, AccessTokenModel)
    session_before = await _count_rows(c5_runtime, AuthSessionModel)

    await c5_runtime.provisioning.revoke_account(account["id"], reason="eol")
    with pytest.raises(CredentialExchangeError):
        await c5_runtime.auth.issue_invitation(account["id"], issued_by="test")
    with pytest.raises(CredentialExchangeError):
        await c5_runtime.auth.exchange_invitation(raw, user_agent="t")

    assert await _count_rows(c5_runtime, AccessTokenModel) == token_before
    assert await _count_rows(c5_runtime, AuthSessionModel) == session_before
    account_row = await c5_runtime.provisioning.get_account(account["id"])
    assert account_row["status"] == "revoked"


async def test_admin_audit_never_records_plaintext(c5_runtime, c5_active_account, c5_reset):
    """admin_audit_events 审计明细不含明文凭据（token/session/recovery，ADR-1）。

    `c5_reset` 提供空基线但必须**先于**凭据建立：bootstrap/rotate 依赖 admin
    单行从空库开始（共享 scratch DB 中前置测试可能已 bootstrap，重复 bootstrap
    抛 AdminBootstrapError）；账号/邀请/会话也须在 reset 之后重建，否则残影行
    被截断后 id 悬空。
    """
    c5_reset()
    created = await c5_runtime.provisioning.create_account(display_name="Audit")
    await c5_runtime.provisioning.run_pending(max_jobs=4)
    account = await c5_runtime.provisioning.get_account(created["account"]["id"])
    assert account["status"] == "active"
    _, raw = await c5_runtime.auth.issue_invitation(account["id"], issued_by="test")
    _, raw_session = await c5_runtime.auth.exchange_invitation(raw, user_agent="t")
    recovery_raw = await c5_runtime.admin.bootstrap()
    await c5_runtime.admin.rotate_recovery_token(None, force_local=True)
    await c5_runtime.provisioning.suspend_account(account["id"], reason="audit-test")

    from sqlalchemy import text

    async with c5_runtime.session_factory() as sess:
        rows = (
            await sess.execute(
                text(
                    "SELECT actor, action, target_type, target_id, "
                    "COALESCE(detail::text, ''), created_at "
                    "FROM admin_audit_events"
                )
            )
        ).fetchall()
    assert rows, "应有审计事件落库"
    blob = "\n".join("|".join(str(c) for c in row) for row in rows)
    assert raw not in blob
    assert raw_session not in blob
    assert recovery_raw not in blob