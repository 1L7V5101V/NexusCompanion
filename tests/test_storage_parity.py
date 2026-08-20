"""SQLite vs PG 存储等价性 parity 测试（M4 迁入自 tmp/parity_*.py）。

约束：本文件两个测试在 M4 之后任何存储层改动下都必须保持绿色。
只断言 store 层等价性；工厂/接线由 tests/test_storage_factory.py 覆盖。
PG 不可用时自动 skip（不 fail）；PG URL 可用 NEXUS_TEST_PG_URL 覆盖。
"""
import os
import uuid
from datetime import datetime

import psycopg
import pytest

from infra.storage.postgres_memory_store import PostgresMemoryStore
from infra.storage.postgres_session_store import PostgresSessionStore
from memory2.store import MemoryStore2
from session.store import SessionStore

DIM = 1024

PG_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture
def pg_url() -> str:
    if not _pg_alive(PG_URL):
        pytest.skip(f"本地 PG 不可用（{PG_URL}），跳过 postgres parity")
    return PG_URL


def _unique_tenant(prefix: str) -> str:
    return f"{prefix}_{os.getpid()}_{uuid.uuid4().hex[:6]}"


def _clean_pg(url: str, tenant: str) -> None:
    conn = psycopg.connect(url, autocommit=True)
    for table in ("consolidation_events", "memory_replacements", "memory_items"):
        conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant,))
    conn.execute("DELETE FROM messages WHERE tenant_id = %s", (tenant,))
    conn.execute("DELETE FROM sessions WHERE tenant_id = %s", (tenant,))
    conn.close()


# ---------------------------------------------------------------------------
# memory parity（原 tmp/parity_smoke.py）
# ---------------------------------------------------------------------------


def _emb(base: int, mag: float = 1.0) -> list[float]:
    v = [0.0] * DIM
    v[base] = mag
    return v


@pytest.mark.postgres
def test_memory_parity(pg_url: str, tmp_path) -> None:
    tenant = _unique_tenant("parity_mem")
    _clean_pg(pg_url, tenant)

    sq = MemoryStore2(tmp_path / "m.db", vec_dim=DIM)
    pg = PostgresMemoryStore(pg_url, tenant_id=tenant, vec_dim=DIM)
    try:
        # 同数据：5 条 note + 2 条 event
        seeds = [
            ("note", "猫抓板 拆家", _emb(0)),
            ("note", "猫咪疫苗 时间", _emb(1)),
            ("note", "公司团建 爬山", _emb(2)),
            ("note", "周末 火锅 朋友", _emb(3)),
            ("note", "读书 习惯 养成", _emb(4)),
            ("event", "给猫打了疫苗", _emb(5)),
            ("event", "团建去了爬山", _emb(6)),
        ]
        for mtype, summary, e in seeds:
            sq.upsert_item(
                mtype, summary, e, extra={"scope_channel": "tg", "scope_chat_id": "1"}
            )
            pg.upsert_item(
                mtype, summary, e, extra={"scope_channel": "tg", "scope_chat_id": "1"}
            )

        # vector_search top-k 对比（tie 时 HNSW/KNN 截断顺序可不一致，只比明确项）
        for q, k, types in [(_emb(1), 3, None), (_emb(6), 2, ["event"]), (_emb(2), 5, None)]:
            rs = sq.vector_search(q, top_k=k)
            rp = pg.vector_search(q, top_k=k)
            rs_sig = [(r["summary"], round(r["score"], 4)) for r in rs if r["score"] > 0.01]
            rp_sig = [(r["summary"], round(r["score"], 4)) for r in rp if r["score"] > 0.01]
            assert rs_sig == rp_sig, (q, rs_sig, rp_sig)
            for a, b in zip(rs, rp):
                assert abs(a["score"] - b["score"]) < 1e-3, (a["score"], b["score"])

        # scope 过滤
        rs = sq.vector_search(
            _emb(1), top_k=3, require_scope_match=True,
            scope_channel="tg", scope_chat_id="1",
        )
        rp = pg.vector_search(
            _emb(1), top_k=3, require_scope_match=True,
            scope_channel="tg", scope_chat_id="1",
        )
        rs_sig = [(r["summary"], round(r["score"], 4)) for r in rs if r["score"] > 0.01]
        rp_sig = [(r["summary"], round(r["score"], 4)) for r in rp if r["score"] > 0.01]
        assert rs_sig == rp_sig, (rs_sig, rp_sig)

        # keyword_search_summary
        rs = sq.keyword_search_summary(["猫"], limit=10)
        rp = pg.keyword_search_summary(["猫"], limit=10)
        assert [r["summary"] for r in rs] == [r["summary"] for r in rp], (rs, rp)

        # get_all_with_embedding 数量
        assert len(sq.get_all_with_embedding()) == len(pg.get_all_with_embedding())

        # find_similar_recent_events
        rs = sq.find_similar_recent_events(_emb(6), days_back=30, threshold=0.5, top_k=3)
        rp = pg.find_similar_recent_events(_emb(6), days_back=30, threshold=0.5, top_k=3)
        assert len(rs) == len(rp), (rs, rp)

        # list_by_type
        assert len(sq.list_by_type("event")) == len(pg.list_by_type("event"))
    finally:
        sq.close()
        pg.close()


