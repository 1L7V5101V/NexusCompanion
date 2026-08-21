"""SQLite vs PG 存储契约测试：共用一套矩阵（M4H-1）。

同一份测试 body 参数化跑 `sqlite` 与 `postgres` 两个 backend；断言以 SQLite
（MemoryStore2/SessionStore，文档基线）为 reference。只断言 store 层契约等价性；
工厂/接线由 tests/test_storage_factory.py 覆盖。PG 不可用时自动 skip（不 fail）；
PG URL 可用 NEXUS_TEST_PG_URL 覆盖。
"""
import os
import uuid
from datetime import datetime
from pathlib import Path

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
# memory contract matrix
# ---------------------------------------------------------------------------


def _emb(base: int, mag: float = 1.0) -> list[float]:
    v = [0.0] * DIM
    v[base] = mag
    return v


def _open_memory(backend: str, pg_url: str, tmp_path: Path, tenant: str) -> MemoryStore2 | PostgresMemoryStore:
    if backend == "sqlite":
        return MemoryStore2(tmp_path / f"m_{uuid.uuid4().hex[:6]}.db", vec_dim=DIM)
    return PostgresMemoryStore(pg_url, tenant_id=tenant, vec_dim=DIM)


def _seed_memory(st) -> None:
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
        st.upsert_item(
            mtype, summary, e, extra={"scope_channel": "tg", "scope_chat_id": "1"}
        )


def _assert_memory_contract(subject, ref) -> None:
    # vector_search top-k（tie 时 HNSW/KNN 截断顺序可不一致，只比明确项与分数）
    for q, k, types in [(_emb(1), 3, None), (_emb(6), 2, ["event"]), (_emb(2), 5, None)]:
        rs = subject.vector_search(q, top_k=k)
        rp = ref.vector_search(q, top_k=k)
        rs_sig = [(r["summary"], round(r["score"], 4)) for r in rs if r["score"] > 0.01]
        rp_sig = [(r["summary"], round(r["score"], 4)) for r in rp if r["score"] > 0.01]
        assert rs_sig == rp_sig, (q, rs_sig, rp_sig)
        for a, b in zip(rs, rp):
            assert abs(a["score"] - b["score"]) < 1e-3, (a["score"], b["score"])

    # scope 过滤
    rs = subject.vector_search(
        _emb(1), top_k=3, require_scope_match=True,
        scope_channel="tg", scope_chat_id="1",
    )
    rp = ref.vector_search(
        _emb(1), top_k=3, require_scope_match=True,
        scope_channel="tg", scope_chat_id="1",
    )
    rs_sig = [(r["summary"], round(r["score"], 4)) for r in rs if r["score"] > 0.01]
    rp_sig = [(r["summary"], round(r["score"], 4)) for r in rp if r["score"] > 0.01]
    assert rs_sig == rp_sig, (rs_sig, rp_sig)

    # keyword_search_summary
    rs = subject.keyword_search_summary(["猫"], limit=10)
    rp = ref.keyword_search_summary(["猫"], limit=10)
    assert [r["summary"] for r in rs] == [r["summary"] for r in rp], (rs, rp)

    # get_all_with_embedding 数量
    assert len(subject.get_all_with_embedding()) == len(ref.get_all_with_embedding())

    # find_similar_recent_events
    rs = subject.find_similar_recent_events(_emb(6), days_back=30, threshold=0.5, top_k=3)
    rp = ref.find_similar_recent_events(_emb(6), days_back=30, threshold=0.5, top_k=3)
    assert len(rs) == len(rp), (rs, rp)

    # list_by_type
    assert len(subject.list_by_type("event")) == len(ref.list_by_type("event"))


@pytest.mark.postgres
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_memory_contract(backend: str, pg_url: str, tmp_path) -> None:
    tenant = _unique_tenant("ct_mem")
    if backend == "postgres":
        _clean_pg(pg_url, tenant)
    ref = MemoryStore2(tmp_path / f"ref_m_{uuid.uuid4().hex[:6]}.db", vec_dim=DIM)
    subject = _open_memory(backend, pg_url, tmp_path, tenant)
    try:
        _seed_memory(subject)
        _seed_memory(ref)
        _assert_memory_contract(subject, ref)
    finally:
        subject.close()
        ref.close()


# ---------------------------------------------------------------------------
# session contract matrix
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


def _open_session(backend: str, pg_url: str, tmp_path: Path, tenant: str) -> SessionStore | PostgresSessionStore:
    if backend == "sqlite":
        return SessionStore(tmp_path / f"s_{uuid.uuid4().hex[:6]}.db")
    return PostgresSessionStore(pg_url, tenant_id=tenant)


