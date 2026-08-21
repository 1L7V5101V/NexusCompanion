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

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from enum import Enum
import re
from typing import Any, Protocol, runtime_checkable

import psycopg
from psycopg import sql as pgsql

from infra.storage.postgres_memory_store import PostgresMemoryBackend

__all__ = [
    "PartitionStatus",
    "PartitionNotReady",
    "PartitionProvisioningFailed",
    "TenantProvisioning",
    "TenantProvisioningService",
    "TenantProvisioningWorker",
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

    async def submit_provision(self, tenant_id: str) -> None:
        """把同步幂等 DDL 提交到 bounded executor（control worker 专用）。

        provision_tenant 是同步执行器；worker（async）经 ``run_db`` 让它
        在池线程执行，DDL 与锁等待都不占用 event loop。
        """
        await self._run_db(self.provision_tenant, tenant_id)

    def mark_failed(self, tenant_id: str) -> None:
        """PENDING → FAILED（worker 尝试耗尽）；FAILED 可经 request_provisioning 重入队。"""
        self._states[tenant_id] = PartitionStatus.FAILED

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


class TenantProvisioningWorker:
    """独立 control worker：消费 ``_queued``，经 ``run_db`` 执行幂等 DDL（ADR §1.3）。

    - 单 async task 轮询队列；同 tenant 由 ``_in_flight`` 去重（恰一次在途），
      异 tenant 并行 dispatch（全局 advisory 锁在 DDL 层串行化）。
    - 失败分类：``psycopg.Error``（advisory 锁超时等瞬态）→ backoff 重试至
      ``max_attempts`` 后置 FAILED；其余异常非重试 → 立即 FAILED。事务失败已由
      M4H-3 ``connection()`` 包装 rollback，worker 只观察并更新状态。
    - 成功由 ``provision_tenant`` 置 READY；FAILED 可经 ``request_provisioning``
      重新入队。stop 时在途 tenant 未 resolve 仍留 ``_queued``，重启后重处理。
    """

    def __init__(
        self,
        service: TenantProvisioningService,
        *,
        poll_interval: float = 0.05,
        max_attempts: int = 3,
        backoff: float = 0.05,
    ) -> None:
        self._service = service
        self._poll_interval = poll_interval
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._in_flight: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """启动 worker task；已在运行时 no-op。"""
        if self._task is None:
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run(), name="tenant-provisioner")

    async def stop(self) -> None:
        """优雅停止：置停止标志，等待在途任务完成（不取消执行中的 DDL）。"""
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await self._task
        finally:
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            await self._drain_once()
            if not self._service._queued:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass

    async def _drain_once(self) -> None:
        batch = list(self._service._queued - self._in_flight)
        if not batch:
            return
        self._in_flight.update(batch)
        try:
            await asyncio.gather(
                *(self._process(tenant_id) for tenant_id in batch),
                return_exceptions=True,
            )
        finally:
            # 批结束即清在途（_drain_once 串行，无并发冲突）；未 resolve 的
            # tenant 仍在 _queued（_process 只在成功/FAILED 时出队），
            # 重启 worker 会重新处理。
            self._in_flight.clear()

    async def _process(self, tenant_id: str) -> None:
        for attempt in range(1, self._max_attempts + 1):
            try:
                await self._service.submit_provision(tenant_id)
                self._service._queued.discard(tenant_id)
                return
            except Exception as exc:
                retryable = isinstance(exc, psycopg.Error)
                if not retryable or attempt == self._max_attempts:
                    self._service.mark_failed(tenant_id)
                    self._service._queued.discard(tenant_id)
                    return
                await asyncio.sleep(self._backoff * attempt)