# ---------------------------------------------------------------------------
# session parity（原 tmp/parity_session_smoke.py）
# ---------------------------------------------------------------------------


def _norm_ts(v):
    if v is None:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()


def _msg_norm(m):
    """归一化消息：排除 timestamp（时区表示不同）与 extra 合并字段。"""
    stable = tuple(m[k] for k in ("id", "session_key", "seq", "role", "content"))
    tc = None
    if "tool_chain" in m:
        tc = tuple(sorted(m["tool_chain"].items()))
    extra = tuple(
        sorted(
            (k, v)
            for k, v in m.items()
            if k
            not in (
                "id", "session_key", "seq", "role", "content", "timestamp",
                "tool_chain", "in_source_ref",
            )
        )
    )
    return (stable, extra, tc)


def _seed_sessions(st) -> None:
    st.create_session(key="tg:1", metadata={"topic": "cat"})
    st.create_session(
        key="tg:2", metadata={}, last_proactive_at="2026-08-02T10:00:00+08:00"
    )
    st.update_presence("tg:1", last_user_at="2026-08-03T09:30:00+08:00")
    st.insert_message(
        "tg:1", role="user", content="你好 猫咪", ts="2026-08-03T10:00:00+08:00", seq=0
    )
    st.insert_message(
        "tg:1", role="assistant", content="猫咪 疫苗 时间",
        ts="2026-08-03T10:01:00+08:00", seq=1,
        tool_chain={"tool": "x"}, extra={"sent": True},
    )
    st.insert_message(
        "tg:1", role="user", content="周末 去爬山", ts="2026-08-03T10:02:00+08:00", seq=2
    )
    st.insert_message(
        "tg:2", role="user", content="读书 习惯", ts="2026-08-04T10:00:00+08:00", seq=0
    )


