"""Inspect Rachael graph structure - audit the actual rachael.db."""
import sqlite3, os

paths = [
    r"C:\Users\HP\AppData\Local\Temp\locomo_bench\conv-26\rachael.db",
    r"C:\Users\HP\AppData\Local\Temp\locomo_bench\conv-26\memory\rachael.db",
]

for db_path in paths:
    conn2 = sqlite3.connect(db_path)
    size = os.path.getsize(db_path)
    tables2 = conn2.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    print(f"=== {db_path} (size={size/1024:.0f}KB) ===")
    print(f"  Tables ({len(tables2)}):")
    for (t,) in tables2:
        print(f"    - {t}")
    conn2.close()
    print()

# Use the data-bearing one
db_path = paths[1]
conn = sqlite3.connect(db_path)

# -- Nodes --
ncols = [d[1] for d in conn.execute("PRAGMA table_info(akasha_nodes)").fetchall()]
print(f"akasha_nodes columns: {ncols}")
ncount = conn.execute("SELECT COUNT(1) FROM akasha_nodes").fetchone()[0]
print(f"\nTotal nodes: {ncount}")

# Top nodes by degree
rows = conn.execute("""
    SELECT n.key,
           (SELECT COUNT(1) FROM akasha_edges WHERE src_key = n.key) as out_degree,
           (SELECT COUNT(1) FROM akasha_edges WHERE dst_key = n.key) as in_degree
    FROM akasha_nodes n
    ORDER BY (out_degree + in_degree) DESC
    LIMIT 10
""").fetchall()
print("\n=== Top 10 Nodes by Degree ===")
for key, outd, ind in rows:
    print(f"  {key} out={outd} in={ind} total={outd+ind}")

# -- Edges --
ecols = [d[1] for d in conn.execute("PRAGMA table_info(akasha_edges)").fetchall()]
print(f"\nakasha_edges columns: {ecols}")
ecount = conn.execute("SELECT COUNT(1) FROM akasha_edges").fetchone()[0]
print(f"Total edges: {ecount}")

# Edge type distribution
if "edge_type" in ecols:
    rows = conn.execute(
        "SELECT edge_type, COUNT(1) as cnt FROM akasha_edges GROUP BY edge_type ORDER BY cnt DESC"
    ).fetchall()
    print("\n=== Edge Types ===")
    for t, c in rows:
        print(f"  {t}: {c}")

# Weight distribution
if "weight" in ecols:
    rows = conn.execute("""
        SELECT CASE
            WHEN weight < 0.01 THEN '<0.01'
            WHEN weight < 0.05 THEN '0.01-0.05'
            WHEN weight < 0.10 THEN '0.05-0.10'
            WHEN weight < 0.20 THEN '0.10-0.20'
            ELSE '>=0.20'
        END as bucket,
        COUNT(1) as cnt
        FROM akasha_edges
        GROUP BY bucket
        ORDER BY bucket
    """).fetchall()
    print("\n=== Edge Weight Distribution ===")
    for b, c in rows:
        print(f"  {b}: {c} edges")

# co_count distribution
if "co_count" in ecols:
    rows = conn.execute("""
        SELECT co_count, COUNT(1) as cnt FROM akasha_edges
        GROUP BY co_count ORDER BY co_count
    """).fetchall()
    print("\n=== Edge co_count Distribution ===")
    for cc, cnt in rows:
        print(f"  co_count={cc}: {cnt} edges")

# Top edges
if "co_count" in ecols:
    rows = conn.execute("""
        SELECT src_key, dst_key, co_count, weight
        FROM akasha_edges ORDER BY co_count DESC LIMIT 15
    """).fetchall()
    print("\n=== Top 15 Edges ===")
    for src, dst, cc, w in rows:
        print(f"  {src} --[cc={cc} w={w:.4f}]--> {dst}")

# Embedding cache
rows = conn.execute(
    "SELECT model, COUNT(1) as cnt FROM akasha_embedding_cache GROUP BY model"
).fetchall()
print("\n=== Embedding Cache ===")
for m, c in rows:
    print(f"  {m}: {c} entries")

# Activation events
rows = conn.execute("SELECT COUNT(1) FROM akasha_activation_events").fetchone()
print(f"\nActivation events: {rows[0]}")

# Query log
rows = conn.execute("SELECT DISTINCT session_key FROM akasha_query_log").fetchall()
print(f"Sessions in query log: {len(rows)}")

# Salience stats
rows = conn.execute("""
    SELECT
        COUNT(1) as total,
        SUM(CASE WHEN salience >= 0.7 THEN 1 ELSE 0 END) as high_salience,
        SUM(CASE WHEN salience >= 0.4 AND salience < 0.7 THEN 1 ELSE 0 END) as medium_salience,
        SUM(CASE WHEN salience < 0.4 THEN 1 ELSE 0 END) as low_salience
    FROM akasha_nodes
""").fetchone()
print(f"\nSalience distribution: total={rows[0]}, high(>=0.7)={rows[1]}, medium(0.4-0.7)={rows[2]}, low(<0.4)={rows[3]}")

conn.close()
