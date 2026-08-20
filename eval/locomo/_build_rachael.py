"""
Step 1: Pre-fill embedding cache from sessions.db
Step 2: Build Rachael graph from sessions.db (offline reconstruction)

Usage:
  uv run python eval/locomo/_build_rachael.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import sqlite3
import logging
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger("build_rachael")

# Conv-26 workspace
WORKSPACE = Path(r"C:\Users\HP\AppData\Local\Temp\locomo_bench\conv-26")
SESSIONS_DB = WORKSPACE / "sessions.db"
RACHAEL_DB = WORKSPACE / "memory" / "rachael.db"
CONFIG_TOML = _PROJECT_ROOT / "eval" / "locomo" / "config.toml"
EMBEDDING_MODEL = "text-embedding-v4"


async def step1_prefill_embeddings():
    """Read all user messages from sessions.db, embed them, store in cache."""
    from agent.config_models import Config
    from core.net.http import SharedHttpResources
    from memory2.embedder import Embedder

    # 1. Count messages
    conn = sqlite3.connect(str(SESSIONS_DB))
    total = conn.execute("SELECT COUNT(1) FROM messages WHERE role IN ('user','assistant')").fetchone()[0]
    logger.info("sessions.db messages (user+assistant): %d", total)

    # 2. Create rachael.db with schema
    RACHAEL_DB.parent.mkdir(parents=True, exist_ok=True)

    from plugins.rachael.config import RachaelConfig, load_rachael_config, resolve_rachael_db_path
    from plugins.rachael.store import RachaelStore

    rachael_config = load_rachael_config()
    store = RachaelStore(RACHAEL_DB)

    # Check existing cache (use store's db, not sessions.db)
    cached = store.db.execute("SELECT COUNT(1) FROM akasha_embedding_cache").fetchone()[0]
    if cached > 0:
        logger.info("akasha_embedding_cache already has %d entries, skipping pre-fill", cached)
        store.close()
        conn.close()
        return

    # 3. Read all user messages in order
    messages = conn.execute(
        "SELECT id, session_key, seq, role, content, ts FROM messages WHERE role IN ('user','assistant') ORDER BY ts, session_key, seq"
    ).fetchall()
    logger.info("Read %d messages from sessions.db", len(messages))

    # 4. Create embedder
    config = Config.load(str(CONFIG_TOML))
    http = SharedHttpResources()
    embedding_cfg = config.memory.embedding
    embedder = Embedder(
        base_url=embedding_cfg.base_url,
        api_key=embedding_cfg.api_key,
        model=embedding_cfg.model,
        output_dimensionality=embedding_cfg.output_dimensionality,
        requester=http.external_default,
    )

    from plugins.rachael.core import SourceMessage

    # 5. Batch embed and cache
    batch_size = 20
    total_embedded = 0
    for i in range(0, len(messages), batch_size):
        batch = messages[i : i + batch_size]
        texts = [str(row[4] or "") for row in batch]
        try:
            embeddings = await embedder.embed_batch(texts)
        except Exception as e:
            logger.warning("embed_batch failed at idx %d: %s, retrying one-by-one", i, e)
            embeddings = []
            for text in texts:
                try:
                    emb = await embedder.embed(text)
                    embeddings.append(emb)
                except Exception as e2:
                    logger.warning("embed single failed: %s", e2)
                    embeddings.append([0.0] * 1024)

        for row, emb in zip(batch, embeddings):
            msg = SourceMessage(
                id=str(row[0]),
                session_key=str(row[1]),
                seq=int(row[2]),
                role=str(row[3]),
                content=str(row[4] or ""),
                ts=str(row[5] or ""),
            )
            store.upsert_cached_embedding(message=msg, model=embedding_cfg.model, embedding=emb)

        total_embedded += len(batch)
        if (i // batch_size) % 5 == 0:
            logger.info("Embedded %d/%d messages...", total_embedded, len(messages))

    store.close()
    await http.aclose()
    conn.close()
    logger.info("Pre-fill complete: %d embeddings cached", total_embedded)


def _normalize_timestamps():
    """Convert human-readable timestamps to ISO format in sessions.db.

    LoCoMo timestamps look like '1:56 pm on 8 May, 2023' which parse_ts_unix
    (Rachael core) doesn't handle. Convert them to ISO format in-place.
    """
    import re
    from datetime import datetime

    conn = sqlite3.connect(str(SESSIONS_DB))
    rows = conn.execute("SELECT id, ts FROM messages").fetchall()
    updated = 0
    for msg_id, ts_raw in rows:
        ts_str = str(ts_raw or "")
        if not ts_str or ts_str.startswith("20") or ts_str.startswith("202"):
            continue  # already ISO (starts with year)
        # Try parsing LoCoMo format: "1:56 pm on 8 May, 2023"
        m = re.match(r"(\d+):(\d+)\s*(am|pm)\s*on\s+(\d+)\s+(\w+),\s+(\d{4})", ts_str, re.IGNORECASE)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2))
            ampm = m.group(3).lower()
            day = int(m.group(4))
            month_str = m.group(5)
            year = int(m.group(6))
            month_map = {
                "january": 1, "february": 2, "march": 3, "april": 4,
                "may": 5, "june": 6, "july": 7, "august": 8,
                "september": 9, "october": 10, "november": 11, "december": 12,
            }
            month = month_map.get(month_str.lower(), 1)
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            dt = datetime(year, month, day, hour, minute)
            iso_ts = dt.isoformat()
            conn.execute("UPDATE messages SET ts = ? WHERE id = ?", (iso_ts, msg_id))
            updated += 1
        else:
            logger.warning("Unparseable timestamp: %r (id=%s)", ts_str, msg_id)
    conn.commit()
    conn.close()
    logger.info("Normalized %d timestamps to ISO format", updated)


def step2_build_graph():
    """Run the offline Rachael graph reconstruction."""
    from scripts.build_akasha_db import _run, _parse_args
    import argparse

    # Build args manually (simulate CLI)
    sys.argv = [
        "build_akasha_db.py",
        "--config", str(CONFIG_TOML),
        "--workspace", str(WORKSPACE),
        "--progress-every", "500",
    ]
    args = _parse_args()
    # Override workspace
    args.workspace = str(WORKSPACE)
    args.db_path = str(RACHAEL_DB)
    args.config = str(CONFIG_TOML)

    logger.info("Building Rachael graph from sessions.db...")
    stats = _run()
    logger.info(
        "Build complete: messages=%d activations=%d cache_hits=%d cache_misses=%d snapshots=%d",
        stats.messages, stats.activations, stats.cache_hits, stats.cache_misses, stats.snapshots,
    )
    return stats


def step3_inspect_graph():
    """Print basic graph stats."""
    import sqlite3

    conn = sqlite3.connect(str(RACHAEL_DB))
    nodes = conn.execute("SELECT COUNT(1) FROM akasha_nodes").fetchone()[0]
    edges = conn.execute("SELECT COUNT(1) FROM akasha_edges").fetchone()[0]
    logger.info("=== Rachael Graph Stats ===")
    logger.info("Nodes: %d", nodes)
    logger.info("Edges: %d", edges)

    if nodes > 0:
        # Top edges by co_count
        top = conn.execute(
            "SELECT src_key, dst_key, co_count, weight FROM akasha_edges ORDER BY co_count DESC LIMIT 10"
        ).fetchall()
        logger.info("Top 10 edges by co_count:")
        for src, dst, cc, w in top:
            logger.info("  %s --[cc=%d w=%.3f]--> %s", src[:50], cc, w, dst[:50])

    if edges > 0:
        # Edge distribution
        dist = conn.execute("SELECT co_count, COUNT(1) AS cnt FROM akasha_edges GROUP BY co_count ORDER BY co_count").fetchall()
        logger.info("Edge co_count distribution:")
        for cc, cnt in dist:
            logger.info("  co_count=%d: %d edges", cc, cnt)

    conn.close()


async def main():
    t0 = time.time()
    await step1_prefill_embeddings()
    t1 = time.time()
    logger.info("Pre-fill took %.1f seconds", t1 - t0)

    _normalize_timestamps()

    stats = step2_build_graph()
    t2 = time.time()
    logger.info("Build took %.1f seconds", t2 - t1)

    step3_inspect_graph()
    logger.info("Total time: %.1f seconds", time.time() - t0)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
