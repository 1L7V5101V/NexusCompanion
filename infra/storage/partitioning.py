"""分区共享叶模块：哨兵异常 + 稳定分区命名（M4H-4 commit 5）。

只允许 stdlib 依赖，供 ``provisioning``（control path）与
``postgres_memory_store``（写路径 fail-fast）共同导入，避免两者互相依赖。
"""

from __future__ import annotations

import hashlib
import re


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
