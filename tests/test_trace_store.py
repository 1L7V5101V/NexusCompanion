"""TraceStore 单测：按 turn_id 聚合、环形缓冲上限、落盘 JSON（C0 任务 3.1）。"""

from __future__ import annotations

import json

from core.telemetry.trace_store import TraceStore, trace_phase


def _run_phase(store: TraceStore, turn_id: str, phase: str, *, fail: bool = False) -> None:
    with trace_phase(store, turn_id, phase):
        if fail:
            raise RuntimeError("boom")


def test_spans_aggregate_by_turn_id() -> None:
    store = TraceStore()
    _run_phase(store, "t1", "before_turn")
    _run_phase(store, "t1", "reasoner")
    _run_phase(store, "t2", "before_turn")

    assert store.phases_for("t1") == ["before_turn", "reasoner"]
    assert store.phases_for("t2") == ["before_turn"]
    spans = store.spans_for("t1")
    assert all(s.status == "ok" for s in spans)
    assert all(s.duration_ms >= 0 for s in spans)
    # 同一 turn 的 phase 顺序保持进入顺序。
    assert store.spans_for("t1")[0].phase == "before_turn"
    assert store.spans_for("t1")[1].phase == "reasoner"


def test_failed_phase_marked_fail_and_exception_propagates() -> None:
    store = TraceStore()
    try:
        _run_phase(store, "t1", "reasoner", fail=True)
    except RuntimeError:
        pass
    span = store.spans_for("t1")[0]
    assert span.status == "fail"
    assert span.detail == "RuntimeError"


def test_trace_phase_noop_without_store() -> None:
    with trace_phase(None, "t1", "before_turn"):
        pass


def test_ring_buffer_caps_at_max_entries() -> None:
    store = TraceStore(max_entries=5)
    for i in range(10):
        _run_phase(store, f"t{i}", "before_turn")
    assert len(store) == 5
    # 环形缓冲淘汰最旧：只剩后 5 条。
    assert {s.turn_id for s in store.all_spans()} == {"t5", "t6", "t7", "t8", "t9"}
    assert store.max_entries == 5


def test_dump_json_round_trip(tmp_path) -> None:
    store = TraceStore(dump_dir=tmp_path)
    _run_phase(store, "t1", "before_turn")
    try:
        _run_phase(store, "t1", "reasoner", fail=True)
    except RuntimeError:
        pass

    path = store.dump_json()
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) == 2
    by_phase = {d["phase"]: d for d in data}
    assert by_phase["before_turn"]["status"] == "ok"
    assert by_phase["reasoner"]["status"] == "fail"
    assert all({"turn_id", "phase", "started_at", "duration_ms", "status"} <= set(d) for d in data)


def test_dump_json_explicit_path(tmp_path) -> None:
    store = TraceStore()
    _run_phase(store, "t1", "before_turn")
    target = tmp_path / "nested" / "trace.json"
    path = store.dump_json(target)
    assert path == target
    assert json.loads(path.read_text(encoding="utf-8"))[0]["turn_id"] == "t1"


def test_max_entries_must_be_positive() -> None:
    try:
        TraceStore(max_entries=0)
    except ValueError:
        pass
