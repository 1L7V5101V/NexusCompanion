"""
Memory v2 SQLite 存储层
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from memory2.models import MemoryItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_items (
    id            TEXT PRIMARY KEY,
    memory_type   TEXT NOT NULL,
    summary       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    embedding     TEXT,
    reinforcement INTEGER NOT NULL DEFAULT 1,
    extra_json    TEXT,
    source_ref    TEXT,
    happened_at   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_items_hash
    ON memory_items (content_hash, memory_type);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(summary: str, memory_type: str) -> str:
    text = re.sub(r"\s+", " ", summary.lower().strip()) + memory_type
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class MemoryStore2:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def upsert_item(
        self,
        memory_type: str,
        summary: str,
        embedding: list[float] | None,
        source_ref: str | None = None,
        extra: dict | None = None,
        happened_at: str | None = None,
    ) -> str:
        """写入或强化一条记忆。返回 'new:id' 或 'reinforced:id'"""
        chash = _content_hash(summary, memory_type)
        existing = self._db.execute(
            "SELECT id FROM memory_items WHERE content_hash=? AND memory_type=?",
            (chash, memory_type),
        ).fetchone()
        if existing:
            self._db.execute(
                "UPDATE memory_items SET reinforcement=reinforcement+1, updated_at=? WHERE id=?",
                (_now_iso(), existing[0]),
            )
            self._db.commit()
            return f"reinforced:{existing[0]}"

        item_id = hashlib.md5(f"{chash}{time.time()}".encode()).hexdigest()[:12]
        self._db.execute(
            """INSERT INTO memory_items
               (id, memory_type, summary, content_hash, embedding, extra_json,
                source_ref, happened_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                item_id,
                memory_type,
                summary,
                chash,
                json.dumps(embedding) if embedding is not None else None,
                json.dumps(extra) if extra else None,
                source_ref,
                happened_at,
                _now_iso(),
                _now_iso(),
            ),
        )
        self._db.commit()
        return f"new:{item_id}"

    def get_all_with_embedding(self) -> list[tuple]:
        """返回 [(id, memory_type, summary, embedding_list, extra_json_dict, happened_at)]"""
        rows = self._db.execute(
            "SELECT id, memory_type, summary, embedding, extra_json, happened_at "
            "FROM memory_items WHERE embedding IS NOT NULL"
        ).fetchall()
        result = []
        for row_id, mtype, summary, emb_json, extra_json, happened_at in rows:
            emb = json.loads(emb_json) if emb_json else None
            extra = json.loads(extra_json) if extra_json else {}
            result.append((row_id, mtype, summary, emb, extra, happened_at))
        return result

    def vector_search(
        self,
        query_vec: list[float],
        top_k: int = 8,
        memory_types: list[str] | None = None,
        score_threshold: float = 0.0,
    ) -> list[dict]:
        """cosine similarity 检索，返回 top-k 结果"""
        rows = self.get_all_with_embedding()
        if not rows:
            return []

        if memory_types:
            rows = [r for r in rows if r[1] in memory_types]

        if not rows:
            return []

        q = np.array(query_vec, dtype=np.float32)
        q_norm = float(np.linalg.norm(q)) + 1e-9

        scored = []
        for row_id, mtype, summary, emb, extra, happened_at in rows:
            if emb is None:
                continue
            e = np.array(emb, dtype=np.float32)
            score = float(e @ q) / (float(np.linalg.norm(e)) + 1e-9) / q_norm
            if score < score_threshold:
                continue
            scored.append(
                {
                    "id": row_id,
                    "memory_type": mtype,
                    "summary": summary,
                    "extra_json": extra,
                    "happened_at": happened_at,
                    "score": round(score, 4),
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def list_by_type(self, memory_type: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, memory_type, summary, extra_json, happened_at, reinforcement "
            "FROM memory_items WHERE memory_type=?",
            (memory_type,),
        ).fetchall()
        result = []
        for row_id, mtype, summary, extra_json, happened_at, reinforcement in rows:
            result.append(
                {
                    "id": row_id,
                    "memory_type": mtype,
                    "summary": summary,
                    "extra_json": json.loads(extra_json) if extra_json else {},
                    "happened_at": happened_at,
                    "reinforcement": reinforcement,
                }
            )
        return result

    def delete_by_source_ref(self, source_ref: str) -> int:
        """删除指定 source_ref 的所有条目，返回删除行数。"""
        cur = self._db.execute(
            "DELETE FROM memory_items WHERE source_ref=?", (source_ref,)
        )
        self._db.commit()
        return cur.rowcount

    def close(self) -> None:
        self._db.close()
