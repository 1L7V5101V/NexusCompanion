"""M4H-4 幂等 provisioning 实现测试（commit 3，ADR §1.3）。

覆盖：provision_tenant 幂等 DDL（同/异 tenant 并发恰一次有效 CREATE）、
两阶段 gate（request_provisioning / require_ready，含 FAILED 重新入队）、
advisory 锁超时失败回滚与连接恢复、进程重启后的只读探测恢复。
postgres 用例依赖本地 PG，无 PG 自动 skip。
"""
import os
import threading

import psycopg
import pytest

from infra.storage import provisioning as provisioning_mod
from infra.storage.provisioning import (
    PartitionNotReady,
    PartitionProvisioningFailed,
    PartitionStatus,
    TenantProvisioningService,
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
        pytest.skip(f"本地 PG 不可用（{PG_URL}），跳过 postgres provisioning 测试")


def _tenant(tag: str) -> str:
    return f"prov_{tag}_{os.getpid()}"


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


@pytest.mark.postgres
def test_provision_tenant_creates_partition(pg_alive, tmp_path) -> None:
    svc = _Service(tmp_path)
    try:
        t = _tenant("basic")
        svc.created.append(partition_name_for_tenant(t))
        svc.service.provision_tenant(t)
        assert svc.service.status(t) is PartitionStatus.READY
        assert svc.service.created_count == 1
        conn = psycopg.connect(PG_URL)
        try:
            row = conn.execute("SELECT to_regclass(%s)", (partition_name_for_tenant(t),)).fetchone()
            assert row and row[0] is not None
            # 是 memory_items 的直接子分区（继承分区 + HNSW 索引）
            parent = conn.execute(
                "SELECT count(*) FROM pg_inherits WHERE inhrelid = %s::regclass "
                "AND inhparent = 'memory_items'::regclass",
                (partition_name_for_tenant(t),),
            ).fetchone()
            assert parent and parent[0] == 1
        finally:
            conn.close()
    finally:
        svc.close()


@pytest.mark.postgres
def test_provision_tenant_idempotent(pg_alive, tmp_path) -> None:
    svc = _Service(tmp_path)
    try:
        t = _tenant("idem")
        svc.created.append(partition_name_for_tenant(t))
        svc.service.provision_tenant(t)
        svc.service.provision_tenant(t)
        svc.service.provision_tenant(t)
        assert svc.service.created_count == 1
        assert svc.service.status(t) is PartitionStatus.READY
    finally:
        svc.close()


@pytest.mark.postgres
def test_concurrent_provision_same_tenant_single_ddl(pg_alive, tmp_path) -> None:
    svc = _Service(tmp_path)
    try:
        t = _tenant("same")
        svc.created.append(partition_name_for_tenant(t))
        barrier = threading.Barrier(6)
        errors: list[BaseException] = []

        def run() -> None:
            try:
                barrier.wait()
                svc.service.provision_tenant(t)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert not errors
        assert svc.service.created_count == 1  # advisory 锁双检保证恰一次 DDL
        assert svc.service.status(t) is PartitionStatus.READY
    finally:
        svc.close()


@pytest.mark.postgres
def test_concurrent_provision_different_tenants(pg_alive, tmp_path) -> None:
    svc = _Service(tmp_path)
    try:
        tenants = [_tenant(f"diff{i}") for i in range(6)]
        svc.created.extend(partition_name_for_tenant(x) for x in tenants)
        errors: list[BaseException] = []

        def run(tenant_id: str) -> None:
            try:
                svc.service.provision_tenant(tenant_id)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(x,)) for x in tenants]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert not errors
        assert svc.service.created_count == len(tenants)  # 每个 tenant 各一次
        for x in tenants:
            assert svc.service.status(x) is PartitionStatus.READY
    finally:
        svc.close()


@pytest.mark.postgres
def test_provision_failure_rolls_back_and_recovers(pg_alive, tmp_path) -> None:
    # lock_timeout 很短：另一事务持有 advisory 锁时 provision 快速失败，
    # 验证失败回滚（无 aborted transaction）且连接可恢复、重试成功。
    svc = _Service(tmp_path, lock_timeout="200ms")
    try:
        t = _tenant("fail")
        svc.created.append(partition_name_for_tenant(t))
        blocker = psycopg.connect(PG_URL)
        try:
            blocker.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            with pytest.raises(psycopg.Error):
                svc.service.provision_tenant(t)
            assert svc.service.status(t) is not PartitionStatus.READY
        finally:
            blocker.rollback()
            blocker.close()
        svc.service.provision_tenant(t)
        assert svc.service.status(t) is PartitionStatus.READY
        assert svc.service.created_count == 1
    finally:
        svc.close()


@pytest.mark.postgres
async def test_request_provisioning_and_require_ready(pg_alive, tmp_path) -> None:
    svc = _Service(tmp_path)
    try:
        t = _tenant("gate")
        svc.created.append(partition_name_for_tenant(t))
        assert await svc.service.request_provisioning(t) is PartitionStatus.PENDING
        assert svc.service._queued == {t}
        with pytest.raises(PartitionNotReady):
            await svc.service.require_ready(t)
        svc.service.provision_tenant(t)
        await svc.service.require_ready(t)  # READY 通过
        # READY 后 request 幂等返回 READY，不重复入队
        assert await svc.service.request_provisioning(t) is PartitionStatus.READY
        assert svc.service._queued == {t}
    finally:
        svc.close()


@pytest.mark.postgres
async def test_request_provisioning_recovers_existing_partition(pg_alive, tmp_path) -> None:
    svc = _Service(tmp_path)
    try:
        t = _tenant("restart")
        svc.created.append(partition_name_for_tenant(t))
        svc.service.provision_tenant(t)
        # 模拟进程重启：同一 backend 上新建 service（状态注册表清空），
        # request_provisioning 经只读 to_regclass 探测恢复 READY，不 fail-fast。
        memory_backend = svc.runtime._memory_backend
        assert memory_backend is not None
        svc2 = TenantProvisioningService(
            memory_backend,
            run_db=svc.runtime.run_db,
        )
        assert svc2.status(t) is PartitionStatus.UNKNOWN
        assert await svc2.request_provisioning(t) is PartitionStatus.READY
        await svc2.require_ready(t)
        assert svc2._queued == set()
    finally:
        svc.close()


@pytest.mark.postgres
async def test_failed_tenant_reenqueues_on_request(pg_alive, tmp_path) -> None:
    svc = _Service(tmp_path)
    try:
        t = _tenant("failed")
        svc.created.append(partition_name_for_tenant(t))
        svc.service._states[t] = PartitionStatus.FAILED
        with pytest.raises(PartitionProvisioningFailed):
            await svc.service.require_ready(t)
        # FAILED 可经 request_provisioning 重新入队（ADR §1.3）
        assert await svc.service.request_provisioning(t) is PartitionStatus.PENDING
        assert t in svc.service._queued
    finally:
        svc.close()
