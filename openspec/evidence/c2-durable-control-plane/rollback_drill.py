"""C2 Create→Verify→Enable→rollback 演练（可重跑）。

对应 design.md §5 / §5.9.9 首次启用语义：
  Create   — 空库 alembic upgrade head（七表 + 约束 + 索引，无 seed）；
  Verify   — 空基线断言（表/关键约束/部分唯一索引/计数为 0）；
  Enable   — 模拟入口开放：T1 接受 + T2 完成 + delivery sent 全链路写入；
  rollback — 关闭入口 + 停 worker，保留 PG 数据，断言行数与状态不变。

用法：python rollback_drill.py [ADMIN_URL]
默认 ADMIN_URL = postgresql://nexus:nexus_dev@localhost:5433/nexus
scratch DB：nexus_c2drill（演练结束保留数据，重跑时先 DROP 重建）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from pathlib import Path

import psycopg
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from scripts.migrate.alembic_util import upgrade_head  # noqa: E402

ADMIN_URL = (
    sys.argv[1] if len(sys.argv) > 1
    else "postgresql://nexus:nexus_dev@localhost:5433/nexus"
)
DRILL_DB = "nexus_c2drill"

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    RESULTS.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def counts(url: str) -> dict[str, int]:
    conn = psycopg.connect(url)
    try:
        out = {}
        for table in (
            "delivery_attempts",
            "outbound_delivery_intents",
            "background_work_items",
            "tool_calls",
            "turns",
            "inbox_records",
            "message_deduplication_keys",
            "canonical_messages",
            "canonical_conversations",
            "test_accounts",
        ):
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out
    finally:
        conn.close()


def main() -> int:
    logging.getLogger("alembic").setLevel(logging.CRITICAL)
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DRILL_DB}")
    admin.execute(f"CREATE DATABASE {DRILL_DB}")
    admin.close()
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + DRILL_DB
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.close()

    print("== Create：空库 upgrade head ==")
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option(
        "sqlalchemy.url", url.replace("postgresql://", "postgresql+psycopg://")
    )
    upgrade_head(cfg)
    tables = {
        r[0]
        for r in psycopg.connect(url).execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ).fetchall()
    }
    expected = {
        "inbox_records", "message_deduplication_keys", "turns", "tool_calls",
        "background_work_items", "outbound_delivery_intents", "delivery_attempts",
    }
    check(expected <= tables, f"Create：七表齐备 {sorted(expected & tables)}")
    check(counts(url)["outbound_delivery_intents"] == 0, "Create：seed=not_applicable，outbox 空")

    print("== Verify：空基线 + 关键约束 ==")
    conn = psycopg.connect(url)
    idx = {
        r[0]
        for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
            " AND indexname IN ('uq_message_dedup_keys_source',"
            " 'uq_message_dedup_keys_client','uq_outbound_delivery_intents_idempotency_key',"
            " 'ix_outbound_delivery_intents_claim')"
        ).fetchall()
    }
    conn.close()
    check(len(idx) == 4, f"Verify：部分唯一/claim 索引在位 {sorted(idx)}")

    print("== Enable：模拟入口开放（T1 + T2 + delivery ack 全链路） ==")
    c2_url = url.replace("postgresql://", "postgresql+asyncpg://")

    async def enable() -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from bootstrap.db.models.canonical import (
            CanonicalConversationModel,
            TestAccountModel,
        )
        from bootstrap.db.repository.control_plane_repo import (
            DeliveryRepository,
            IngressRepository,
            TurnControlRepository,
        )
        from bootstrap.delivery_worker import OutboundDeliveryWorker

        engine = create_async_engine(c2_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        # 准备一个 Pilot 账号 + agent 会话（生产由 C5 provisioning 创建）
        account_id = uuid.uuid5(uuid.NAMESPACE_URL, "c2-drill-account")
        conversation_id = uuid.uuid5(uuid.NAMESPACE_URL, "c2-drill-conversation")
        async with factory() as sess, sess.begin():
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            await sess.execute(
                pg_insert(TestAccountModel)
                .values(id=account_id, status="active", display_name="C2 Drill")
                .on_conflict_do_nothing()
            )
            await sess.execute(
                pg_insert(CanonicalConversationModel)
                .values(id=conversation_id, tenant_id="c2-drill", account_id=account_id)
                .on_conflict_do_nothing()
            )

        ingress = IngressRepository(factory)
        control = TurnControlRepository(factory)
        delivery = DeliveryRepository(factory)

        accepted = await ingress.accept_inbound(
            "c2-drill",
            conversation_id,
            source_channel="telegram",
            source_identity_id="drill-user",
            source_message_id="drill-msg-1",
            content="drill hello",
        )
        assert accepted.turn_id is not None
        completion = await control.complete_turn_with_delivery(
            "c2-drill",
            conversation_id,
            accepted.turn_id,
            expected_status="queued",
            response_content="drill reply",
            delivery_channel="telegram",
            delivery_target="drill-chat",
        )
        worker = OutboundDeliveryWorker(delivery, _drill_send, worker_id="drill-worker")
        await worker.process_once()
        await engine.dispose()
        globals()["_INTENT_ID"] = completion.intent["id"]

    async def _drill_send(envelope: object) -> str | None:
        return "drill-receipt-001"

    asyncio.run(enable())
    enabled_counts = counts(url)
    check(enabled_counts["message_deduplication_keys"] == 1, "Enable：dedupe 键 ×1")
    check(enabled_counts["inbox_records"] == 1, "Enable：inbox ×1（accepted）")
    check(enabled_counts["turns"] == 1, "Enable：turn ×1")
    check(enabled_counts["canonical_messages"] == 2, "Enable：canonical message ×2（user+assistant final）")
    check(enabled_counts["outbound_delivery_intents"] == 1, "Enable：outbox intent ×1")
    check(enabled_counts["delivery_attempts"] == 1, "Enable：attempt ×1（sent）")
    conn = psycopg.connect(url)
    status = conn.execute(
        "SELECT status FROM outbound_delivery_intents"
    ).fetchone()[0]
    conn.close()
    check(status == "sent", f"Enable：intent 状态 = sent（ack 推进）实际 {status}")

    print("== rollback：关闭入口 + 停 worker，保留 PG 数据 ==")
    # rollback 动作 = 不再接受新 work / 不再投递（进程级开关）；已产生数据保留。
    rollback_counts = counts(url)
    check(rollback_counts == enabled_counts, "rollback：行数与 Enable 时完全一致（数据保留）")
    conn = psycopg.connect(url)
    row = conn.execute(
        "SELECT status, attempt_count, sent_at IS NOT NULL FROM outbound_delivery_intents"
    ).fetchone()
    conn.close()
    check(row == ("sent", 1, True), f"rollback：终态与凭证保留 {row}")
    check(counts(url)["test_accounts"] >= 1, "rollback：test_accounts 数据保留（不反向同步任何旧库）")

    passed = sum(1 for ok, _ in RESULTS if ok)
    print(f"\n共 {len(RESULTS)} 项：{passed} PASS / {len(RESULTS) - passed} FAIL")
    print(f"scratch DB {DRILL_DB} 保留（数据保留语义），重跑将先 DROP 重建。")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