@pytest.mark.postgres
def test_session_parity(pg_url: str, tmp_path) -> None:
    tenant = _unique_tenant("parity_sess")
    _clean_pg(pg_url, tenant)

    sq = SessionStore(tmp_path / "s.db")
    pg = PostgresSessionStore(pg_url, tenant_id=tenant)
    try:
        _seed_sessions(sq)
        _seed_sessions(pg)

        # 1. next_seq 序列一致
        for k in ["tg:3", "tg:3", "tg:3"]:
            assert sq.next_seq(k) == pg.next_seq(k)

        # 2. session meta 一致（created_at 由 now() 自动生成，只比格式与 metadata）
        assert _norm_ts(sq.get_session_meta("tg:1")["created_at"]) is not None
        assert _norm_ts(pg.get_session_meta("tg:1")["created_at"]) is not None
        assert sq.get_session_meta("tg:1")["metadata"] == pg.get_session_meta("tg:1")["metadata"]
        assert sq.get_session_meta("tg:nope") == pg.get_session_meta("tg:nope") is None

        # 3. fetch_session_messages 归一化一致
        ms = [_msg_norm(m) for m in sq.fetch_session_messages("tg:1")]
        mp = [_msg_norm(m) for m in pg.fetch_session_messages("tg:1")]
        assert ms == mp, (ms, mp)

        # 4. list_sessions 集合一致
        assert {r["key"] for r in sq.list_sessions()} == {r["key"] for r in pg.list_sessions()}

        # 5. presence
        assert _norm_ts(sq.get_presence("tg:1")["last_user_at"]) == _norm_ts(
            pg.get_presence("tg:1")["last_user_at"]
        )
        assert set(sq.list_presence()) == set(pg.list_presence())
        assert _norm_ts(sq.most_recent_user_at()) == _norm_ts(pg.most_recent_user_at())
        assert [c["chat_id"] for c in sq.get_channel_metadata("tg")] == [
            c["chat_id"] for c in pg.get_channel_metadata("tg")
        ]

        # 6. dashboard 分页
        ds, ts_ = sq.list_sessions_for_dashboard(has_proactive=True)
        dp, tp = pg.list_sessions_for_dashboard(has_proactive=True)
        assert [r["key"] for r in ds] == [r["key"] for r in dp] and ts_ == tp
        md, mt = sq.list_messages_for_dashboard(q="猫咪")
        mdp, mtp = pg.list_messages_for_dashboard(q="猫咪")
        assert mt == mtp and [_msg_norm(m) for m in md] == [_msg_norm(m) for m in mdp]

        # 7. search_messages：集合 + 总数一致（排序可不同：FTS bm25 vs 命中词数）
        for q in ["猫咪", "疫苗", "爬山", "不存在词xyz"]:
            rs, cts = sq.search_messages(q)
            rp, ctp = pg.search_messages(q)
            assert cts == ctp, (q, cts, ctp)
            assert {m["id"] for m in rs} == {m["id"] for m in rp}, (q, rs, rp)
            assert [_msg_norm(m) for m in sorted(rs, key=lambda x: x["id"])] == [
                _msg_norm(m) for m in sorted(rp, key=lambda x: x["id"])
            ]

        # 8. fetch_by_ids 保序 + context
        assert [m["id"] for m in sq.fetch_by_ids(["tg:1:2", "tg:1:0"])] == [
            m["id"] for m in pg.fetch_by_ids(["tg:1:2", "tg:1:0"])
        ]
        cs = sq.fetch_by_ids_with_context(["tg:1:0"], context=1)
        cp = pg.fetch_by_ids_with_context(["tg:1:0"], context=1)
        assert len(cs) == len(cp)
        assert all("in_source_ref" in m for m in cp)

        # 9. delete 语义
        assert sq.delete_message("tg:1:2") == pg.delete_message("tg:1:2") is True
        assert sq.delete_message("tg:1:2") == pg.delete_message("tg:1:2") is False
        n1 = sq.delete_messages_batch(["tg:1:0", "tg:2:0"])
        n2 = pg.delete_messages_batch(["tg:1:0", "tg:2:0"])
        assert n1 == n2 == 2
        try:
            sq.delete_session("tg:1")
            sq_raise = True
        except ValueError:
            sq_raise = False
        try:
            pg.delete_session("tg:1")
            pg_raise = True
        except ValueError:
            pg_raise = False
        assert sq_raise == pg_raise is False
        assert sq.delete_session("tg:1", cascade=True) == pg.delete_session("tg:1", cascade=True) is True

        # 10. delete_sessions_batch
        assert sq.delete_sessions_batch(["tg:2"], cascade=True) == pg.delete_sessions_batch(
            ["tg:2"], cascade=True
        ) == 1
    finally:
        sq.close()
        pg.close()
