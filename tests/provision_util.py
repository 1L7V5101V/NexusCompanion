"""共享 PG 测试前置：直接创建 tenant 分区（M4H-4 commit 5）。

Parent ``memory_items`` 分区父表由 alembic 迁移建好；本 helper 只按
provisioning 命名规则建对应 tenant 分区，供各测试文件在写 memory 前调用。
不依赖 provisioning service（保持测试与 control path 解耦）。
"""

import psycopg
from psycopg import sql as pgsql

from infra.storage.partitioning import partition_name_for_tenant


def provision_partition(pg_url: str, tenant: str) -> str:
    """幂等建分区；返回分区名。命名与生产 control path 完全一致。"""
    name = partition_name_for_tenant(tenant)
    conn = psycopg.connect(pg_url)
    try:
        conn.autocommit = True
        conn.execute(
            pgsql.SQL(
                "CREATE TABLE IF NOT EXISTS {} PARTITION OF memory_items "
                "FOR VALUES IN ({})"
            ).format(pgsql.Identifier(name), pgsql.Literal(tenant))
        )
    finally:
        conn.close()
    return name
