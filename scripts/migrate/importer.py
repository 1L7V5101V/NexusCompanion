"""批量导入编排：表规格、FK 顺序、checkpoint 续传、幂等、tenant mapping、结果入库。

覆盖 Phase 1 全部有 SQLite 源的目标表（12 张）：
sessions → messages → memory_items → memory_replacements →
consolidation_events / deliveries / session_state / context_only_timestamps /
tick_log / tick_step_log / scheduled_jobs / app_configs。

- FK 顺序：sessions 先于 messages；memory_items 先于 memory_replacements。
- 幂等：BulkPgWriter COPY + ON CONFLICT DO NOTHING。
- 续传：RunCheckpoint 逐表高水位（source_order 列值）；每批 ``commit`` 后才写
  checkpoint，保证「高水位 ≤ 已提交行数」。
- tenant mapping：预检收集全部源身份，未映射即终止、零写入。
- 行级容错：required 列缺失/非法（如 NOT NULL 时间戳解析失败）的行记为
  ``skipped_invalid`` 跳过，不因单行坏数据中断整表（与 import_to_pg 同语义）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from scripts.migrate.bulk import BulkPgWriter, sanitize_embedding
from scripts.migrate.checkpoint import (
    RunCheckpoint,
    load_checkpoint,
    save_checkpoint,
)
from scripts.migrate.config import MEMORY_SOURCE_IDENTITY, MigrationConfig
from scripts.migrate.mapping import MappingError, TenantMapping
from scripts.migrate.source import (
    MEMORY_DB,
    PROACTIVE_DB,
    SESSIONS_DB,
    SqliteSource,
    canon_ts,
    channel_of,
)

Transform = Callable[[dict[str, Any], str], dict[str, Any]]
Identity = Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class TableSpec:
    """一张目标表的导入描述。"""

    name: str
    db: str  # 源 DB（SESSIONS_DB / MEMORY_DB / PROACTIVE_DB / "json"）
    source: str  # 源表名或 JSON 来源 key
    columns: list[str]  # 目标列（= COPY 列序）
    pkey: list[str]  # 目标主键列（幂等冲突目标）
    source_order: list[str]  # 源行迭代排序列（checkpoint 高水位）
    required: tuple[str, ...] = ()  # 目标列必须非 None，否则行跳过（skipped_invalid）
    partitioned: bool = False  # 仅 memory_items
    identity: Identity | None = None  # None → 该表无独立源身份（tick_step_log）
    transform: Transform | None = None  # None → 逐列同名复制 + tenant_id


def _ident_session(row: dict[str, Any]) -> str:
    return channel_of(str(row.get("session_key", "")))


def _ident_session_key(row: dict[str, Any]) -> str:
    """sessions 表的源身份列是 ``key``（``channel:chat_id``），非 session_key。"""
    return channel_of(str(row.get("key", "")))


def _ident_memory(_row: dict[str, Any]) -> str:
    return MEMORY_SOURCE_IDENTITY


def _same_columns(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    out = {"tenant_id": tenant_id}
    out.update({k: row.get(k) for k in row.keys()})
    return out


# ── 各表 transform（SQLite 列 → PG 列）──────────────────────────────────────

def _session_transform(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    key = str(row.get("key", ""))
    parts = key.split(":", 1)
    now = datetime.now(timezone.utc).isoformat()
    return {
        "tenant_id": tenant_id,
        "key": key,
        "channel": parts[0] if len(parts) > 1 else "",
        "chat_id": parts[1] if len(parts) > 1 else key,
        "metadata_json": row.get("metadata") or None,
        "last_consolidated": int(row.get("last_consolidated") or 0),
        "last_user_at": canon_ts(row.get("last_user_at")),
        "last_proactive_at": canon_ts(row.get("last_proactive_at")),
        "next_seq": int(row.get("next_seq") or 0),
        "created_at": canon_ts(row.get("created_at")) or now,
        "updated_at": canon_ts(row.get("updated_at")) or now,
    }


def _message_transform(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "id": str(row.get("id")),
        "session_key": row.get("session_key"),
        "seq": int(row.get("seq") or 0),
        "role": str(row.get("role") or ""),
        "content": row.get("content") or None,
        "tool_chain": row.get("tool_chain") or None,
        "extra": row.get("extra") or None,
        "ts": canon_ts(row.get("ts")),
    }


def _memory_transform(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "tenant_id": tenant_id,
        "id": str(row.get("id")),
        "memory_type": str(row.get("memory_type") or ""),
        "summary": str(row.get("summary") or ""),
        "content_hash": str(row.get("content_hash") or ""),
        "embedding": sanitize_embedding(row.get("embedding")),
        "reinforcement": int(row.get("reinforcement") or 1),
        "emotional_weight": int(row.get("emotional_weight") or 0),
        "extra_json": row.get("extra_json") or None,
        "source_ref": row.get("source_ref") or None,
        "happened_at": canon_ts(row.get("happened_at")),
        "status": str(row.get("status") or "active"),
        "created_at": canon_ts(row.get("created_at")) or now,
        "updated_at": canon_ts(row.get("updated_at")) or now,
    }


def _consolidation_transform(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "source_ref": str(row.get("source_ref")),
        "item_id": row.get("item_id"),
        "created_at": canon_ts(row.get("created_at"))
        or datetime.now(timezone.utc).isoformat(),
    }


def _replacement_transform(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {"tenant_id": tenant_id}
    for col in (
        "id", "old_item_id", "old_memory_type", "old_summary", "old_source_ref",
        "old_happened_at", "old_extra_json", "new_item_id", "new_memory_type",
        "new_summary", "new_source_ref", "new_happened_at", "new_extra_json",
        "relation_type", "source_ref", "created_at",
    ):
        if col in row:
            out[col] = row[col]
    out["old_happened_at"] = canon_ts(out.get("old_happened_at"))
    out["new_happened_at"] = canon_ts(out.get("new_happened_at"))
    out["created_at"] = canon_ts(out.get("created_at")) or datetime.now(
        timezone.utc
    ).isoformat()
    return out


def _delivery_transform(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "session_key": str(row.get("session_key")),
        "delivery_key": str(row.get("delivery_key")),
        "sent_at": canon_ts(row.get("sent_at")),
    }


def _state_transform(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "session_key": str(row.get("session_key")),
        "key": str(row.get("key")),
        "value": row.get("value") or None,
    }


def _cot_transform(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "id": row.get("id"),
        "session_key": str(row.get("session_key")),
        "ts": canon_ts(row.get("ts")),
    }


_TICK_LOG_COLS = (
    "id", "tick_id", "session_key", "started_at", "finished_at", "gate_exit",
    "terminal_action", "skip_reason", "steps_taken", "alert_count",
    "content_count", "context_count", "interesting_ids", "discarded_ids",
    "cited_ids", "drift_entered", "final_message", "proactive_effects_json",
)


def _tick_log_transform(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {"tenant_id": tenant_id}
    for col in _TICK_LOG_COLS:
        out[col] = row.get(col) if col in row else None
    out["started_at"] = canon_ts(out["started_at"])
    out["finished_at"] = canon_ts(out["finished_at"])
    return out


_TICK_STEP_COLS = (
    "id", "tick_id", "step_index", "phase", "tool_name", "tool_call_id",
    "tool_args_json", "tool_result_text", "terminal_action_after",
    "skip_reason_after", "interesting_ids_after", "discarded_ids_after",
    "cited_ids_after", "final_message_after",
)


def _tick_step_transform(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {"tenant_id": tenant_id}
    for col in _TICK_STEP_COLS:
        out[col] = row.get(col) if col in row else None
    return out


def _job_transform(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "tenant_id": tenant_id,
        "id": str(row.get("id") or ""),
        "trigger": str(row.get("trigger") or "at"),
        "tier": str(row.get("tier") or "instant"),
        "fire_at": canon_ts(row.get("fire_at")),
        "channel": str(row.get("channel") or ""),
        "chat_id": str(row.get("chat_id") or ""),
        "interval_seconds": row.get("interval_seconds"),
        "cron_expr": row.get("cron_expr"),
        "message": row.get("message"),
        "prompt": row.get("prompt"),
        "name": row.get("name"),
        "timezone": str(row.get("timezone") or "UTC"),
        "run_count": int(row.get("run_count") or 0),
        "enabled": bool(row.get("enabled", True)),
        "created_at": now,
        "updated_at": now,
    }


def _app_config_transform(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "tenant_id": tenant_id,
        "key": str(row.get("key")),
        "value_json": row.get("value_json") or "{}",
        "created_at": now,
        "updated_at": now,
    }


def _ident_job(row: dict[str, Any]) -> str:
    return str(row.get("channel") or "")


# ── 表规格注册表（FK 顺序）───────────────────────────────────────────────────

TABLE_SPECS: list[TableSpec] = [
    TableSpec(
        name="sessions", db=SESSIONS_DB, source="sessions",
        columns=["tenant_id", "key", "channel", "chat_id", "metadata_json",
                 "last_consolidated", "last_user_at", "last_proactive_at",
                 "next_seq", "created_at", "updated_at"],
        pkey=["tenant_id", "key"], source_order=["key"],
        identity=_ident_session_key, transform=_session_transform,
    ),
    TableSpec(
        name="messages", db=SESSIONS_DB, source="messages",
        columns=["tenant_id", "id", "session_key", "seq", "role", "content",
                 "tool_chain", "extra", "ts"],
        pkey=["tenant_id", "id"], source_order=["session_key", "seq"],
        required=("session_key", "ts"),
        identity=_ident_session, transform=_message_transform,
    ),
    TableSpec(
        name="memory_items", db=MEMORY_DB, source="memory_items",
        columns=["tenant_id", "id", "memory_type", "summary", "content_hash",
                 "embedding", "reinforcement", "emotional_weight", "extra_json",
                 "source_ref", "happened_at", "status", "created_at",
                 "updated_at"],
        pkey=["tenant_id", "id"], source_order=["id"], partitioned=True,
        identity=_ident_memory, transform=_memory_transform,
    ),
    TableSpec(
        name="memory_replacements", db=MEMORY_DB, source="memory_replacements",
        columns=["tenant_id", "id", "old_item_id", "old_memory_type",
                 "old_summary", "old_source_ref", "old_happened_at",
                 "old_extra_json", "new_item_id", "new_memory_type",
                 "new_summary", "new_source_ref", "new_happened_at",
                 "new_extra_json", "relation_type", "source_ref", "created_at"],
        pkey=["tenant_id", "id"], source_order=["id"],
        identity=_ident_memory, transform=_replacement_transform,
    ),
    TableSpec(
        name="consolidation_events", db=MEMORY_DB, source="consolidation_events",
        columns=["tenant_id", "source_ref", "item_id", "created_at"],
        pkey=["tenant_id", "source_ref"], source_order=["source_ref"],
        identity=_ident_memory, transform=_consolidation_transform,
    ),
    TableSpec(
        name="deliveries", db=PROACTIVE_DB, source="deliveries",
        columns=["tenant_id", "session_key", "delivery_key", "sent_at"],
        pkey=["tenant_id", "session_key", "delivery_key"],
        source_order=["session_key", "delivery_key"],
        required=("sent_at",),
        identity=_ident_session, transform=_delivery_transform,
    ),
    TableSpec(
        name="session_state", db=PROACTIVE_DB, source="session_state",
        columns=["tenant_id", "session_key", "key", "value"],
        pkey=["tenant_id", "session_key", "key"],
        source_order=["session_key", "key"],
        identity=_ident_session, transform=_state_transform,
    ),
    TableSpec(
        name="context_only_timestamps", db=PROACTIVE_DB,
        source="context_only_timestamps",
        columns=["tenant_id", "id", "session_key", "ts"],
        pkey=["tenant_id", "id"], source_order=["id"],
        required=("ts",),
        identity=_ident_session, transform=_cot_transform,
    ),
    TableSpec(
        name="tick_log", db=PROACTIVE_DB, source="tick_log",
        columns=["tenant_id", "id", "tick_id", "session_key", "started_at",
                 "finished_at", "gate_exit", "terminal_action", "skip_reason",
                 "steps_taken", "alert_count", "content_count", "context_count",
                 "interesting_ids", "discarded_ids", "cited_ids",
                 "drift_entered", "final_message", "proactive_effects_json"],
        pkey=["tenant_id", "id"], source_order=["id"],
        required=("started_at",),
        identity=_ident_session, transform=_tick_log_transform,
    ),
    TableSpec(
        name="tick_step_log", db=PROACTIVE_DB, source="tick_step_log",
        columns=["tenant_id", "id", "tick_id", "step_index", "phase",
                 "tool_name", "tool_call_id", "tool_args_json",
                 "tool_result_text", "terminal_action_after",
                 "skip_reason_after", "interesting_ids_after",
                 "discarded_ids_after", "cited_ids_after",
                 "final_message_after"],
        pkey=["tenant_id", "id"], source_order=["id"],
        required=("id", "tick_id", "step_index"),
        identity=None,  # tenant 经 tick_id → tick_log.session_key 解析
        transform=_tick_step_transform,
    ),
    TableSpec(
        name="scheduled_jobs", db="json", source="schedules",
        columns=["tenant_id", "id", "trigger", "tier", "fire_at", "channel",
                 "chat_id", "interval_seconds", "cron_expr", "message",
                 "prompt", "name", "timezone", "run_count", "enabled",
                 "created_at", "updated_at"],
        pkey=["tenant_id", "id"], source_order=["id"],
        required=("fire_at",),
        identity=_ident_job, transform=_job_transform,
    ),
    TableSpec(
        name="app_configs", db="json", source="app_configs",
        columns=["tenant_id", "key", "value_json", "created_at", "updated_at"],
        pkey=["tenant_id", "key"], source_order=["key"],
        identity=_ident_memory, transform=_app_config_transform,
    ),
]


def _json_rows(source: SqliteSource, spec: TableSpec) -> list[dict[str, Any]]:
    """JSON 源行（schedules.json / mcp_servers.json / proactive_quota.json）。"""
    ws = source.workspace
    if spec.source == "schedules":
        path = ws / "schedules.json"
        if not path.exists():
            return []
        return list(json.loads(path.read_text(encoding="utf-8")))
    if spec.source == "app_configs":
        rows: list[dict[str, Any]] = []
        mcp = ws / "mcp_servers.json"
        if mcp.exists():
            servers = json.loads(mcp.read_text(encoding="utf-8")).get(
                "servers", {}
            )
            rows.append({"key": "mcp_servers",
                         "value_json": json.dumps(servers,
                                                  ensure_ascii=False)})
        quota = ws / "proactive_quota.json"
        if quota.exists():
            rows.append({"key": "proactive_quota",
                         "value_json": quota.read_text(encoding="utf-8")})
        return rows
    return []


def collect_identities(source: SqliteSource) -> set[str]:
    """预检收集全部源身份（通道 + memory）。用于 mapping 覆盖检查。"""
    identities: set[str] = set()
    for spec in TABLE_SPECS:
        if spec.identity is None:
            continue
        for row in _iter_spec_rows(source, spec):
            identities.add(spec.identity(row))
    return identities


def _iter_spec_rows(
    source: SqliteSource, spec: TableSpec
) -> list[dict[str, Any]]:
    if spec.db == "json":
        return _json_rows(source, spec)
    if not source.table_exists(spec.db, spec.source):
        return []
    return list(source.iter_rows(spec.db, spec.source,
                                 order_by=spec.source_order))


def _resolve_tenant(spec: TableSpec, row: dict[str, Any],
                    mapping: TenantMapping,
                    tick_tenants: dict[str, str]) -> str:
    if spec.name == "tick_step_log":
        tick_id = str(row.get("tick_id", ""))
        if tick_id not in tick_tenants:
            raise MappingError(f"tick_step_log 找不到所属 tick_id: {tick_id!r}")
        return tick_tenants[tick_id]
    if spec.identity is None:
        raise MappingError(f"{spec.name} 缺少源身份解析")
    return mapping.resolve(spec.identity(row))


def _order_key(row: dict[str, Any], spec: TableSpec) -> list[Any]:
    return [row.get(c) for c in spec.source_order]


def import_table(
    writer: BulkPgWriter,
    source: SqliteSource,
    spec: TableSpec,
    mapping: TenantMapping,
    ckpt: RunCheckpoint,
    cfg: MigrationConfig,
    tick_tenants: dict[str, str] | None = None,
) -> dict[str, Any]:
    """导入单张表；返回统计。支持 checkpoint 续传与幂等。"""
    rows = _iter_spec_rows(source, spec)
    stat: dict[str, Any] = {
        "table": spec.name,
        "source_count": len(rows),
        "inserted": 0,
        "skipped_resume": 0,
        "skipped_invalid": 0,
        "skipped_conflict": 0,
        "tenants": {},
    }
    if not rows:
        return stat

    high_water = ckpt.get(spec.name)
    tick_tenants = tick_tenants or {}
    # 预检所有待导入行的 tenant（未映射 / 孤儿 tick 在此阶段抛错，零写入）。
    tenant_of: dict[int, str] = {}
    for i, row in enumerate(rows):
        if high_water is not None and _order_key(row, spec) <= high_water:
            stat["skipped_resume"] += 1
            continue
        tenant_of[i] = _resolve_tenant(spec, row, mapping, tick_tenants)

    if spec.partitioned:
        writer.provision_partitions(sorted(set(tenant_of.values())))

    batch: list[dict[str, Any]] = []
    batch_keys: list[list[Any]] = []
    for i, row in enumerate(rows):
        if high_water is not None and _order_key(row, spec) <= high_water:
            continue
        tid = tenant_of[i]
        out = spec.transform(row, tid) if spec.transform else _same_columns(row, tid)
        if any(out.get(c) is None for c in spec.required):
            stat["skipped_invalid"] += 1
            continue
        batch.append(out)
        batch_keys.append(_order_key(row, spec))
        if len(batch) >= cfg.batch_size:
            _flush_batch(writer, spec, cfg, ckpt, batch, batch_keys, stat)
            batch = []
            batch_keys = []
    if batch:
        _flush_batch(writer, spec, cfg, ckpt, batch, batch_keys, stat)
    return stat


def _flush_batch(
    writer: BulkPgWriter,
    spec: TableSpec,
    cfg: MigrationConfig,
    ckpt: RunCheckpoint,
    batch: list[dict[str, Any]],
    batch_keys: list[list[Any]],
    stat: dict[str, Any],
) -> None:
    """写一批：COPY → commit → 写 checkpoint（先提交后记录，见 D2）。"""
    inserted = writer.copy_table(spec.name, spec.columns, batch,
                                 partitioned=spec.partitioned)
    writer.commit()
    stat["inserted"] += inserted
    stat["skipped_conflict"] += len(batch) - inserted
    for row in batch:
        stat["tenants"][row["tenant_id"]] = (
            stat["tenants"].get(row["tenant_id"], 0) + 1
        )
    ckpt.set(spec.name, batch_keys[-1])
    save_checkpoint(cfg.checkpoint_path, ckpt)


def _preflight(
    source: SqliteSource, mapping: TenantMapping
) -> tuple[set[str], dict[str, str]]:
    """零写入预检：mapping 覆盖 + tick_step_log 孤儿检测。"""
    identities = collect_identities(source)
    mapping.require_covered(sorted(identities))

    tick_tenants: dict[str, str] = {}
    if source.table_exists(PROACTIVE_DB, "tick_log"):
        for row in source.iter_rows(PROACTIVE_DB, "tick_log"):
            tid = mapping.resolve(
                channel_of(str(row.get("session_key", "")))
            )
            tick_tenants[str(row.get("tick_id", ""))] = tid

    if source.table_exists(PROACTIVE_DB, "tick_step_log"):
        orphans = sorted(
            str(row.get("tick_id", ""))
            for row in source.iter_rows(PROACTIVE_DB, "tick_step_log")
            if str(row.get("tick_id", "")) not in tick_tenants
        )
        if orphans:
            raise MappingError(
                "tick_step_log 存在孤儿 tick_id（无对应 tick_log）: "
                + ", ".join(repr(o) for o in orphans[:20])
            )
    return identities, tick_tenants


def run_import(
    cfg: MigrationConfig,
    *,
    dry_run: bool = False,
    max_batch_limit: int | None = None,
) -> dict[str, Any]:
    """执行导入（预检 → FK 顺序逐表 COPY）。dry_run 只做预检与计划输出。"""
    start = time.monotonic()
    source = SqliteSource(cfg.workspace)
    if cfg.mapping_path is None:
        raise MappingError("必须提供 tenant mapping 文件（--mapping），拒绝导入")
    mapping = TenantMapping.load(cfg.mapping_path)

    identities, tick_tenants = _preflight(source, mapping)
    cfg.ensure_dirs()
    ckpt = load_checkpoint(cfg.checkpoint_path) or RunCheckpoint(run_id=cfg.run_id)

    plan = {
        "dry_run": dry_run,
        "source_identities": sorted(identities),
        "mapping": mapping.to_dict(),
        "tenants": sorted(mapping.tenants()),
        "tables": [t.name for t in TABLE_SPECS],
    }

    if dry_run:
        return {
            "meta": cfg.to_meta(),
            "plan": plan,
            "result": "preflight_ok",
            "elapsed_sec": 0.0,
        }

    writer = BulkPgWriter(cfg.pg_url)
    results: dict[str, Any] = {}
    try:
        for spec in TABLE_SPECS:
            results[spec.name] = import_table(
                writer, source, spec, mapping, ckpt, cfg, tick_tenants
            )
            if max_batch_limit is not None and sum(
                r["inserted"] for r in results.values()
            ) >= max_batch_limit:
                break
    finally:
        writer.close()

    elapsed = time.monotonic() - start
    report: dict[str, Any] = {
        "meta": cfg.to_meta(),
        "elapsed_sec": round(elapsed, 3),
        "plan": plan,
        "tables": results,
        "ok": True,
    }
    evidence_path = _write_import_result(cfg, report)
    report["evidence_path"] = str(evidence_path)
    return report


def _write_import_result(cfg: MigrationConfig, report: dict[str, Any]) -> Path:
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.results_dir / f"{cfg.run_id}-import.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path
