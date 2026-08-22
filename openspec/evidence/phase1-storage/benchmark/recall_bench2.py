"""Benchmark v2: pgvector HNSW recall under tenant_id filtering, realistic data.

v1 used uniform random vectors -> all near-orthogonal -> no meaningful nearest
neighbor structure, so HNSW recall was garbage (0.33 global) and results were
uninterpretable. v2 generates cluster-structured data (realistic embeddings):
G random centroids, each item = normalize(centroid + sigma*noise). Tenants are
assigned uniformly at random (adversarial: tenant is NOT vector-separable).

Answers:
  1. Does global HNSW recall hold without filter? (index sanity check)
  2. What does production actually do -- WHERE tenant_id=? ORDER BY embedding<=>q
     -- across tenant selectivity, in recall AND plan (index vs seq scan)?
  3. Does a per-tenant HNSW index restore recall for small tenants? (the
     mitigation control: if global+filter recall << per-tenant recall for the
     same tenant data, the degradation is a traversal artifact, not data sparsity)
"""

import argparse
import time
import numpy as np
import psycopg2
from psycopg2.extras import execute_values

TENANTS = [  # shares sum to 0.8885; remainder 0.1115 -> t_rest (unmeasured)
    ("t_50pct", 0.50),
    ("t_20pct", 0.20),
    ("t_10pct", 0.10),
    ("t_05pct", 0.05),
    ("t_02pct", 0.02),
    ("t_01pct", 0.01),
    ("t_005pct", 0.005),
    ("t_002pct", 0.002),
    ("t_001pct", 0.001),
    ("t_0005pct", 0.0005),
]
REST = "t_rest"