def _assert_session_contract(subject, ref) -> None:
    # 1. next_seq 序列一致
    for k in ["tg:3", "tg:3", "tg:3"]:
        assert subject.next_seq(k) == ref.next_seq(k)

    # 2. session meta 一致（created_at 由 now() 自动生成，只比格式与 metadata）
    assert _norm_ts(subject.get_session_meta("tg:1")["created_at"]) is not None
    assert _norm_ts(ref.get_session_meta("tg:1")["created_at"]) is not None
    assert subject.get_session_meta("tg:1")["metadata"] == ref.get_session_meta("tg:1")["metadata"]
    assert subject.get_session_meta("tg:nope") == ref.get_session_meta("tg:nope") is None

    # 3. fetch_session_messages 归一化一致
    ms = [_msg_norm(m) for m in subject.fetch_session_messages("tg:1")]
    mp = [_msg_norm(m) for m in ref.fetch_session_messages("tg:1")]
    assert ms == mp, (ms, mp)

    # 4. list_sessions 集合一致
    assert {r["key"] for r in subject.list_sessions()} == {r["key"] for r in ref.list_sessions()}

    # 5. presence
    assert _norm_ts(subject.get_presence("tg:1")["last_user_at"]) == _norm_ts(
        ref.get_presence("tg:1")["last_user_at"]
    )
    assert set(subject.list_presence()) == set(ref.list_presence())
    assert _norm_ts(subject.most_recent_user_at()) == _norm_ts(ref.most_recent_user_at())
    assert [c["chat_id"] for c in subject.get_channel_metadata("tg")] == [
        c["chat_id"] for c in ref.get_channel_metadata("tg")
    ]

    # 6. dashboard 分页
    ds, ts_ = subject.list_sessions_for_dashboard(has_proactive=True)
    dp, tp = ref.list_sessions_for_dashboard(has_proactive=True)
    assert [r["key"] for r in ds] == [r["key"] for r in dp] and ts_ == tp
    md, mt = subject.list_messages_for_dashboard(q="猫咪")
    mdp, mtp = ref.list_messages_for_dashboard(q="猫咪")
    assert mt == mtp and [_msg_norm(m) for m in md] == [_msg_norm(m) for m in mdp]

    # 7. search_messages：集合 + 总数一致（排序可不同：FTS bm25 vs 命中词数）
    for q in ["猫咪", "疫苗", "爬山", "不存在词xyz"]:
        rs, cts = subject.search_messages(q)
        rp, ctp = ref.search_messages(q)
        assert cts == ctp, (q, cts, ctp)
        assert {m["id"] for m in rs} == {m["id"] for m in rp}, (q, rs, rp)
        assert [_msg_norm(m) for m in sorted(rs, key=lambda x: x["id"])] == [
            _msg_norm(m) for m in sorted(rp, key=lambda x: x["id"])
        ]

    # 8. fetch_by_ids 保序 + context
    assert [m["id"] for m in subject.fetch_by_ids(["tg:1:2", "tg:1:0"])] == [
        m["id"] for m in ref.fetch_by_ids(["tg:1:2", "tg:1:0"])
    ]
    cs = subject.fetch_by_ids_with_context(["tg:1:0"], context=1)
    cp = ref.fetch_by_ids_with_context(["tg:1:0"], context=1)
    assert len(cs) == len(cp)
    assert all("in_source_ref" in m for m in cp)

    # 9. delete 语义
    assert subject.delete_message("tg:1:2") == ref.delete_message("tg:1:2") is True
    assert subject.delete_message("tg:1:2") == ref.delete_message("tg:1:2") is False
    n1 = subject.delete_messages_batch(["tg:1:0", "tg:2:0"])
    n2 = ref.delete_messages_batch(["tg:1:0", "tg:2:0"])
    assert n1 == n2 == 2
    try:
        subject.delete_session("tg:1")
        subject_raise = True
    except ValueError:
        subject_raise = False
    try:
        ref.delete_session("tg:1")
        ref_raise = True
    except ValueError:
        ref_raise = False
    assert subject_raise == ref_raise is False
    assert subject.delete_session("tg:1", cascade=True) == ref.delete_session("tg:1", cascade=True) is True

    # 10. delete_sessions_batch
    assert subject.delete_sessions_batch(["tg:2"], cascade=True) == ref.delete_sessions_batch(
        ["tg:2"], cascade=True
    ) == 1


@pytest.mark.postgres
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_session_contract(backend: str, pg_url: str, tmp_path) -> None:
    tenant = _unique_tenant("ct_sess")
    if backend == "postgres":
        _clean_pg(pg_url, tenant)
    ref = SessionStore(tmp_path / f"ref_s_{uuid.uuid4().hex[:6]}.db")
    subject = _open_session(backend, pg_url, tmp_path, tenant)
    try:
        _seed_sessions(subject)
        _seed_sessions(ref)
        _assert_session_contract(subject, ref)
    finally:
        subject.close()
        ref.close()
