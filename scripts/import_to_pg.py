#!/usr/bin/env python
"""Import data from existing SQLite databases into PostgreSQL.

Usage:
    uv run python scripts/import_to_pg.py [--workspace PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bootstrap.db.config import DatabaseConfig
from bootstrap.db.engine import create_engine, create_session_factory
from bootstrap.db.repository.memory_repo import AsyncMemoryRepository
from bootstrap.db.repository.proactive_repo import AsyncProactiveRepository
from bootstrap.db.repository.session_repo import AsyncSessionRepository

DEFAULT_TENANT = "default"


def get_workspace(path: str | None = None) -> Path:
    if path:
        return Path(path)
    return Path.home() / ".nexus" / "workspace"


def open_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


async def import_sessions(repo: AsyncSessionRepository, workspace: Path, tenant_id: str) -> None:
    path = workspace / "sessions.db"
    if not path.exists():
        print("  [SKIP] sessions.db")
        return
    conn = open_sqlite(path)
    rows = conn.execute("SELECT * FROM sessions").fetchall()
    print(f"  {len(rows)} sessions...")
    from bootstrap.db.models.session import SessionModel

    for row in rows:
        meta = row["metadata"]
        meta_dict = json.loads(meta) if meta else None
        key: str = row["key"]
        channel = key.split(":", 1)[0] if ":" in key else ""
        chat_id = key.split(":", 1)[1] if ":" in key else key
        await repo.create_session(
            tenant_id, key=key, channel=channel, chat_id=chat_id, metadata=meta_dict,
            last_user_at=row["last_user_at"], last_proactive_at=row["last_proactive_at"],
        )
        await repo.next_seq(tenant_id, key)
        async with repo._sf() as sess:
            existing = await sess.get(SessionModel, (key,))
            if existing:
                existing.next_seq = row["next_seq"] or 0
                await sess.commit()
    msg_rows = conn.execute("SELECT * FROM messages ORDER BY session_key, seq").fetchall()
    print(f"  {len(msg_rows)} messages...")
    for row in msg_rows:
        await repo.insert_message(
            tenant_id, row["session_key"], message_id=row["id"], seq=row["seq"],
            role=row["role"], content=row["content"] or "", ts=row["ts"],
            tool_chain=row["tool_chain"], extra=row["extra"],
        )
    conn.close()


async def import_memory(repo: AsyncMemoryRepository, workspace: Path, tenant_id: str) -> None:
    path = workspace / "memory" / "memory.db"
    if not path.exists():
        print("  [SKIP] memory.db")
        return
    conn = open_sqlite(path)
    rows = conn.execute("SELECT * FROM memory_items").fetchall()
    print(f"  {len(rows)} memory items...")
    for row in rows:
        await repo.upsert_item(
            tenant_id, row["id"], memory_type=row["memory_type"], summary=row["summary"],
            content_hash=row["content_hash"], embedding=row["embedding"],
            reinforcement=row["reinforcement"] or 1, emotional_weight=row["emotional_weight"] or 0,
            extra_json=row["extra_json"], source_ref=row["source_ref"],
            happened_at=row["happened_at"], status=row["status"] or "active",
        )
    for row in conn.execute("SELECT * FROM consolidation_events"):
        await repo.upsert_consolidation_event(tenant_id, row["source_ref"], item_id=row["item_id"])
    conn.close()


async def import_proactive(repo: AsyncProactiveRepository, workspace: Path, tenant_id: str) -> None:
    path = workspace / "proactive.db"
    if not path.exists():
        print("  [SKIP] proactive.db")
        return
    conn = open_sqlite(path)
    for row in conn.execute("SELECT * FROM tick_log ORDER BY id"):
        started_at = _parse_ts(row["started_at"])
        if started_at is None:
            continue
        await repo.record_tick_log_start(tenant_id, row["tick_id"], row["session_key"], started_at)
        finished_at = _parse_ts(row["finished_at"])
        await repo.record_tick_log_finish(
            tenant_id, row["tick_id"], finished_at=finished_at or started_at,
            gate_exit=row["gate_exit"], terminal_action=row["terminal_action"],
            skip_reason=row["skip_reason"], steps_taken=row["steps_taken"],
            alert_count=row["alert_count"], content_count=row["content_count"],
            context_count=row["context_count"], interesting_ids=row["interesting_ids"],
            discarded_ids=row["discarded_ids"], cited_ids=row["cited_ids"],
            final_message=row["final_message"],
            proactive_effects_json=row["proactive_effects_json"],
        )
    for row in conn.execute("SELECT * FROM deliveries ORDER BY session_key, delivery_key"):
        sent_at = _parse_ts(row["sent_at"])
        if sent_at:
            await repo.record_delivery(tenant_id, row["session_key"], row["delivery_key"], sent_at)
    for row in conn.execute("SELECT * FROM context_only_timestamps ORDER BY id"):
        ts = _parse_ts(row["ts"])
        if ts:
            await repo.mark_context_only_send(tenant_id, row["session_key"], ts)
    for row in conn.execute("SELECT * FROM session_state"):
        await repo.set_session_state(tenant_id, row["session_key"], row["key"], row["value"])
    conn.close()


async def import_json(sf: Any, workspace: Path, tenant_id: str) -> None:
    from bootstrap.db.models.extras import AppConfigModel, ScheduledJobModel

    sched_path = workspace / "schedules.json"
    if sched_path.exists():
        data = json.loads(sched_path.read_text())
        print(f"  {len(data)} scheduled jobs...")
        async with sf() as sess:
            for item in data:
                sess.add(ScheduledJobModel(
                    tenant_id=tenant_id, id=item.get("id", ""),
                    trigger=item.get("trigger", "at"), tier=item.get("tier", "instant"),
                    fire_at=datetime.fromisoformat(item["fire_at"]).replace(tzinfo=timezone.utc),
                    channel=item.get("channel", ""), chat_id=item.get("chat_id", ""),
                    interval_seconds=item.get("interval_seconds"),
                    cron_expr=item.get("cron_expr"), message=item.get("message"),
                    prompt=item.get("prompt"), name=item.get("name"),
                    timezone=item.get("timezone", "UTC"), run_count=item.get("run_count", 0),
                    enabled=item.get("enabled", True),
                ))
            await sess.commit()
    mcp_path = workspace / "mcp_servers.json"
    if mcp_path.exists():
        servers = json.loads(mcp_path.read_text()).get("servers", {})
        print(f"  {len(servers)} MCP server configs...")
        async with sf() as sess:
            sess.add(AppConfigModel(
                tenant_id=tenant_id, key="mcp_servers",
                value_json=json.dumps(servers), updated_at=datetime.now(timezone.utc),
            ))
            await sess.commit()
    quota_path = workspace / "proactive_quota.json"
    if quota_path.exists():
        print("  proactive quota...")
        async with sf() as sess:
            sess.add(AppConfigModel(
                tenant_id=tenant_id, key="proactive_quota",
                value_json=quota_path.read_text(), updated_at=datetime.now(timezone.utc),
            ))


async def async_main(workspace: Path, tenant_id: str, db_url: str) -> None:
    cfg = DatabaseConfig(url=db_url)
    engine = create_engine(cfg)
    sf = create_session_factory(engine)
    session_repo = AsyncSessionRepository(sf)
    memory_repo = AsyncMemoryRepository(sf)
    proactive_repo = AsyncProactiveRepository(sf)

    print("=== Sessions ===")
    await import_sessions(session_repo, workspace, tenant_id)
    print("=== Memory ===")
    await import_memory(memory_repo, workspace, tenant_id)
    print("=== Proactive ===")
    await import_proactive(proactive_repo, workspace, tenant_id)
    print("=== JSON configs ===")
    await import_json(sf, workspace, tenant_id)
    await engine.dispose()
    print("\nDone!")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import SQLite data into PostgreSQL")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    parser.add_argument("--db-url", default="postgresql+asyncpg://postgres@localhost:5432/nexus")
    args = parser.parse_args()
    ws = get_workspace(args.workspace)
    print(f"Workspace: {ws}\nTenant: {args.tenant}\nDB: {args.db_url}\n")
    asyncio.run(async_main(ws, args.tenant, args.db_url))


if __name__ == "__main__":
    main()
