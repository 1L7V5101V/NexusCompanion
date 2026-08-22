"""Validate the recommended mitigation in production form: native PG partitioning
by tenant_id + partitioned HNSW index.

Full-run finding: a single global HNSW index + WHERE tenant_id filter degrades
recall@10 from 0.987 (50% share) to ~0.10-0.13 (<=1% share) at ef=40, and the
planner falls back to seq scan (exact but ~230ms) for the smallest tenant.
A per-tenant TABLE control restored recall to 0.99-1.00. Here we test the actual
production shape: one partitioned table (LIST by tenant), HNSW index on the
parent -> per-partition indexes, query filtered by tenant_id (partition pruning).
"""

import time
import psycopg2

DIM = 128
K = 10
NQ = 30
TENANTS = ["t_50pct", "t_02pct", "t_01pct", "t_001pct", "t_0005pct"]
DBURL = "postgresql://postgres@localhost:5432/vecbench"


def main():
    conn = psycopg2.connect(DBURL)
    conn.autocommit = True
    cur = conn.cursor()

    # ---- build partitioned table from existing items ----
    cur.execute("DROP TABLE IF EXISTS items_part")
    cur.execute(
        "CREATE TABLE items_part (tenant_id text NOT NULL, id bigint NOT NULL, "
        f"embedding vector({DIM}) NOT NULL) PARTITION BY LIST (tenant_id)")
    cur.execute("SELECT DISTINCT tenant_id FROM items")
    tids = [r[0] for r in cur.fetchall()]
    for tid in tids:
        cur.execute(f"CREATE TABLE items_part_{tid} PARTITION OF items_part FOR VALUES IN (%s)",
                    (tid,))
    cur.execute("INSERT INTO items_part (id, tenant_id, embedding) SELECT id, tenant_id, embedding FROM items")
    t0 = time.time()
    cur.execute(
        "CREATE INDEX ON items_part USING hnsw (embedding vector_cosine_ops) "
        "WITH (m=16, ef_construction=64)")
    print(f"[index] partitioned HNSW built in {time.time()-t0:.1f}s", flush=True)
    cur.execute("ANALYZE items_part")
    for tid in tids:
        cur.execute(f"ANALYZE items_part_{tid}")

    def topk_exact(tid, qv, k=K):
        cur.execute(
            "SELECT id FROM items WHERE tenant_id=%s ORDER BY embedding <=> %s::vector LIMIT %s",
            (tid, qv, k))
        return [r[0] for r in cur.fetchall()]

    def topk_part(tid, qv, k=K):
        cur.execute(
            "SELECT id FROM items_part WHERE tenant_id=%s ORDER BY embedding <=> %s::vector LIMIT %s",
            (tid, qv, k))
        return [r[0] for r in cur.fetchall()]

    def plan_of(tid, qv):
        cur.execute("EXPLAIN SELECT id FROM items_part WHERE tenant_id=%s "
                    "ORDER BY embedding <=> %s::vector LIMIT 10", (tid, qv))
        text = "\n".join(r[0] for r in cur.fetchall())
        return "\n".join(l.strip() for l in text.splitlines())

    def recall(ex, ap):
        return len(set(ex) & set(ap)) / K

    # sample queries (reproducible)
    cur.execute("SELECT setseed(0.5)")
    queries = {}
    for tid in TENANTS:
        cur.execute("SELECT embedding::text FROM items WHERE tenant_id=%s "
                    "ORDER BY random() LIMIT %s", (tid, NQ))
        queries[tid] = [r[0] for r in cur.fetchall()]

    # exact ground truth via forced seq scan (on items)
    cur.execute("SET enable_indexscan = off")
    cur.execute("SET enable_bitmapscan = off")
    exact = {}
    for tid, qs in queries.items():
        exact[tid] = [topk_exact(tid, qv) for qv in qs]
    cur.execute("SET enable_indexscan = on")
    cur.execute("SET enable_bitmapscan = on")

    # partitioned-approx: recall + latency + plan, across ef
    cur.execute("SET hnsw.ef_search = 40")
    print(f"\n{'tenant':<12}{'rows':>7}{'plan':>38}   recall@10  ms/q", flush=True)
    for tid in TENANTS:
        cur.execute("SELECT count(*) FROM items_part WHERE tenant_id=%s", (tid,))
        nrows = cur.fetchone()[0]
        plan = plan_of(tid, queries[tid][0]).splitlines()[0]
        ap_all, lat = [], []
        for qv in queries[tid]:
            t0 = time.time()
            ap_all.append(topk_part(tid, qv))
            lat.append(time.time() - t0)
        rec = sum(recall(e, a) for e, a in zip(exact[tid], ap_all)) / NQ
        ms = sum(lat) / len(lat) * 1000
        print(f"{tid:<12}{nrows:>7}{plan:>38}   {rec:>7.3f}  {ms:>5.2f}", flush=True)

    conn.close()


if __name__ == "__main__":
    main()
