"""M4H-4 独立 control worker 测试（commit 4，ADR §1.3）。

覆盖：队列消费与 READY 收敛、retryable 失败（psycopg.Error）backoff 重试、
尝试耗尽置 FAILED、非重试错误立即 FAILED、同 tenant 在途去重（恰一次处理）、
start/stop 生命周期与重启恢复、advisory 锁超时真实失败回滚后 FAILED 重入队收敛。
postgres 用例依赖本地 PG，无 PG 自动 skip。
"""
import asyncio
import os

import psycopg
import pytest

from infra.storage.provisioning import (
    PartitionStatus,
    TenantProvisioningService,
    TenantProvisioningWorker,
    partition_name_for_tenant,
)
from infra.storage.runtime import StorageRuntime
from psycopg import sql as pgsql

PG_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)
_LOCK_KEY = 872_001_457


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
        pytest.skip(f"本地 PG 不可用（{PG_URL}），跳过 postgres provisioning worker 测试")


def _tenant(tag: str) -> str:
    return f"provw_{tag}_{os.getpid()}"


def _drop_partition(name: str) -> None:
    conn = psycopg.connect(PG_URL)
    try:
        conn.autocommit = True
        conn.execute(
            pgsql.SQL("DROP TABLE IF EXISTS {}").format(pgsql.Identifier(name))
        )
    finally:
        conn.close()


class _Service:
    """runtime + service 装配，teardown 清理创建的分区。"""

    def __init__(self, tmp_path, pool_size: int = 4, lock_timeout: str = "2s") -> None:
        self.runtime = StorageRuntime(PG_URL, "unused.db", "unused.db", pool_size=pool_size)
        memory_backend = self.runtime._memory_backend
        assert memory_backend is not None  # postgres 模式必有 backend
        self.service = TenantProvisioningService(
            memory_backend,
            run_db=self.runtime.run_db,
            lock_timeout=lock_timeout,
        )
        self.created: list[str] = []

    def close(self) -> None:
        for name in self.created:
            _drop_partition(name)
        self.runtime.close()


async def _wait_for(predicate, timeout: float = 5.0, step: float = 0.05) -> None:
    deadline = timeout
    elapsed = 0.0
    while elapsed < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
        elapsed += step
    assert predicate(), "等待超时"


@pytest.mark.postgres
async def test_worker_drains_queue_and_provisions(pg_alive, tmp_path) -> None:
    svc = _Service(tmp_path)
    worker = TenantProvisioningWorker(svc.service, poll_interval=0.01)
    await worker.start()
    try:
        t = _tenant("drain")
        svc.created.append(partition_name_for_tenant(t))
        assert await svc.service.request_provisioning(t) is PartitionStatus.PENDING
        assert t in svc.service._queued
        await _wait_for(lambda: svc.service.status(t) is PartitionStatus.READY)
        assert t not in svc.service._queued
        assert svc.service.created_count == 1
    finally:
        await worker.stop()
        svc.close()