DBURL = "postgresql://postgres@localhost:5432/vecbench"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--total", type=int, default=150_000)
    ap.add_argument("--g", type=int, default=2000, help="number of centroids")
    ap.add_argument("--nq", type=int, default=30)
    ap.add_argument("--dim", type=int, default=128)
    ap.add_argument("--efs", default="10,40,100,200")
    ap.add_argument("--sigma", type=float, default=0.12)
    ap.add_argument("--skip-load", action="store_true",
                    help="reuse existing items table + index (data already loaded)")
    args = ap.parse_args()

    DIM, TOTAL, NQ, SIGMA, G = args.dim, args.total, args.nq, args.sigma, args.g
    EFS = [int(x) for x in args.efs.split(",")]
    K = 10

    conn = psycopg2.connect(DBURL)
    conn.autocommit = True
    cur = conn.cursor()

    # ---- helpers ----
    def topk(tid, qv, k=K):
        cur.execute(
            "SELECT id FROM items WHERE tenant_id = %s ORDER BY embedding <=> %s::vector LIMIT %s",
            (tid, qv, k))
        return [r[0] for r in cur.fetchall()]

    def topk_global(qv, k=K):
        cur.execute(
            "SELECT id FROM items ORDER BY embedding <=> %s::vector LIMIT %s",
            (qv, k))
        return [r[0] for r in cur.fetchall()]

    def pt_topk(tid, qv, k=K):
        cur.execute(
            f"SELECT id FROM items_{tid} ORDER BY embedding <=> %s::vector LIMIT %s",
            (qv, k))
        return [r[0] for r in cur.fetchall()]

    def plan_of(sql, params):
        cur.execute("EXPLAIN " + sql, params)
        text = "\n".join(r[0] for r in cur.fetchall())
        if "Index Scan using" in text or "Bitmap Heap Scan" in text or "Bitmap Index Scan" in text:
            return "index"
        return "seq"

    def recall(ex, ap):
        return len(set(ex) & set(ap)) / K

    # ---- (re)load data ----
    if not args.skip_load:
        rng = np.random.default_rng(42)
        cents = rng.normal(size=(G, DIM))
        cents /= np.linalg.norm(cents, axis=1, keepdims=True)
        clusters = rng.integers(0, G, size=TOTAL)
        vecs = cents[clusters] + rng.normal(scale=SIGMA, size=(TOTAL, DIM))
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)

        boundaries, acc = [], 0.0
        for name, share in TENANTS:
            acc += share
            boundaries.append((name, acc))
        tenants = []
        for i in range(TOTAL):
            frac = (i + 0.5) / TOTAL
            name = REST
            for n_, b in boundaries:
                if frac < b:
                    name = n_
                    break
            tenants.append(name)

        cur.execute("DROP TABLE IF EXISTS items")
        for tid, _ in TENANTS:
            cur.execute(f"DROP TABLE IF EXISTS items_{tid}")
        cur.execute(
            f"CREATE TABLE items (tenant_id text NOT NULL, "
            f"id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, embedding vector({DIM}) NOT NULL)")

        def vtext(v):
            return "[" + ",".join(format(x, ".8f") for x in v) + "]"

        t0 = time.time()
        chunk = 5000
        for start in range(0, TOTAL, chunk):
            n = min(chunk, TOTAL - start)
            rows = [(tenants[start + j], vtext(vecs[start + j])) for j in range(n)]
            execute_values(cur, "INSERT INTO items (tenant_id, embedding) VALUES %s", rows,
                           template="(%s, %s::vector)")
        print(f"[load] {TOTAL} rows in {time.time()-t0:.1f}s", flush=True)

        t0 = time.time()
        cur.execute(
            f"CREATE INDEX items_emb_idx ON items USING hnsw (embedding vector_cosine_ops) "
            f"WITH (m=16, ef_construction=64)")
        print(f"[index] global HNSW built in {time.time()-t0:.1f}s", flush=True)
        cur.execute("ANALYZE items")
        print("[index] ANALYZE done", flush=True)

    # ---- sample query vectors per tenant ----
    queries = {}
    for tid, _ in TENANTS:
        cur.execute(
            "SELECT embedding::text FROM items WHERE tenant_id=%s ORDER BY random() LIMIT %s",
            (tid, NQ))
        queries[tid] = [r[0] for r in cur.fetchall()]

    # ---- exact (filtered) ground truth via forced seq scan ----
    cur.execute("SET enable_indexscan = off")
    cur.execute("SET enable_bitmapscan = off")
    exact = {}
    t0 = time.time()
    for tid, qs in queries.items():
        exact[tid] = [topk(tid, qv) for qv in qs]
    exact_time = (time.time() - t0) / (len(TENANTS) * NQ)
    cur.execute("SET enable_indexscan = on")
    cur.execute("SET enable_bitmapscan = on")

    # ---- global (no-filter) control: does HNSW hold without the filter? ----
    qctrl = queries["t_50pct"]
    cur.execute("SET enable_indexscan = off")
    cur.execute("SET enable_bitmapscan = off")
    g_exact = [topk_global(qv) for qv in qctrl]
    cur.execute("SET enable_indexscan = on")
    cur.execute("SET enable_bitmapscan = on")
    g_plan = plan_of("SELECT id FROM items ORDER BY embedding <=> %s::vector LIMIT 10",
                     (qctrl[0],))
    cur.execute("SET hnsw.ef_search = 40")
    g_approx = [topk_global(qv) for qv in qctrl]
    g_recall = sum(recall(e, a) for e, a in zip(g_exact, g_approx)) / NQ
    print(f"\n[control] global HNSW recall@10 (no filter, ef=40): {g_recall:.3f}  plan={g_plan}",
          flush=True)

    # ---- per-tenant filtered recall across ef_search, with plan label ----
    print(f"\n{'tenant':<12}{'share':>8}{'rows':>7}{'plan':>7}   recall@10 by ef_search",
          flush=True)
    print(" " * 36 + "".join(f"{e:>9}" for e in EFS), flush=True)
    lat_ef40 = {}
    plan_at40 = {}
    for tid, share in TENANTS:
        cur.execute("SELECT count(*) FROM items WHERE tenant_id=%s", (tid,))
        nrows = cur.fetchone()[0]
        plan40 = plan_of(
            "SELECT id FROM items WHERE tenant_id=%s ORDER BY embedding <=> %s::vector LIMIT 10",
            (tid, queries[tid][0]))
        plan_at40[tid] = plan40
        recalls = {}
        for ef in EFS:
            cur.execute("SET hnsw.ef_search = %s", (ef,))
            ap_all, lat = [], []
            for qv in queries[tid]:
                t0 = time.time()
                ap_all.append(topk(tid, qv))
                lat.append(time.time() - t0)
            recalls[ef] = sum(recall(e, a) for e, a in zip(exact[tid], ap_all)) / NQ
            if ef == 40:
                lat_ef40[tid] = sum(lat) / len(lat) * 1000
        print(f"{tid:<12}{share:>7.3f}{nrows:>7}{plan40:>7}"
              + "".join(f"{recalls[e]:>9.3f}" for e in EFS), flush=True)

    # ---- latency table (ef=40) ----
    print(f"\n{'tenant':<12}{'rows':>7}{'exact_ms':>10}{'hnsw_ms':>10}   (exact=seq, hnsw=ef40 filtered {plan_at40['t_50pct']})",
          flush=True)
    for tid, _ in TENANTS:
        cur.execute("SELECT count(*) FROM items WHERE tenant_id=%s", (tid,))
        nrows = cur.fetchone()[0]
        print(f"{tid:<12}{nrows:>7}{exact_time*1000:>10.1f}{lat_ef40.get(tid,0):>10.2f}",
              flush=True)

    # ---- mitigation control: per-tenant HNSW index for selected tenants ----
    print(f"\n[control] per-tenant HNSW index (ef=40), same tenant data & queries:",
          flush=True)
    print(f"{'tenant':<12}{'rows':>7}{'global+filter':>15}{'per-tenant':>12}{'perT_ms':>9}",
          flush=True)
    cur.execute("SET hnsw.ef_search = 40")
    for tid in ["t_50pct", "t_02pct", "t_001pct", "t_0005pct"]:
        gf_recall = sum(recall(e, a)
                        for e, a in zip(exact[tid], [topk(tid, qv) for qv in queries[tid]])) / NQ
        cur.execute(f"DROP TABLE IF EXISTS items_{tid}")
        cur.execute(
            f"CREATE TABLE items_{tid} AS SELECT id, embedding FROM items WHERE tenant_id=%s",
            (tid,))
        cur.execute(
            f"CREATE INDEX ON items_{tid} USING hnsw (embedding vector_cosine_ops) "
            f"WITH (m=16, ef_construction=64)")
        cur.execute(f"ANALYZE items_{tid}")
        cur.execute("SET enable_indexscan = off")
        cur.execute("SET enable_bitmapscan = off")
        pt_ex = [pt_topk(tid, qv) for qv in queries[tid]]
        cur.execute("SET enable_indexscan = on")
        cur.execute("SET enable_bitmapscan = on")
        pt_ap, pt_lat = [], []
        for qv in queries[tid]:
            t0 = time.time()
            pt_ap.append(pt_topk(tid, qv))
            pt_lat.append(time.time() - t0)
        pt_recall = sum(recall(e, a) for e, a in zip(pt_ex, pt_ap)) / NQ
        cur.execute(f"SELECT count(*) FROM items_{tid}")
        nrows = cur.fetchone()[0]
        print(f"{tid:<12}{nrows:>7}{gf_recall:>15.3f}{pt_recall:>12.3f}"
              f"{sum(pt_lat)/len(pt_lat)*1000:>9.2f}", flush=True)

    conn.close()


if __name__ == "__main__":
    main()
