"""M4H-4 commit 5：ConversationRuntime provisioning readiness gate 测试。

覆盖：PENDING 分区 fail-fast（executor 不进入，turn FAILED + retryable）、
worker 收敛后第二 turn 通过、FAILED 分区硬失败（retryable=False）、无
tenantId / 未接 provisioning 时 gate 跳过（executor 正常执行）。
PG 用例依赖本地 PG，无 PG 自动 skip。
"""
import asyncio
import os

import psycopg
import pytest

from agent.control.models import TurnRequest, TurnStatus
from agent.control.runtime import ConversationRuntime
from infra.storage.partitioning import (
    PartitionNotReady,
    PartitionProvisioningFailed,
    partition_name_for_tenant,
)
from infra.storage.provisioning import (
    PartitionStatus,
    TenantProvisioningService,
    TenantProvisioningWorker,
)
from infra.storage.runtime import StorageRuntime
from session.store import SessionStore

PG_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)


def _pg_alive() -> bool:
    try:
        conn = psycopg.connect(PG_URL, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture
def pg_alive() -> None:
    if not _pg_alive():
        pytest.skip(f"本地 PG 不可用（{PG_URL}），跳过 turn gate 测试")


def _tenant(tag: str) -> str:
    return f"gate_{tag}_{os.getpid()}"


def _drop_partition(name: str) -> None:
    import psycopg.sql as pgsql

    conn = psycopg.connect(PG_URL)
    try:
        conn.autocommit = True
        conn.execute(
            pgsql.SQL("DROP TABLE IF EXISTS {}").format(pgsql.Identifier(name))
        )
    finally:
        conn.close()


class _PgService:
    """runtime + service + worker 装配，teardown 清理创建的分区。"""

    def __init__(self) -> None:
        self.runtime = StorageRuntime(PG_URL, "unused.db", "unused.db", pool_size=4)
        memory_backend = self.runtime.memory_backend
        assert memory_backend is not None
        self.service = TenantProvisioningService(
            memory_backend,
            run_db=self.runtime.run_db,
        )
        self.worker = TenantProvisioningWorker(self.service, poll_interval=0.01)
        self.created: list[str] = []

    def close(self) -> None:
        for name in self.created:
            _drop_partition(name)
        self.runtime.close()


class _FakeProvisioning:
    """结构等价 TenantProvisioning Protocol：记录调用，require_ready 放行。"""

    def __init__(self) -> None:
        self.requested: list[str] = []
        self.ready_checked: list[str] = []

    async def request_provisioning(self, tenant_id: str) -> PartitionStatus:
        self.requested.append(tenant_id)
        return PartitionStatus.PENDING

    async def require_ready(self, tenant_id: str) -> None:
        self.ready_checked.append(tenant_id)

    def provision_tenant(self, tenant_id: str) -> None:
        pass


class _PendingProvisioning(_FakeProvisioning):
    """require_ready 始终抛 PartitionNotReady（模拟 PENDING 未收敛）。"""

    async def require_ready(self, tenant_id: str) -> None:
        await super().require_ready(tenant_id)
        raise PartitionNotReady(f"tenant {tenant_id} pending")


class _FailedProvisioning(_FakeProvisioning):
    async def require_ready(self, tenant_id: str) -> None:
        await super().require_ready(tenant_id)
        raise PartitionProvisioningFailed(f"tenant {tenant_id} failed")


async def _wait_for(predicate, timeout: float = 5.0, step: float = 0.05) -> None:
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return
        await asyncio.sleep(step)
        elapsed += step
    assert predicate(), "等待超时"


def _make_runtime(tmp_path, executor, provisioning=None) -> ConversationRuntime:
    return ConversationRuntime(
        SessionStore(tmp_path / "control.db"),
        executor,
        provisioning=provisioning,
    )


async def test_gate_skipped_without_provisioning(tmp_path) -> None:
    calls: list[TurnRequest] = []

    async def executor(request: TurnRequest) -> str:
        calls.append(request)
        return "ok"

    runtime = _make_runtime(tmp_path, executor)
    try:
        handle = await runtime.start_turn(
            TurnRequest("t", "hello", {"tenantId": "no-provisioning"})
        )
        result = await handle.result()
        assert result.status is TurnStatus.COMPLETED
        assert len(calls) == 1
    finally:
        await runtime.shutdown()


async def test_gate_skipped_without_tenant_id(tmp_path) -> None:
    calls: list[TurnRequest] = []
    prov = _FakeProvisioning()

    async def executor(request: TurnRequest) -> str:
        calls.append(request)
        return "ok"

    runtime = _make_runtime(tmp_path, executor, provisioning=prov)
    try:
        handle = await runtime.start_turn(TurnRequest("t", "hello", {}))
        result = await handle.result()
        assert result.status is TurnStatus.COMPLETED
        assert len(calls) == 1
        assert prov.requested == []  # 无 tenantId 不触发 provisioning
    finally:
        await runtime.shutdown()


async def test_gate_fails_fast_on_pending(tmp_path) -> None:
    calls: list[TurnRequest] = []
    prov = _PendingProvisioning()

    async def executor(request: TurnRequest) -> str:
        calls.append(request)
        return "ok"

    runtime = _make_runtime(tmp_path, executor, provisioning=prov)
    try:
        handle = await runtime.start_turn(
            TurnRequest("t", "hello", {"tenantId": "pending-tenant"})
        )
        result = await handle.result()
        assert result.status is TurnStatus.FAILED
        assert result.error is not None
        assert result.error.retryable is True  # PENDING fail-fast，上层可重试
        assert calls == []  # executor 未进入
        assert prov.ready_checked == ["pending-tenant"]
    finally:
        await runtime.shutdown()


async def test_gate_failed_partition_fails_hard(tmp_path) -> None:
    calls: list[TurnRequest] = []
    prov = _FailedProvisioning()

    async def executor(request: TurnRequest) -> str:
        calls.append(request)
        return "ok"

    runtime = _make_runtime(tmp_path, executor, provisioning=prov)
    try:
        handle = await runtime.start_turn(
            TurnRequest("t", "hello", {"tenantId": "failed-tenant"})
        )
        result = await handle.result()
        assert result.status is TurnStatus.FAILED
        assert result.error is not None
        assert result.error.retryable is False  # FAILED 需人工介入，不重试
        assert calls == []
    finally:
        await runtime.shutdown()


@pytest.mark.postgres
async def test_gate_provisions_then_executes(pg_alive, tmp_path) -> None:
    svc = _PgService()
    calls: list[TurnRequest] = []

    async def executor(request: TurnRequest) -> str:
        calls.append(request)
        return "ok"

    runtime = _make_runtime(tmp_path, executor, provisioning=svc.service)
    t = _tenant("prov")
    svc.created.append(partition_name_for_tenant(t))
    try:
        # 首次消息：UNKNOWN → request 提交 job，require_ready PENDING → fail-fast。
        h1 = await runtime.start_turn(
            TurnRequest("t1", "hello", {"tenantId": t})
        )
        r1 = await h1.result()
        assert r1.status is TurnStatus.FAILED
        assert r1.error is not None and r1.error.retryable is True
        assert calls == []  # 动态 DDL 不在用户 hot path

        # worker 收敛 READY 后，第二 turn 通过 gate 进入 executor。
        await svc.worker.start()
        await _wait_for(lambda: svc.service.status(t) is PartitionStatus.READY)
        h2 = await runtime.start_turn(
            TurnRequest("t1", "hello again", {"tenantId": t})
        )
        r2 = await h2.result()
        assert r2.status is TurnStatus.COMPLETED
        assert len(calls) == 1
    finally:
        await svc.worker.stop()
        await runtime.shutdown()
        svc.close()
