"""Tenant partition provisioning control seam（M4H-4）。

ADR 决策（m4h-4-partition-provisioning.md §1.2）：turn 路径只做两阶段
readiness gate（request_provisioning + require_ready），真正 DDL 由独立
control worker 经 ``StorageRuntime.run_db``（pool 连接 + 独立事务）执行；
store 写路径只 fail-fast，绝不执行 ``CREATE PARTITION``。

本模块只定义 seam 与状态机：``PartitionStatus``、``TenantProvisioning``
接口、哨兵异常。具体实现（命名规则、状态注册表、幂等 DDL 执行器、
worker）按 m4h-4 ADR §2 的 commit 2-5 落地。
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from enum import Enum
import re
from typing import Any, Protocol, runtime_checkable

from psycopg import sql as pgsql

from infra.storage.postgres_memory_store import PostgresMemoryBackend

__all__ = [
    "PartitionStatus",
    "PartitionNotReady",
    "PartitionProvisioningFailed",
    "TenantProvisioning",
    "TenantProvisioningService",
    "partition_name_for_tenant",
]

# 全局 advisory 锁：序列化跨进程/线程的分区创建。本模块是控制路径所有者；
# postgres_memory_store 里的同名常量在 commit 5 移除懒 DDL 时一并删除。
_PARTITION_LOCK_KEY = 872_001_457


class PartitionStatus(str, Enum):
    """tenant 分区的 provisioning 状态。

    UNKNOWN -> PENDING -> READY；PENDING -> FAILED（尝试耗尽）。
    FAILED 可经 request_provisioning 重新入队。
    """

    UNKNOWN = "unknown"
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class PartitionNotReady(Exception):
    """require_ready 时分区尚未 READY（PENDING 或探测缺失）。

    turn 路径 fail-fast 哨兵：``retryable=True`` 表达控制错误语义，调用方
    重试即可（worker 通常在毫秒级完成 provisioning）。
    """

    retryable = True
    error_type = "partition_not_ready"


class PartitionProvisioningFailed(Exception):
    """provisioning 尝试耗尽仍 FAILED，需人工介入。

    ``retryable=False``：重复请求不会自愈，应告警并检查 DDL 失败根因。
    """

    retryable = False
    error_type = "partition_provisioning_failed"


@runtime_checkable
class TenantProvisioning(Protocol):
    """provisioning control seam：turn gate 与 control worker 共用。

    - ``request_provisioning``：幂等提交 provisioning job，不执行 DDL；
      UNKNOWN 时先做只读 ``to_regclass`` 探测，已存在则直接恢复 READY。
    - ``require_ready``：只读检查 READY/PENDING/FAILED，不执行 DDL；
      PENDING 抛 ``PartitionNotReady``，FAILED 抛 ``PartitionProvisioningFailed``。
    - ``provision_tenant``：同步幂等 DDL 执行器，仅由 control worker 经
      ``StorageRuntime.run_db`` 调用（pool 连接 + 独立事务 + rollback）。
    """

    async def request_provisioning(self, tenant_id: str) -> PartitionStatus: ...

    async def require_ready(self, tenant_id: str) -> None: ...

    def provision_tenant(self, tenant_id: str) -> None: ...


def partition_name_for_tenant(tenant_id: str) -> str:
    """稳定 memory_items 分区名：可读前缀 + 48-bit 唯一后缀（ADR §1.4）。

    有损 sanitize 只用于可读前缀（[:30]），唯一性由 ``md5(原始 tenant_id)[:12]``
    保证（5000 tenant 碰撞概率 ~4e-8），不再依赖有损字符替换；总长 ≤ 56 ≤ PG 63
    字符标识符限制。分区 bound 值仍是原始 tenant_id（``sql.Literal``），
    本函数只决定分区标识符。
    """
    readable = re.sub(r"[^A-Za-z0-9_]", "_", tenant_id)[:30]
    digest = hashlib.md5(tenant_id.encode("utf-8")).hexdigest()[:12]
    return f"memory_items_{readable}_{digest}"


class TenantProvisioningService:
    """provisioning control path：状态注册表 + 幂等 DDL 同步执行器（ADR §1.3）。

    - ``request_provisioning``：幂等提交（UNKNOWN 先只读 ``to_regclass`` 探测
      恢复，缺失 → PENDING + 入队；FAILED 可重新入队）。
    - ``require_ready``：只读检查，READY 通过，PENDING/FAILED 抛哨兵。
    - ``provision_tenant``：同步幂等 DDL 执行器，仅由 control worker 经
      ``StorageRuntime.run_db`` 调用；pool 连接 + ``SET LOCAL lock_timeout`` +
      全局 advisory 锁 + ``to_regclass`` 双检 + ``CREATE PARTITION`` + commit。
      事务失败由 M4H-3 ``connection()`` 包装 rollback，无 aborted transaction /
      半初始化态；缓存只在 commit 后更新。

    状态注册表单进程内存态（``worker_replicas=1``）；多进程持久化状态留 M7。
    commit 4 在此之上加独立 worker（消费 ``_queued``、retry/backoff）。
    """

    def __init__(
        self,
        memory_backend: PostgresMemoryBackend,
        *,
        run_db: Callable[..., Awaitable[Any]],
        lock_timeout: str = "2s",
    ) -> None:
        self._backend = memory_backend
        self._run_db = run_db
        self._lock_timeout = lock_timeout
        self._states: dict[str, PartitionStatus] = {}
        self._queued: set[str] = set()
        # 实际执行过 CREATE 的次数（advisory 锁双检保证同 tenant 恰一次）。
        self.created_count = 0

    # ── gate（turn 路径调用，不执行 DDL）────────────────────────────────

    async def request_provisioning(self, tenant_id: str) -> PartitionStatus:
        """幂等提交 provisioning job；不执行 DDL，返回提交后状态。"""
        current = self._states.get(tenant_id)
        if current is PartitionStatus.READY or current is PartitionStatus.PENDING:
            return current
        # FAILED / UNKNOWN：只读探测恢复已存在的分区；缺失则重新入队。
        exists = await self._run_db(self._partition_exists, tenant_id)
        if exists:
            self._mark_ready(tenant_id)
            return PartitionStatus.READY
        self._states[tenant_id] = PartitionStatus.PENDING
        self._queued.add(tenant_id)
        return PartitionStatus.PENDING

    async def require_ready(self, tenant_id: str) -> None:
        """只读检查；READY 通过，否则抛哨兵（PENDING 可重试，FAILED 不重试）。"""
        current = self._states.get(tenant_id)
        if current is PartitionStatus.READY:
            return
        if current is PartitionStatus.PENDING:
            raise PartitionNotReady(f"tenant {tenant_id} partition not ready (pending)")
        if current is PartitionStatus.FAILED:
            raise PartitionProvisioningFailed(f"tenant {tenant_id} provisioning failed")
        raise PartitionNotReady(f"tenant {tenant_id} partition not provisioned")

    # ── 幂等 DDL 执行器（control worker 经 run_db 调用）─────────────────

    def provision_tenant(self, tenant_id: str) -> None:
        """同步幂等 provisioning DDL；恰好一次有效 CREATE，事务安全。"""
        if self._states.get(tenant_id) is PartitionStatus.READY:
            return
        with self._backend._lock, self._backend.connection():
            self._backend._check_open()
            if self._states.get(tenant_id) is PartitionStatus.READY:
                return
            conn = self._backend.conn
            # 限界 advisory 锁等待，避免 provisioning 延迟不可控（GUC 不接受
            # bind 参数，经 set_config 参数化；is_local=true 即事务内 SET LOCAL）
            conn.execute("SELECT set_config('lock_timeout', %s, true)", (self._lock_timeout,))
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PARTITION_LOCK_KEY,))
            name = partition_name_for_tenant(tenant_id)
            row = conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()
            if row is None or row[0] is None:
                conn.execute(
                    pgsql.SQL(
                        "CREATE TABLE {} PARTITION OF memory_items FOR VALUES IN ({})"
                    ).format(
                        pgsql.Identifier(name),
                        pgsql.Literal(tenant_id),
                    )
                )
                self.created_count += 1
            conn.commit()
            # 缓存与状态在 commit 成功后、释放 RLock 前更新：崩溃于 commit 与
            # 缓存之间时，下次探测 to_regclass 直接恢复 READY，不重复 CREATE。
            self._backend._partitions_known.add(name)
            self._states[tenant_id] = PartitionStatus.READY

    # ── 内部 ────────────────────────────────────────────────────────────

    def _partition_exists(self, tenant_id: str) -> bool:
        """只读 catalog 探测：分区是否已存在（request_provisioning 恢复用）。"""
        with self._backend.connection():
            row = self._backend.conn.execute(
                "SELECT to_regclass(%s)", (partition_name_for_tenant(tenant_id),)
            ).fetchone()
            return bool(row and row[0] is not None)

    def _mark_ready(self, tenant_id: str) -> None:
        self._backend._partitions_known.add(partition_name_for_tenant(tenant_id))
        self._states[tenant_id] = PartitionStatus.READY

    def status(self, tenant_id: str) -> PartitionStatus:
        return self._states.get(tenant_id, PartitionStatus.UNKNOWN)
