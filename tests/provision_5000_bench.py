"""M4H-4 commit 6：5000 tenant provisioning 基准 harness（ADR §2 commit 6）。

独立脚本（非 pytest 用例，pytest 不收集 provision_*_bench.py），显式运行：

    python tests/provision_5000_bench.py [--tenants N] [--pool-size N]
        [--poll-interval S] [--out PATH] [--keep]

记录指标：
- sequential：逐个 request_provisioning + 等待 READY 的每 tenant 延迟（p50/p95/p99）。
- concurrent_burst：一次性入队全部 tenant，worker 全量收敛总耗时。
- catalog：provisioning 后 pg_inherits 分区数与 catalog 查询延迟。
- planning：目标 tenant 查询的 plan 延迟（EXPLAIN FORMAT JSON）。
- pruning：EXPLAIN 证明 WHERE tenant_id 只扫描目标分区（partition pruning）。
- failure_retry：advisory 锁被占用 → 尝试耗尽 FAILED → 释放锁重入队收敛 READY。

结果写入 --out（默认 results/m4h4_partition_provisioning.json），默认运行后清理
创建的分区（--keep 保留供检查）。依赖本地 PG（NEXUS_TEST_PG_URL，
默认 postgresql://nexus:nexus_dev@localhost:5433/nexus）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql as pgsql

# 独立脚本：把仓库根加入 sys.path，使 infra/ 可直接导入。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.storage.provisioning import (
    PartitionStatus,
    TenantProvisioningService,
    TenantProvisioningWorker,
    partition_name_for_tenant,
)
from infra.storage.runtime import StorageRuntime

PG_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)
_LOCK_KEY = 872_001_457


async def _wait_for(predicate, timeout: float = 300.0, step: float = 0.01) -> None:
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return
        await asyncio.sleep(step)
        elapsed += step
    raise TimeoutError("等待超时")


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(len(ordered) * p / 100))
    return ordered[idx]


def _summarize(latencies_ms: list[float]) -> dict[str, float]:
    return {
        "count": len(latencies_ms),
        "total_s": sum(latencies_ms) / 1000,
        "mean_ms": round(statistics.mean(latencies_ms), 3),
        "p50_ms": round(_percentile(latencies_ms, 50), 3),
        "p95_ms": round(_percentile(latencies_ms, 95), 3),
        "p99_ms": round(_percentile(latencies_ms, 99), 3),
        "max_ms": round(max(latencies_ms), 3),
    }


def _assert_parent_exists() -> None:
    conn = psycopg.connect(PG_URL)
    try:
        row = conn.execute("SELECT to_regclass(%s)", ("memory_items",)).fetchone()
    finally:
        conn.close()
    if row is None or row[0] is None:
        raise RuntimeError(
            "memory_items 分区父表不存在：请先应用分区迁移 "
            "（alembic a3d5c7e9f1b2_partition_memory_items）"
        )


def _drop_partitions(names: list[str]) -> None:
    conn = psycopg.connect(PG_URL)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for name in names:
                cur.execute(
                    pgsql.SQL("DROP TABLE IF EXISTS {}").format(pgsql.Identifier(name))
                )
    finally:
        conn.close()


def _collect_relations(node: Any) -> list[str]:
    relations: list[str] = []

    def walk(item: Any) -> None:
        if not isinstance(item, dict):
            return
        rel = item.get("Relation Name")
        if rel:
            relations.append(str(rel))
        for child in item.get("Plans") or []:
            walk(child)

    walk(node.get("Plan", {}))
    return relations


class _ProvisionBench:
    """主基准：sequential + concurrent burst + catalog + planning + pruning。"""

    def __init__(self, count: int, pool_size: int, poll_interval: float) -> None:
        self.count = count
        self.runtime = StorageRuntime(
            PG_URL, "unused.db", "unused.db", pool_size=pool_size
        )
        backend = self.runtime.memory_backend
        assert backend is not None
        self.service = TenantProvisioningService(backend, run_db=self.runtime.run_db)
        self.worker = TenantProvisioningWorker(
            self.service, poll_interval=poll_interval
        )
        self.created: list[tuple[str, str]] = []  # (tenant_id, partition_name)
        self._baseline_partitions = self._partition_count()

    def _partition_count(self) -> int:
        conn = psycopg.connect(PG_URL)
        try:
            row = conn.execute(
                "SELECT count(*) FROM pg_inherits WHERE inhparent = 'memory_items'::regclass"
            ).fetchone()
            return int(row[0])
        finally:
            conn.close()

    async def run(self) -> dict[str, Any]:
        await self.worker.start()
        try:
            metrics: dict[str, Any] = {}
            metrics["sequential"] = await self._bench_sequential()
            metrics["concurrent_burst"] = await self._bench_burst()
            metrics["catalog"] = self._bench_catalog()
            sample = self._sample_tenant()
            metrics["planning"] = self._bench_planning(sample)
            metrics["pruning"] = self._bench_pruning(sample)
            return metrics
        finally:
            await self.worker.stop()
            self.runtime.close()

    async def _bench_sequential(self) -> dict[str, float]:
        latencies: list[float] = []
        for i in range(self.count):
            tenant = f"bench_seq_{i}_{os.getpid()}"
            self.created.append((tenant, partition_name_for_tenant(tenant)))
            t0 = time.perf_counter()
            await self.service.request_provisioning(tenant)
            await _wait_for(
                lambda t=tenant: self.service.status(t) is PartitionStatus.READY,
                timeout=30.0,
                step=0.002,
            )
            latencies.append((time.perf_counter() - t0) * 1000)
        return _summarize(latencies)

    async def _bench_burst(self) -> dict[str, Any]:
        tenants = [f"bench_burst_{i}_{os.getpid()}" for i in range(self.count)]
        for tenant in tenants:
            self.created.append((tenant, partition_name_for_tenant(tenant)))
            await self.service.request_provisioning(tenant)
        t0 = time.perf_counter()
        await _wait_for(
            lambda: all(
                self.service.status(t) is PartitionStatus.READY for t in tenants
            ),
            timeout=600.0,
            step=0.05,
        )
        total_s = time.perf_counter() - t0
        return {
            "count": self.count,
            "total_s": round(total_s, 3),
            "per_tenant_mean_ms": round(total_s * 1000 / self.count, 3),
        }

    def _bench_catalog(self) -> dict[str, Any]:
        conn = psycopg.connect(PG_URL)
        try:
            t0 = time.perf_counter()
            row = conn.execute(
                "SELECT count(*) FROM pg_inherits WHERE inhparent = 'memory_items'::regclass"
            ).fetchone()
            lookup_ms = (time.perf_counter() - t0) * 1000
            after = int(row[0])
            return {
                "baseline": self._baseline_partitions,
                "after": after,
                "delta": after - self._baseline_partitions,
                "lookup_ms": round(lookup_ms, 3),
            }
        finally:
            conn.close()

    def _sample_tenant(self) -> str:
        if not self.created:
            raise RuntimeError("无已 provisioning tenant，无法采样")
        return self.created[-1][0]

    def _bench_planning(self, tenant: str) -> dict[str, Any]:
        conn = psycopg.connect(PG_URL)
        try:
            t0 = time.perf_counter()
            row = conn.execute(
                "EXPLAIN (FORMAT JSON) "
                "SELECT id, summary FROM memory_items "
                "WHERE tenant_id=%s AND memory_type=%s AND status='active' "
                "ORDER BY created_at DESC LIMIT 10",
                (tenant, "note"),
            ).fetchone()
            plan_ms = (time.perf_counter() - t0) * 1000
            return {"plan_latency_ms": round(plan_ms, 3)}
        finally:
            conn.close()

    def _bench_pruning(self, tenant: str) -> dict[str, Any]:
        name = partition_name_for_tenant(tenant)
        conn = psycopg.connect(PG_URL)
        try:
            row = conn.execute(
                "EXPLAIN (FORMAT JSON) "
                "SELECT id FROM memory_items WHERE tenant_id=%s AND status='active'",
                (tenant,),
            ).fetchone()
            payload = row[0]
            plan_dict = payload[0] if isinstance(payload, list) else payload
            relations = _collect_relations(plan_dict)
            return {
                "scanned_relations": relations,
                "selected_count": len(relations),
                "pruned_to_single_partition": relations == [name],
            }
        finally:
            conn.close()


class _RetryBench:
    """失败重试收敛：advisory 锁占用 → FAILED → 释放重入队 → READY。"""

    def __init__(self) -> None:
        self.runtime = StorageRuntime(PG_URL, "unused.db", "unused.db", pool_size=2)
        backend = self.runtime.memory_backend
        assert backend is not None
        self.service = TenantProvisioningService(
            backend, run_db=self.runtime.run_db, lock_timeout="100ms"
        )
        self.worker = TenantProvisioningWorker(
            self.service,
            max_attempts=2,
            backoff=0.02,
            poll_interval=0.01,
        )
        self.tenant = f"bench_retry_{os.getpid()}"
        self.name = partition_name_for_tenant(self.tenant)

    async def run(self) -> dict[str, float]:
        await self.worker.start()
        try:
            blocker = psycopg.connect(PG_URL)
            try:
                blocker.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
                await self.service.request_provisioning(self.tenant)
                t0 = time.perf_counter()
                await _wait_for(
                    lambda: self.service.status(self.tenant)
                    is PartitionStatus.FAILED,
                    timeout=30.0,
                )
                failed_ms = (time.perf_counter() - t0) * 1000
            finally:
                blocker.rollback()
                blocker.close()
            await self.service.request_provisioning(self.tenant)
            t0 = time.perf_counter()
            await _wait_for(
                lambda: self.service.status(self.tenant) is PartitionStatus.READY,
                timeout=30.0,
            )
            converge_ms = (time.perf_counter() - t0) * 1000
            return {
                "attempts_to_fail": 2,
                "failed_after_ms": round(failed_ms, 3),
                "requeue_converged_ms": round(converge_ms, 3),
            }
        finally:
            await self.worker.stop()
            self.runtime.close()


def _print_summary(metrics: dict[str, Any]) -> None:
    seq = metrics["sequential"]
    burst = metrics["concurrent_burst"]
    print("=== M4H-4 5000 tenant provisioning 基准 ===")
    print(f"sequential:   count={seq['count']} total={seq['total_s']:.1f}s "
          f"mean={seq['mean_ms']:.1f}ms p50={seq['p50_ms']:.1f}ms "
          f"p95={seq['p95_ms']:.1f}ms p99={seq['p99_ms']:.1f}ms")
    print(f"burst:        count={burst['count']} total={burst['total_s']:.1f}s "
          f"per-tenant={burst['per_tenant_mean_ms']:.2f}ms")
    catalog = metrics["catalog"]
    print(f"catalog:      baseline={catalog['baseline']} after={catalog['after']} "
          f"delta=+{catalog['delta']} lookup={catalog['lookup_ms']:.2f}ms")
    planning = metrics["planning"]
    print(f"planning:     plan_latency={planning['plan_latency_ms']:.2f}ms")
    pruning = metrics["pruning"]
    print(f"pruning:      scanned={pruning['selected_count']} "
          f"single_partition={pruning['pruned_to_single_partition']}")
    retry = metrics["failure_retry"]
    print(f"failure_retry: failed_after={retry['failed_after_ms']:.1f}ms "
          f"requeue_converged={retry['requeue_converged_ms']:.1f}ms")


def main() -> None:
    parser = argparse.ArgumentParser(description="5000 tenant provisioning 基准")
    parser.add_argument("--tenants", type=int, default=int(os.environ.get("BENCH_TENANTS", "5000")))
    parser.add_argument("--pool-size", type=int, default=20)
    parser.add_argument("--poll-interval", type=float, default=0.01)
    parser.add_argument("--out", default="results/m4h4_partition_provisioning.json")
    parser.add_argument("--keep", action="store_true", help="不清理创建的分区")
    args = parser.parse_args()

    _assert_parent_exists()
    bench = _ProvisionBench(args.tenants, args.pool_size, args.poll_interval)
    metrics = asyncio.run(bench.run())
    retry = _RetryBench()
    metrics["failure_retry"] = asyncio.run(retry.run())

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "bench": "m4h4_partition_provisioning",
        "config": {
            "tenants": args.tenants,
            "pool_size": args.pool_size,
            "poll_interval": args.poll_interval,
            "pg_url_host": PG_URL.split("@")[-1],
        },
        "metrics": metrics,
    }
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _print_summary(metrics)
    print(f"结果写入 {out_path}")

    if not args.keep:
        _drop_partitions([name for _, name in bench.created] + [retry.name])
        print(f"已清理 {len(bench.created) + 1} 个创建的分区")


if __name__ == "__main__":
    main()
