"""机器可读校验：逐表行数、关键字段 hash、引用完整性、语义抽样。

校验口径与导入一致：
- 行数：对源行套用同一 transform + required 过滤后计数（``expected_counts`` 语义）。
- 字段 hash：按主键（含 tenant_id）排序后对规范字段子集做流式 SHA-256。
  规范字段排除易变列——被注入 ``now`` 的 created_at/updated_at，以及
  float 表示敏感的 embedding（后者由语义抽样向量 top-k 覆盖）。
- 引用完整性：messages/deliveries/session_state/context_only_timestamps/
  tick_log 的 session_key → sessions.key；memory_replacements 的 old/new
  item_id → memory_items.id；tick_step_log.tick_id → tick_log.id（同 tenant）。
- 语义抽样：镜像 memory2/store 的真实查询语义——L2 归一化后 KNN
  （≡ cosine ranking，PG 用 ``<=>``）、summary OR-LIKE 关键词、sessions.next_seq。

发现任何维度差异 → ``run_verify`` 返回 ``exit_code`` 非零，报告落盘
``openspec/evidence/phase1b/results/<run_id>-verify.json``。
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

from scripts.migrate.config import MEMORY_SOURCE_IDENTITY, MigrationConfig
from scripts.migrate.importer import (
    TABLE_SPECS,
    _iter_spec_rows,
    _preflight,
    _resolve_tenant,
    _same_columns,
)
from scripts.migrate.mapping import MappingError, TenantMapping
from scripts.migrate.source import MEMORY_DB, SESSIONS_DB, SqliteSource

VEC_DIM = 1024
TOP_K = 5
N_QUERIES = 3


# ── 规范字段子集（排除被注入的 created_at/updated_at 与 embedding）────────────

HASH_COLS: dict[str, tuple[str, ...]] = {
    "sessions": ("tenant_id", "key", "channel", "chat_id", "metadata_json",
                 "last_consolidated", "last_user_at", "last_proactive_at",
                 "next_seq"),
    "messages": ("tenant_id", "id", "session_key", "seq", "role", "content",
                 "tool_chain", "extra", "ts"),
    "memory_items": ("tenant_id", "id", "memory_type", "summary",
                     "content_hash", "reinforcement", "emotional_weight",
                     "extra_json", "source_ref", "happened_at", "status"),
    "memory_replacements": (
        "tenant_id", "id", "old_item_id", "old_memory_type", "old_summary",
        "old_source_ref", "old_happened_at", "old_extra_json", "new_item_id",
        "new_memory_type", "new_summary", "new_source_ref", "new_happened_at",
        "new_extra_json", "relation_type", "source_ref"),
    "consolidation_events": ("tenant_id", "source_ref", "item_id"),
    "deliveries": ("tenant_id", "session_key", "delivery_key", "sent_at"),
    "session_state": ("tenant_id", "session_key", "key", "value"),
    "context_only_timestamps": ("tenant_id", "id", "session_key", "ts"),
    "tick_log": (
        "tenant_id", "id", "tick_id", "session_key", "started_at",
        "finished_at", "gate_exit", "terminal_action", "skip_reason",
        "steps_taken", "alert_count", "content_count", "context_count",
        "interesting_ids", "discarded_ids", "cited_ids", "drift_entered",
        "final_message", "proactive_effects_json"),
    "tick_step_log": (
        "tenant_id", "id", "tick_id", "step_index", "phase", "tool_name",
        "tool_call_id", "tool_args_json", "tool_result_text",
        "terminal_action_after", "skip_reason_after", "interesting_ids_after",
        "discarded_ids_after", "cited_ids_after", "final_message_after"),
    "scheduled_jobs": (
        "tenant_id", "id", "trigger", "tier", "fire_at", "channel", "chat_id",
        "interval_seconds", "cron_expr", "message", "prompt", "name",
        "timezone", "run_count", "enabled"),
    "app_configs": ("tenant_id", "key", "value_json"),
}


# ── 引用完整性规则（表, 外键列, 引用表, 引用列；均同 tenant）────────────────

FK_CHECKS: list[tuple[str, str, str, str, str]] = [
    ("messages.session_key", "messages", "session_key", "sessions", "key"),
    ("deliveries.session_key", "deliveries", "session_key", "sessions", "key"),
    ("session_state.session_key", "session_state", "session_key", "sessions",
     "key"),
    ("context_only_timestamps.session_key", "context_only_timestamps",
     "session_key", "sessions", "key"),
    ("tick_log.session_key", "tick_log", "session_key", "sessions", "key"),
    ("memory_replacements.old_item_id", "memory_replacements", "old_item_id",
     "memory_items", "id"),
    ("memory_replacements.new_item_id", "memory_replacements", "new_item_id",
     "memory_items", "id"),
    ("tick_step_log.tick_id", "tick_step_log", "tick_id", "tick_log",
     "tick_id"),
]


def _sort_key(v: Any) -> tuple[int, Any]:
    """跨类型可排序主键值（None / bool / int-float / str）。"""
    if v is None:
        return (0, "")
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, (int, float)):
        return (2, float(v))
    return (3, str(v))


def _canon(v: Any) -> str:
    """单值规范序列化：源侧字符串与目标侧 datetime/原生类型映射到同一文本。"""
    if v is None:
        return "N"
    if isinstance(v, bool):
        return "B" + ("1" if v else "0")
    if isinstance(v, int):
        return "I" + str(v)
    if isinstance(v, float):
        return "F" + repr(v)
    if isinstance(v, str):
        return "S" + v
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        # 与源侧 canon_ts 的 ISO 字符串同形（时间戳统一按 'S' 序列化）。
        return "S" + v.astimezone(timezone.utc).isoformat()
    if isinstance(v, date):
        return "S" + datetime(v.year, v.month, v.day,
                              tzinfo=timezone.utc).isoformat()
    return "T" + repr(v)


def _hash_rows(values_rows: list[list[Any]]) -> str:
    d = hashlib.sha256()
    for values in values_rows:
        d.update("\x1f".join(_canon(v) for v in values).encode("utf-8"))
        d.update(b"\x1e")
    return d.hexdigest()


def _source_hash_rows(
    source: SqliteSource, spec, mapping: TenantMapping,
    tick_tenants: dict[str, str],
) -> tuple[list[list[Any]], int]:
    hash_cols = HASH_COLS[spec.name]
    rows: list[tuple[list[Any], list[Any]]] = []
    for row in _iter_spec_rows(source, spec):
        tid = _resolve_tenant(spec, row, mapping, tick_tenants)
        tr = spec.transform(row, tid) if spec.transform else _same_columns(row, tid)
        if any(tr.get(c) is None for c in spec.required):
            continue
        rows.append(
            ([tr.get(c) for c in spec.pkey], [tr.get(c) for c in hash_cols])
        )
    rows.sort(key=lambda pair: [_sort_key(v) for v in pair[0]])
    return [vals for _, vals in rows], len(rows)


def _target_hash_rows(
    conn, spec,
) -> tuple[list[list[Any]], int]:
    hash_cols = HASH_COLS[spec.name]
    cols = ", ".join(hash_cols + tuple(spec.pkey))
    fetched = conn.execute(
        f"SELECT {cols} FROM {spec.name} ORDER BY {', '.join(spec.pkey)}"
    ).fetchall()
    rows = []
    for r in fetched:
        hash_vals = list(r[: len(hash_cols)])
        pkey_vals = list(r[len(hash_cols):])
        rows.append((pkey_vals, hash_vals))
    rows.sort(key=lambda pair: [_sort_key(v) for v in pair[0]])
    return [vals for _, vals in rows], len(rows)


# ── 维度 1：逐表行数 + 关键字段 hash ─────────────────────────────────────────

def _row_count_dim(
    source: SqliteSource, conn, mapping: TenantMapping,
    tick_tenants: dict[str, str],
) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for spec in TABLE_SPECS:
        src_n = 0
        for row in _iter_spec_rows(source, spec):
            tid = _resolve_tenant(spec, row, mapping, tick_tenants)
            tr = spec.transform(row, tid) if spec.transform else _same_columns(row, tid)
            if any(tr.get(c) is None for c in spec.required):
                continue
            src_n += 1
        tg_n = int(conn.execute(f"SELECT COUNT(*) FROM {spec.name}").fetchone()[0])
        tables[spec.name] = {
            "source": src_n,
            "target": tg_n,
            "match": src_n == tg_n,
        }
    return {
        "ok": all(t["match"] for t in tables.values()),
        "tables": tables,
    }


def _field_hash_dim(
    source: SqliteSource, conn, mapping: TenantMapping,
    tick_tenants: dict[str, str],
) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for spec in TABLE_SPECS:
        src_vals, _ = _source_hash_rows(source, spec, mapping, tick_tenants)
        tg_vals, _ = _target_hash_rows(conn, spec)
        src_hash = _hash_rows(src_vals)
        tg_hash = _hash_rows(tg_vals)
        tables[spec.name] = {
            "columns": list(HASH_COLS[spec.name]),
            "source_hash": src_hash,
            "target_hash": tg_hash,
            "match": src_hash == tg_hash,
        }
    return {
        "ok": all(t["match"] for t in tables.values()),
        "tables": tables,
    }


# ── 维度 2：引用完整性 ───────────────────────────────────────────────────────

def _referential_dim(conn) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for name, table, fk_col, ref_table, ref_pk in FK_CHECKS:
        sql = (
            f"SELECT COUNT(*) FROM {table} t "
            f"LEFT JOIN {ref_table} r "
            f"ON r.tenant_id = t.tenant_id AND r.{ref_pk} = t.{fk_col} "
            f"WHERE t.{fk_col} IS NOT NULL AND r.{ref_pk} IS NULL"
        )
        orphans = int(conn.execute(sql).fetchone()[0])
        checks[name] = {"table": table, "fk": fk_col,
                        "references": f"{ref_table}.{ref_pk}",
                        "orphans": orphans,
                        "match": orphans == 0}
    return {
        "ok": all(c["match"] for c in checks.values()),
        "checks": checks,
    }


# ── 维度 3：语义抽样（镜像 memory2/store 真实查询语义）──────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    import numpy as np
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    return float(np.dot(va, vb) / (np.linalg.norm(va) * np.linalg.norm(vb) + 1e-9))


def _active_source_items(source: SqliteSource) -> list[tuple[str, list[float]]]:
    items: list[tuple[str, list[float]]] = []
    if not source.table_exists(MEMORY_DB, "memory_items"):
        return items
    for row in source.iter_rows(MEMORY_DB, "memory_items", order_by=["id"]):
        if str(row.get("status") or "active") != "active":
            continue
        raw = row.get("embedding")
        if not raw:
            continue
        try:
            vec = json.loads(str(raw))
        except (ValueError, TypeError):
            continue
        if not isinstance(vec, list) or len(vec) != VEC_DIM:
            continue
        items.append((str(row.get("id")), [float(v) for v in vec]))
    return items


def _vector_topk(
    source: SqliteSource, conn, mem_tenant: str,
) -> dict[str, Any]:
    items = _active_source_items(source)
    queries = [emb for _, emb in items[:N_QUERIES]]
    results: list[dict[str, Any]] = []
    for qi, q in enumerate(queries):
        scored = sorted(
            ((iid, _cosine(q, emb)) for iid, emb in items),
            key=lambda t: (-t[1], t[0]),
        )
        src_ids = [iid for iid, _ in scored[:TOP_K]]
        q_text = "[" + ", ".join(f"{v:.8g}" for v in q) + "]"
        rows = conn.execute(
            "SELECT id FROM memory_items WHERE tenant_id = %s "
            "AND status = 'active' AND embedding IS NOT NULL "
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            (mem_tenant, q_text, TOP_K),
        ).fetchall()
        tg_ids = [str(r[0]) for r in rows]
        results.append({
            "query_index": qi,
            "source_ids": src_ids,
            "target_ids": tg_ids,
            "match": src_ids == tg_ids,
        })
    return {
        "k": TOP_K,
        "n_queries": len(queries),
        "ok": all(r["match"] for r in results),
        "results": results,
    }


def _keyword_hits(
    source: SqliteSource, conn, mem_tenant: str, terms: list[str],
) -> dict[str, Any]:
    if terms:
        src_cond = " OR ".join(["summary LIKE ?"] * len(terms))
        src_rows = source.query(
            MEMORY_DB,
            f"SELECT id FROM memory_items WHERE status='active' AND ({src_cond})",
            [f"%{t}%" for t in terms],
        )
        src_ids = sorted(str(r["id"]) for r in src_rows)
    else:
        src_ids = []
    if terms:
        tg_cond = " OR ".join(["summary ILIKE %s"] * len(terms))
        tg_rows = conn.execute(
            f"SELECT id FROM memory_items WHERE tenant_id = %s "
            f"AND status = 'active' AND ({tg_cond})",
            [mem_tenant] + [f"%{t}%" for t in terms],
        ).fetchall()
        tg_ids = sorted(str(r[0]) for r in tg_rows)
    else:
        tg_ids = []
    return {
        "terms": terms,
        "source_ids": src_ids,
        "target_ids": tg_ids,
        "match": src_ids == tg_ids,
    }


def _session_next_seq(source: SqliteSource, conn) -> dict[str, Any]:
    src: dict[str, int] = {}
    if source.table_exists(SESSIONS_DB, "sessions"):
        for row in source.iter_rows(SESSIONS_DB, "sessions", order_by=["key"]):
            src[str(row.get("key"))] = int(row.get("next_seq") or 0)
    tg_rows = conn.execute("SELECT key, next_seq FROM sessions").fetchall()
    tg = {str(r[0]): int(r[1] or 0) for r in tg_rows}
    mismatches = {
        k: {"source": src[k], "target": tg[k]}
        for k in src
        if k in tg and src[k] != tg[k]
    }
    missing = sorted(set(src) - set(tg))
    extra = sorted(set(tg) - set(src))
    return {
        "source_count": len(src),
        "target_count": len(tg),
        "ok": not mismatches and not missing and not extra,
        "mismatches": mismatches,
        "missing_keys": missing,
        "extra_keys": extra,
    }


def _semantic_dim(
    source: SqliteSource, conn, mapping: TenantMapping,
) -> dict[str, Any]:
    mem_tenant = mapping.resolve(MEMORY_SOURCE_IDENTITY)

    # 关键词词项：取前 3 条 active item summary 的前两个词（长度 ≥ 3），去重保序。
    items = _active_source_items(source)
    terms: list[str] = []
    for iid, _ in items[:3]:
        rows = source.query(
            MEMORY_DB,
            "SELECT summary FROM memory_items WHERE id = ?",
            [iid],
        )
        if not rows:
            continue
        for word in str(rows[0]["summary"] or "").split():
            if len(word) >= 3 and word not in terms:
                terms.append(word)
        if len(terms) >= 2:
            break
    terms = terms[:2]
    absent_term = "zzzqqxabsent12345"
    checks = [
        _keyword_hits(source, conn, mem_tenant, terms),
        _keyword_hits(source, conn, mem_tenant, [absent_term]),
    ]

    topk = _vector_topk(source, conn, mem_tenant)
    next_seq = _session_next_seq(source, conn)

    return {
        "ok": topk["ok"] and next_seq["ok"] and all(c["match"] for c in checks),
        "vector_topk": topk,
        "keyword_search": {
            "ok": all(c["match"] for c in checks),
            "checks": checks,
        },
        "session_next_seq": next_seq,
    }


# ── 编排 ─────────────────────────────────────────────────────────────────────

def run_verify(cfg: MigrationConfig) -> dict[str, Any]:
    """执行全量校验；返回报告（含 ``exit_code``）。差异时 exit_code=1，不落盘异常=2。"""
    if cfg.mapping_path is None:
        raise MappingError("校验需要 tenant mapping 文件（--mapping），用于源侧 tenant 解析")
    start = time.monotonic()
    source = SqliteSource(cfg.workspace)
    mapping = TenantMapping.load(cfg.mapping_path)
    _identities, tick_tenants = _preflight(source, mapping)
    conn = psycopg.connect(cfg.pg_url)
    try:
        row_count = _row_count_dim(source, conn, mapping, tick_tenants)
        field_hash = _field_hash_dim(source, conn, mapping, tick_tenants)
        referential = _referential_dim(conn)
        semantic = _semantic_dim(source, conn, mapping)
    finally:
        conn.close()

    dimensions = {
        "row_count": row_count,
        "field_hash": field_hash,
        "referential_integrity": referential,
        "semantic_sampling": semantic,
    }
    ok = all(d["ok"] for d in dimensions.values())
    exit_code = 0 if ok else 1
    report = {
        "meta": {
            **cfg.to_meta(),
            "tool": "phase1b-verify",
            "hash_algorithm": "sha256",
            "hash_columns_per_table": {k: list(v) for k, v in HASH_COLS.items()},
            "fk_checks": [list(f) for f in FK_CHECKS],
        },
        "elapsed_sec": round(time.monotonic() - start, 3),
        "dimensions": dimensions,
        "ok": ok,
        "exit_code": exit_code,
    }
    evidence_path = _write_verify_result(cfg, report)
    report["evidence_path"] = str(evidence_path)
    return report


def _write_verify_result(cfg: MigrationConfig, report: dict[str, Any]) -> Path:
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.results_dir / f"{cfg.run_id}-verify.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path
