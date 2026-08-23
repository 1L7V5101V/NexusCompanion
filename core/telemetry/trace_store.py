"""进程内 turn 追踪存储：按 turn_id 聚合 phase span（C0 任务 3.1）。

环形缓冲上限（默认 4096 条 span）防进程内无限膨胀；`dump_json` 把全部 span 落盘。
`trace_phase` 上下文管理器供 PassiveTurnPipeline phase 边界使用：进入记起点，
正常退出记 ok，异常退出记 fail（不吞异常）。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["TraceSpan", "TraceStore", "trace_phase"]

_DEFAULT_MAX_ENTRIES = 4096


@dataclass
class TraceSpan:
    """一个 phase 的完整 span：起止、耗时、结果。"""

    turn_id: str
    phase: str
    started_at: float
    duration_ms: float
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "phase": self.phase,
            "started_at": self.started_at,
            "duration_ms": round(self.duration_ms, 3),
            "status": self.status,
            "detail": self.detail,
        }


class TraceStore:
    """进程内环形缓冲：按 turn_id 聚合 phase span，可落盘 JSON。"""

    def __init__(
        self,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        dump_dir: str | Path | None = None,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = max_entries
        self._dump_dir = Path(dump_dir) if dump_dir else None
        self._spans: deque[TraceSpan] = deque(maxlen=max_entries)
        self._lock = threading.RLock()

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def begin(self, turn_id: str, phase: str) -> "_SpanHandle":
        return _SpanHandle(self, turn_id, phase, time.perf_counter(), time.time())

    def _record(self, span: TraceSpan) -> None:
        with self._lock:
            self._spans.append(span)

    def spans_for(self, turn_id: str) -> list[TraceSpan]:
        with self._lock:
            return [s for s in self._spans if s.turn_id == turn_id]

    def phases_for(self, turn_id: str) -> list[str]:
        return [s.phase for s in self.spans_for(turn_id)]

    def all_spans(self) -> list[TraceSpan]:
        with self._lock:
            return list(self._spans)

    def snapshot(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.all_spans()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._spans)

    def dump_json(self, path: str | Path | None = None) -> Path:
        """把全部 span 落盘 JSON。path 缺省用 dump_dir/<ts>.json。"""
        target = Path(path) if path else self._default_dump_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def _default_dump_path(self) -> Path:
        if self._dump_dir is None:
            raise ValueError("dump_json 需要显式 path，或在构造时传 dump_dir")
        name = time.strftime("%Y%m%dT%H%M%SZ") + f"-{time.time_ns()}.json"
        return self._dump_dir / name


class _SpanHandle:
    def __init__(
        self,
        store: TraceStore,
        turn_id: str,
        phase: str,
        started_perf: float,
        started_epoch: float,
    ) -> None:
        self._store = store
        self._turn_id = turn_id
        self._phase = phase
        self._started_perf = started_perf
        self._started_epoch = started_epoch
        self._finished = False

    def finish(self, *, status: str, detail: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        self._store._record(  # noqa: SLF001 - 同模块内部协作
            TraceSpan(
                turn_id=self._turn_id,
                phase=self._phase,
                started_at=self._started_epoch,
                duration_ms=(time.perf_counter() - self._started_perf) * 1000.0,
                status=status,
                detail=detail,
            )
        )


class trace_phase:
    """phase 边界追踪上下文管理器。

    store 为 None 时是 no-op（保持生产默认零开销路径）。异常正常向外传播，
    同时把 span 记为 fail。
    """

    def __init__(
        self,
        store: TraceStore | None,
        turn_id: str,
        phase: str,
    ) -> None:
        self._handle = store.begin(turn_id, phase) if store is not None else None

    def __enter__(self) -> "trace_phase":
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> bool:
        if self._handle is not None:
            if exc_type is not None:
                self._handle.finish(status="fail", detail=exc_type.__name__)
            else:
                self._handle.finish(status="ok")
        return False