@pytest.mark.postgres
async def test_worker_retries_retryable_error_then_succeeds(
    pg_alive, tmp_path, monkeypatch
) -> None:
    svc = _Service(tmp_path)
    real_submit = svc.service.submit_provision
    attempts = 0

    async def flaky(tenant_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise psycopg.errors.LockNotAvailable("mock transient lock timeout")
        await real_submit(tenant_id)

    monkeypatch.setattr(svc.service, "submit_provision", flaky)
    worker = TenantProvisioningWorker(svc.service, max_attempts=3, backoff=0.01)
    await worker.start()
    try:
        t = _tenant("wretry")
        svc.created.append(partition_name_for_tenant(t))
        assert await svc.service.request_provisioning(t) is PartitionStatus.PENDING
        await _wait_for(lambda: svc.service.status(t) is PartitionStatus.READY)
        assert attempts == 2  # 首次瞬态失败重试，第二次成功
        assert svc.service.created_count == 1
    finally:
        await worker.stop()
        svc.close()


@pytest.mark.postgres
async def test_worker_marks_failed_after_attempts_exhausted(
    pg_alive, tmp_path, monkeypatch
) -> None:
    svc = _Service(tmp_path)
    attempts = 0

    async def always_fail(tenant_id: str) -> None:
        nonlocal attempts
        attempts += 1
        raise psycopg.errors.LockNotAvailable("mock persistent lock timeout")

    monkeypatch.setattr(svc.service, "submit_provision", always_fail)
    worker = TenantProvisioningWorker(svc.service, max_attempts=3, backoff=0.01)
    await worker.start()
    try:
        t = _tenant("wexh")
        assert await svc.service.request_provisioning(t) is PartitionStatus.PENDING
        await _wait_for(lambda: svc.service.status(t) is PartitionStatus.FAILED)
        assert attempts == 3  # retryable 错误消耗全部重试
        assert t not in svc.service._queued
    finally:
        await worker.stop()
        svc.close()


@pytest.mark.postgres
async def test_worker_non_retryable_fails_immediately(
    pg_alive, tmp_path, monkeypatch
) -> None:
    svc = _Service(tmp_path)
    attempts = 0

    async def boom(tenant_id: str) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("programming error")

    monkeypatch.setattr(svc.service, "submit_provision", boom)
    worker = TenantProvisioningWorker(svc.service, max_attempts=3, backoff=0.01)
    await worker.start()
    try:
        t = _tenant("wnon")
        assert await svc.service.request_provisioning(t) is PartitionStatus.PENDING
        await _wait_for(lambda: svc.service.status(t) is PartitionStatus.FAILED)
        assert attempts == 1  # 非 psycopg.Error 不消耗重试，立即 FAILED
    finally:
        await worker.stop()
        svc.close()


@pytest.mark.postgres
async def test_worker_same_tenant_dedup(pg_alive, tmp_path, monkeypatch) -> None:
    svc = _Service(tmp_path)
    calls = 0
    max_inflight = 0
    inflight = 0

    async def record(tenant_id: str) -> None:
        nonlocal calls, max_inflight, inflight
        calls += 1
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.02)
        inflight -= 1

    monkeypatch.setattr(svc.service, "submit_provision", record)
    worker = TenantProvisioningWorker(svc.service, poll_interval=0.01)
    await worker.start()
    try:
        t = _tenant("wdedup")
        # 重复 request 幂等：只入队一次
        assert await svc.service.request_provisioning(t) is PartitionStatus.PENDING
        assert await svc.service.request_provisioning(t) is PartitionStatus.PENDING
        assert svc.service._queued == {t}
        await _wait_for(lambda: not svc.service._queued)
        assert calls == 1  # 同 tenant 恰一次在途处理（_in_flight 去重）
        assert max_inflight == 1
    finally:
        await worker.stop()
        svc.close()


@pytest.mark.postgres
async def test_worker_start_stop_restart(pg_alive, tmp_path) -> None:
    svc = _Service(tmp_path)
    worker = TenantProvisioningWorker(svc.service, poll_interval=0.01)
    await worker.start()
    await worker.start()  # 幂等：不重复创建 task
    assert worker._task is not None
    await worker.stop()
    assert worker._task is None
    # 重启后能再次消费队列
    await worker.start()
    try:
        t = _tenant("wlife")
        svc.created.append(partition_name_for_tenant(t))
        assert await svc.service.request_provisioning(t) is PartitionStatus.PENDING
        await _wait_for(lambda: svc.service.status(t) is PartitionStatus.READY)
        assert svc.service.created_count == 1
    finally:
        await worker.stop()
        svc.close()


@pytest.mark.postgres
async def test_worker_lock_timeout_fails_then_recovers_after_requeue(
    pg_alive, tmp_path
) -> None:
    # 真实锁超时：blocker 持有 advisory 锁，worker 短 lock_timeout 下耗尽尝试
    # → FAILED（事务已由 connection() 回滚）；释放锁 + request_provisioning
    # 重入队 → 收敛 READY。
    svc = _Service(tmp_path, lock_timeout="100ms")
    worker = TenantProvisioningWorker(
        svc.service, max_attempts=2, backoff=0.02, poll_interval=0.01
    )
    await worker.start()
    try:
        t = _tenant("wfail")
        svc.created.append(partition_name_for_tenant(t))
        blocker = psycopg.connect(PG_URL)
        try:
            blocker.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            assert await svc.service.request_provisioning(t) is PartitionStatus.PENDING
            await _wait_for(lambda: svc.service.status(t) is PartitionStatus.FAILED)
            assert t not in svc.service._queued
            assert svc.service.created_count == 0  # 从未 CREATE
        finally:
            blocker.rollback()
            blocker.close()
        # FAILED 可重入队；锁已释放 → worker 收敛 READY
        assert await svc.service.request_provisioning(t) is PartitionStatus.PENDING
        await _wait_for(lambda: svc.service.status(t) is PartitionStatus.READY)
        assert svc.service.created_count == 1
    finally:
        await worker.stop()
        svc.close()
