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
from enum import Enum
import re
from typing import Protocol, runtime_checkable

__all__ = [
    "PartitionStatus",
    "PartitionNotReady",
    "PartitionProvisioningFailed",
    "TenantProvisioning",
    "partition_name_for_tenant",
]


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

    def request_provisioning(self, tenant_id: str) -> PartitionStatus: ...

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
