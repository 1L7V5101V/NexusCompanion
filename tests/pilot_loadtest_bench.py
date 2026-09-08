"""Pilot 交互层容量/隔离压测基准（PILOT_ROADMAP §5.9.5 + §7.1 数据字段）。

独立脚本（非 pytest 用例，pytest 不收集 pilot_*_bench.py），显式运行：

    python tests/pilot_loadtest_bench.py [--only s1,s2,s3,s4] [--flood N]
        [--burst N] [--tenants K] [--turns M] [--recovery-turns N]
        [--out PATH] [--timeout S]

场景全部驱动仓库内的生产类（不复制实现逻辑），LLM/turn 用可控延时 stub
模拟（等价 mock LLM，隔离上游变量，测量自系统容量边界）：

- s1 有界队列拒绝曲线：MessageBus global interactive 有界（默认 128），洪峰
  发布至满载 → 第 129 条即拒（AdmissionOverloadError/Retry-After）；已接受项
  FIFO 保序、零静默丢失、内存有界。再加 PassiveMessageWorker per-tenant
  pending（默认 16）真路径：单租户突发 200 条，第 17 条起明确拒绝回执
  （nexus_overload outbound），无静默丢弃。
- s2 慢消费者 WS 分级降级：真实 WebChatChannel._broadcast + 真实 _Connection，
  消费者完全停滞：soft 192 起 delta 丢弃并发一次 replay_required；hard 256
  用 close code 1013 断开；terminal 帧全程盖 seq 进重放 buffer，重连按
  after_seq 补拉 100% 覆盖、无重复、无丢失。
- s3 tenant 隔离与优先级：TenantLaneRouter K 租户 × M 轮 turn，同租户串行
  零重叠、跨租户异步（总墙钟不随 K 增长）；interactive 活跃期同租户
  maintenance 被延后、跨租户 maintenance 不被阻塞；ResourceSemaphores
  LLM=30 时第 31 个并发等待（记录等待分布）。
- s4 重启恢复扫描：真实 SessionStore(SQLite) 播种 N 条非终态 turn →
  StartupRecoveryScanner 全量处置为 cancelled，记录扫描耗时、动作分布、
  无 apply 异常、扫描后残留 0。

结果写入 --out（默认 results/pilot_loadtest.json），同时打印人类可读摘要。
每条 metric 只来自实测计数；meta 记录 git rev 与运行参数，方便复盘。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

# 独立脚本：把仓库根加入 sys.path，使 agent/bus/infra 可直接导入。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.admission.lanes import MaintenanceDeferred, TenantLaneRouter
from agent.admission.queues import (
    GLOBAL_INTERACTIVE_QUEUE,
    PER_TENANT_PENDING_INTERACTIVE,
    WS_OUTBOUND_HARD_LIMIT,
    WS_OUTBOUND_SOFT_LIMIT,
    AdmissionOverloadError,
)
from agent.admission.recovery import (
    RecoveryAction,
    StartupRecoveryScanner,
    TurnAuditRecoverySource,
)
from agent.admission.resources import ResourceKind, ResourceSemaphores
from agent.control.models import TurnRecord, TurnStatus
from bootstrap.passive_worker import PassiveMessageWorker
from bus.events import InboundMessage, OutboundMessage
from bus.queue import MessageBus
from infra.channels.web_chat_channel import WebChatChannel, _Connection
from infra.channels.web_chat_protocol import (
    CLOSE_OVERLOAD,
    message_delta,
    turn_completed,
)
from session.store import SessionStore

logger = logging.getLogger(__name__)

_BENCH_NAME = "pilot_loadtest"
_DEFAULT_OUT = Path("results") / "pilot_loadtest.json"


# ── 工具 ────────────────────────────────────────────────────────────


def _git_rev() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


async def _delay_s(seconds: float) -> None:
    """自旋式高精度延时。

    Windows ProactorEventLoop 对 <~15ms 的 asyncio.sleep 会合并（busy 时近 0ms、
    idle 时按 15.6ms 量子唤醒），节奏性延时不可靠；这里以 perf_counter(QPC)
    为目标自旋，实测 5/10/20/50ms 在 busy/idle 下均精确命中。
    """
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        await asyncio.sleep(0)


def _queued_types(conn: _Connection) -> list[str]:
    return [
        str(entry[0].get("type")) for entry in list(conn.outbound._queue)
    ]  # noqa: SLF001


def _pct(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(
        len(sorted_values) - 1, int(round(pct / 100.0 * (len(sorted_values) - 1)))
    )
    return sorted_values[idx]


def _checks(pairs: list[tuple[str, bool]]) -> list[dict[str, object]]:
    return [{"check": desc, "passed": ok} for desc, ok in pairs]


# ── S1：有界队列拒绝曲线 ────────────────────────────────────────────


async def _s1_global_overload(flood: int) -> dict[str, object]:
    """MessageBus 入站有界 128：洪峰 → 即时拒绝、已接受项保序不丢、内存有界。"""
    bus = MessageBus()
    sent: list[InboundMessage] = []
    for i in range(flood):
        sent.append(
            InboundMessage(
                channel="bench",
                sender="s1",
                chat_id=f"c{i}",
                content=f"m{i}",
                media=[],
                metadata={"bench_seq": i},
            )
        )

    accepted: list[int] = []
    rejected = 0
    first_reject_retry_after: float | None = None
    tracemalloc.start()
    base = tracemalloc.take_snapshot()
    t0 = time.monotonic()
    max_depth = 0
    for msg in sent:
        try:
            await bus.publish_inbound(msg)
        except AdmissionOverloadError as exc:
            rejected += 1
            if first_reject_retry_after is None:
                first_reject_retry_after = exc.retry_after
                assert exc.limit_kind == "global_interactive"
        max_depth = max(max_depth, bus.inbound_size)
    wall_s = time.monotonic() - t0
    peak = tracemalloc.take_snapshot()
    tracemalloc.stop()
    growth_bytes = sum(stat.size_diff for stat in peak.compare_to(base, "filename"))

    # 已接受 128 条全部可消费、严格 FIFO、无重复无缺失。
    drained: list[int] = []
    for _ in range(bus.inbound_size):
        item = cast(InboundMessage, await bus.consume_inbound())
        drained.append(int(cast(str, item.metadata["bench_seq"])))
    fifo_ok = drained == list(range(GLOBAL_INTERACTIVE_QUEUE))
    accepted = drained

    metrics: dict[str, object] = {
        "queue_capacity": GLOBAL_INTERACTIVE_QUEUE,
        "flood_total": flood,
        "accepted": len(accepted),
        "rejected": rejected,
        "rejection_starts_at_attempt": len(accepted) + 1,
        "first_reject_retry_after_s": first_reject_retry_after,
        "flood_wall_s": round(wall_s, 4),
        "max_queue_depth_observed": max_depth,
        "peak_mem_growth_bytes": growth_bytes,
        "fifo_preserved": fifo_ok,
        "silent_loss": len(accepted) != GLOBAL_INTERACTIVE_QUEUE,
    }
    ok = (
        len(accepted) == GLOBAL_INTERACTIVE_QUEUE
        and rejected == flood - GLOBAL_INTERACTIVE_QUEUE
        and fifo_ok
        and max_depth <= GLOBAL_INTERACTIVE_QUEUE
    )
    return {
        "ok": ok,
        "metrics": metrics,
        "checks": _checks(
            [
                ("第 129 条发布即抛 AdmissionOverloadError(global_interactive)", ok),
                ("拒绝携带 Retry-After", first_reject_retry_after is not None),
                ("已接受条数 == 容量 128", len(accepted) == GLOBAL_INTERACTIVE_QUEUE),
                ("已接受项 FIFO 保序、无重复无缺失", fifo_ok),
                ("队列深度不超过容量", max_depth <= GLOBAL_INTERACTIVE_QUEUE),
            ]
        ),
    }


class _FakeTurnHandle:
    """可控延时 turn：5ms 模拟一次“完成一轮”（mock LLM）。"""

    def __init__(self, turn_id: str, reply: str, delay_s: float) -> None:
        self.id = turn_id
        self._reply = reply
        self._delay = delay_s

    async def result(self) -> Any:
        await _delay_s(self._delay)
        return SimpleNamespaceResult(self._reply)


class SimpleNamespaceResult:
    def __init__(self, reply: str) -> None:
        self.status = TurnStatus.COMPLETED
        self.final_response = reply
        self.items = [SimpleNamespaceItem(kind=SimpleNamespaceItemKind(), data={})]


class SimpleNamespaceItemKind:
    value = "assistantMessage"


class SimpleNamespaceItem:
    def __init__(self, kind: Any, data: dict[str, Any]) -> None:
        self.kind = kind
        self.data = data


class _FakeRuntime:
    """满足 PassiveMessageWorker 调用面的可控 runtime（无 LLM、无持久化）。"""

    def __init__(self, delay_s: float = 0.005) -> None:
        self._delay = delay_s

    async def wait_thread_available(self, session_key: str) -> None:
        return None

    async def start_turn(self, request: Any) -> _FakeTurnHandle:
        return _FakeTurnHandle(
            turn_id=f"turn-{uuid4().hex[:8]}",
            reply=f"ok:{request.input}",
            delay_s=self._delay,
        )


async def _s1_per_tenant_burst(burst: int) -> dict[str, object]:
    """PassiveMessageWorker 真路径：per-tenant pending 16，突发第 17 条起拒绝。"""
    bus = MessageBus()
    runtime = _FakeRuntime(delay_s=0.005)
    worker = PassiveMessageWorker(bus, cast(Any, runtime), cast(Any, None))

    replies: list[OutboundMessage] = []

    async def _on_outbound(msg: OutboundMessage) -> None:
        replies.append(msg)

    bus.subscribe_outbound("bench", _on_outbound)
    dispatcher = asyncio.create_task(bus.dispatch_outbound())
    worker_task = asyncio.create_task(worker.run())

    sent = [
        InboundMessage(
            channel="bench",
            sender="s1",
            chat_id="local",
            content=f"m{i}",
            media=[],
            metadata={"bench_seq": i},
            tenant_id="tenant-a",
        )
        for i in range(burst)
    ]

    # 采样 per-tenant lane 队列深度峰值。
    depth_peak = 0
    lanes: dict[str, asyncio.Queue[object]] = worker._lane_queues  # noqa: SLF001
    sampler_stop = asyncio.Event()

    async def _sample() -> None:
        nonlocal depth_peak
        while not sampler_stop.is_set():
            depth_peak = max(depth_peak, *(q.qsize() for q in lanes.values()), 0)
            await asyncio.sleep(0.0005)

    bus_rejected_indexes: set[int] = set()
    sampler = asyncio.create_task(_sample())
    t_start = time.monotonic()
    # 逐条发布、每条间让出事件循环，让 worker 持续消费，避免 burst 一次占满
    # global bus(128) 掩盖 per-tenant 语义；bus 满仍单独记录为指标。
    for i, m in enumerate(sent):
        try:
            await bus.publish_inbound(m)
        except AdmissionOverloadError:
            bus_rejected_indexes.add(i)
        await asyncio.sleep(0)
    expected_ok_ids = {
        f"ok:{m.content}"
        for idx, m in enumerate(sent)
        if idx not in bus_rejected_indexes
    }

    # 等 worker 消化完：每条消息恰好收到一种终态回复（ok 或 nexus_overload），
    # 计数到齐 burst 即全部结束。
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        ok_ids_now = {r.content for r in replies if r.content.startswith("ok:")}
        ov_count_now = sum(1 for r in replies if bool(r.metadata.get("nexus_overload")))
        if len(ok_ids_now) + ov_count_now >= burst and worker.rejected_inbound > 0:
            break
        await asyncio.sleep(0.005)
    settled_s = time.monotonic() - t_start

    # 再留 200ms 观察计数稳定，然后收尾。
    await asyncio.sleep(0.2)
    sampler_stop.set()
    await sampler
    worker.stop()
    worker_task.cancel()
    dispatcher.cancel()
    await asyncio.gather(worker_task, dispatcher, return_exceptions=True)

    ok_replies = [r for r in replies if r.content.startswith("ok:")]
    overload_replies = [r for r in replies if bool(r.metadata.get("nexus_overload"))]
    ok_ids = {r.content for r in ok_replies}
    accepted = len(ok_ids)
    rejected = worker.rejected_inbound
    # 无静默丢失：每条消息恰好收到一种终态回复（ok 或 overload 回执），计数到齐。
    no_loss = (
        ok_ids <= expected_ok_ids
        and len(overload_replies) == rejected
        and accepted + rejected + len(bus_rejected_indexes) == burst
    )
    # 无重复/无乱序：每条被接受消息恰好回一次 ok。
    no_dup = len(ok_replies) == len(ok_ids) and len(overload_replies) == rejected

    metrics: dict[str, object] = {
        "per_tenant_pending_capacity": PER_TENANT_PENDING_INTERACTIVE,
        "burst_total": burst,
        "bus_overload_rejected": len(bus_rejected_indexes),
        "accepted": accepted,
        "rejected_by_per_tenant_overload": rejected,
        "rejection_ratio": round(rejected / burst, 4) if burst else 0.0,
        "max_lane_depth_observed": depth_peak,
        "overload_reply_count": len(overload_replies),
        "settle_wall_s": round(settled_s, 3),
        "no_silent_loss": no_loss,
        "no_dup_or_reorder": no_dup,
        "sustained_ok_turn_rate_s": (
            round(accepted / settled_s, 1) if settled_s else 0.0
        ),
    }
    ok = (
        accepted > 0
        and rejected > 0
        and no_loss
        and no_dup
        and len(overload_replies) == rejected
        and depth_peak <= PER_TENANT_PENDING_INTERACTIVE
    )
    return {
        "ok": ok,
        "metrics": metrics,
        "checks": _checks(
            [
                (
                    "per-tenant lane 队列深度不超过 16",
                    depth_peak <= PER_TENANT_PENDING_INTERACTIVE,
                ),
                ("突发超额部分被明确拒绝（rejected>0）", rejected > 0),
                ("拒绝产生 nexus_overload 回执", len(overload_replies) == rejected),
                (
                    "无静默丢失：每条消息恰好收到 ok 或 overload 回执之一",
                    no_loss and no_dup,
                ),
            ]
        ),
    }


# ── S2：慢消费者 WS 分级降级 + 重连重放 ─────────────────────────────


class _StubWebSocket:
    """最小 WebSocket 假件：只记录 close；不做任何发送（消费者停滞）。"""

    def __init__(self) -> None:
        self.closed_with: tuple[int, str] | None = None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


def _bind_conn(channel: WebChatChannel, ws: Any, conn: _Connection) -> None:
    channel._connections[ws] = conn  # noqa: SLF001


async def _s2_slow_consumer(deltas: int, terminals: int) -> dict[str, object]:
    """消费者完全停滞：soft 丢 delta+replay_required；hard 1013 断开；重放覆盖。"""

    # ── soft 段：只有 delta，无消费者排空 ──
    ch_soft = WebChatChannel()
    ws_soft = _StubWebSocket()
    conn_soft = _Connection(cast(Any, ws_soft), "slow-soft")
    _bind_conn(ch_soft, ws_soft, conn_soft)
    for i in range(deltas):
        ch_soft._broadcast(
            message_delta(turn_id=f"t{i}", content_delta="x" * 4, thinking_delta="")
        )
    await asyncio.sleep(0)
    types = _queued_types(conn_soft)
    deltas_in_queue = types.count("message.delta")
    notices = types.count("replay_required")
    soft_dropped = deltas - deltas_in_queue
    soft_queue_depth = conn_soft.outbound.qsize()

    # ── hard 段：只有 terminal 帧（不可丢、盖 seq），消费者停滞 → 封顶后 1013 ──
    ch_hard = WebChatChannel()
    ws_hard = _StubWebSocket()
    conn_hard = _Connection(cast(Any, ws_hard), "slow-hard")
    _bind_conn(ch_hard, ws_hard, conn_hard)
    for i in range(terminals):
        ch_hard._broadcast(
            turn_completed(turn_id=f"t{i}", content="reply", thinking="", media=[])
        )
    await asyncio.sleep(0.02)  # 让 overload close 任务跑完
    hard_queue_depth = conn_hard.outbound.qsize()
    hard_dropped = terminals - hard_queue_depth
    stamped = len(ch_hard._replay.frames)  # noqa: SLF001
    close_code = ws_hard.closed_with[0] if ws_hard.closed_with else None
    close_reason = ws_hard.closed_with[1] if ws_hard.closed_with else ""

    # ── 重连补拉：新连接按 after_seq 从重放 buffer 补齐 ──
    ws_re = _StubWebSocket()
    conn_re = _Connection(cast(Any, ws_re), "reconnect")
    _bind_conn(ch_hard, ws_re, conn_re)
    full = ch_hard._replay.frames_after(0)  # noqa: SLF001
    tail = ch_hard._replay.frames_after(hard_queue_depth)  # noqa: SLF001
    tail_len = len(tail) if tail is not None else -1
    seqs = [int(f["seq"]) for f in full or []]
    no_dup = len(seqs) == len(set(seqs)) if seqs else True
    ascending = seqs == sorted(seqs) if seqs else True

    metrics: dict[str, object] = {
        "soft_limit": WS_OUTBOUND_SOFT_LIMIT,
        "hard_limit": WS_OUTBOUND_HARD_LIMIT,
        "delta_broadcast": deltas,
        "delta_dropped_at_soft": soft_dropped,
        "replay_required_notices": notices,
        "soft_queue_depth": soft_queue_depth,
        "terminal_broadcast": terminals,
        "terminal_enqueued_before_close": hard_queue_depth,
        "terminal_dropped_at_hard": hard_dropped,
        "close_code": close_code,
        "stamped_terminal_in_replay_buffer": stamped,
        "replay_full_coverage": len(full or []),
        "replay_tail_after_last_received": tail_len,
        "replay_no_dup": no_dup,
        "replay_ascending_seq": ascending,
    }
    ok = (
        soft_dropped > 0
        and notices == 1
        and close_code == CLOSE_OVERLOAD
        and hard_queue_depth == WS_OUTBOUND_HARD_LIMIT
        and stamped == terminals
        and len(full or []) == terminals
        and no_dup
        and ascending
    )
    return {
        "ok": ok,
        "metrics": metrics,
        "checks": _checks(
            [
                ("soft 上限起丢 delta", soft_dropped > 0),
                ("首次丢弃只发一次 replay_required", notices == 1),
                (
                    "hard 上限封顶后以 close code 1013 断开",
                    close_code == CLOSE_OVERLOAD,
                ),
                (
                    "terminal 帧在 hard 段也不静默丢（进重放 buffer）",
                    stamped == terminals,
                ),
                (
                    "重连补拉 100% 覆盖、无重复、seq 递增",
                    len(full or []) == terminals and no_dup and ascending,
                ),
            ]
        ),
    }


# ── S3：tenant 隔离 / interactive 优先 / 资源 semaphore ──────────────


async def _run_turn_batch(
    router: TenantLaneRouter,
    tenant: str,
    n: int,
    delay_s: float,
    intervals: list[list[float]],
) -> None:
    """在 tenant lane 里连续跑 n 轮 work，记录每轮 busy 区间。"""
    for i in range(n):

        async def _work() -> None:
            t0 = time.monotonic()
            await _delay_s(delay_s)
            intervals.append([t0, time.monotonic()])

        await router.run_interactive(tenant, f"{tenant}-{i}", _work)


def _overlap_count(intervals: list[list[float]]) -> int:
    srt = sorted(intervals, key=lambda iv: iv[0])
    violations = 0
    for prev, cur in zip(srt, srt[1:]):
        if cur[0] < prev[1] - 1e-9:
            violations += 1
    return violations


async def _s3_isolation(tenants: int, turns: int) -> dict[str, object]:
    router = TenantLaneRouter()
    delay_s = 0.005

    # 单租户基线
    base_intervals: list[list[float]] = []
    base_router = TenantLaneRouter()
    t0 = time.monotonic()
    await _run_turn_batch(base_router, "alone", turns, delay_s, base_intervals)
    alone_wall_s = time.monotonic() - t0

    # K 租户并发：每个租户各自串行 turns
    t0 = time.monotonic()
    per_tenant: dict[str, list[list[float]]] = {}
    tasks = [
        asyncio.create_task(
            _run_turn_batch(
                router, f"t{k}", turns, delay_s, per_tenant.setdefault(f"t{k}", [])
            )
        )
        for k in range(tenants)
    ]
    await asyncio.gather(*tasks)
    k_wall_s = time.monotonic() - t0
    violations = sum(_overlap_count(iv) for iv in per_tenant.values())
    serial_chain_ms = turns * delay_s * 1000

    # interactive 活跃期：同租户 maintenance 延后，跨租户 maintenance 不阻塞
    busy_tenant = "busy"
    deferrals = 0
    cross_wait_ms: list[float] = []

    async def _hold(duration_s: float) -> None:
        await _delay_s(duration_s)

    holding = asyncio.create_task(
        router.run_interactive(busy_tenant, "hold", lambda: _hold(0.10))
    )

    async def _same_tenant_maintenance() -> None:
        nonlocal deferrals
        try:
            await router.run_maintenance(
                busy_tenant, "m-same", lambda: _delay_s(0.001), acquire_timeout=0.02
            )
        except MaintenanceDeferred:
            deferrals += 1

    async def _cross_tenant_maintenance() -> None:
        t0 = time.monotonic()
        await router.run_maintenance(
            "other", "m-cross", lambda: _delay_s(0.001), acquire_timeout=2.0
        )
        cross_wait_ms.append((time.monotonic() - t0) * 1000)

    await asyncio.sleep(0.01)  # 确保 busy tenant 已进入 interactive
    same_t = asyncio.create_task(_same_tenant_maintenance())
    cross_t = asyncio.create_task(_cross_tenant_maintenance())
    await asyncio.gather(same_t, cross_t, holding)

    # 资源 semaphore：LLM=30，60 个并发 → 第 31 个起等待
    sem = ResourceSemaphores()
    waits_ms: list[float] = []
    active_now = 0
    active_max = 0

    async def _grab() -> None:
        nonlocal active_now, active_max
        t0 = time.monotonic()
        async with sem.gate(ResourceKind.LLM):
            waits_ms.append((time.monotonic() - t0) * 1000)
            active_now += 1
            active_max = max(active_max, active_now)
            await _delay_s(0.03)
            active_now -= 1

    await asyncio.gather(*(_grab() for _ in range(60)))

    waits_sorted = sorted(waits_ms)
    metrics: dict[str, object] = {
        "tenants": tenants,
        "turns_per_tenant": turns,
        "alone_wall_ms": round(alone_wall_s * 1000, 2),
        "k_tenants_wall_ms": round(k_wall_s * 1000, 2),
        "same_tenant_serial_chain_ms": round(serial_chain_ms, 1),
        "wall_ratio_k_vs_alone": (
            round(k_wall_s / alone_wall_s, 3) if alone_wall_s else None
        ),
        "same_tenant_overlap_violations": violations,
        "maintenance_deferred_same_tenant": deferrals,
        "cross_tenant_maintenance_wait_ms_p50": (
            round(_pct(cross_wait_ms, 50), 3) if cross_wait_ms else None
        ),
        "llm_semaphore_limit": 30,
        "llm_concurrent_tasks": 60,
        "llm_active_max_observed": active_max,
        "llm_waited_tasks": len([w for w in waits_ms if w > 1.0]),
        "llm_wait_ms_p50": round(_pct(waits_sorted, 50), 2),
        "llm_wait_ms_p95": round(_pct(waits_sorted, 95), 2),
    }
    ok = (
        violations == 0
        and deferrals >= 1
        and cross_wait_ms
        and (cross_wait_ms[0] < 50)
        and active_max <= 30
    )
    wall_ratio: float | None = cast(float | None, metrics["wall_ratio_k_vs_alone"])
    return {
        "ok": ok,
        "metrics": metrics,
        "checks": _checks(
            [
                ("同租户串行：K×M 轮零重叠执行", violations == 0),
                (
                    "跨租户并发：总墙钟 ≈ 单租户基线",
                    wall_ratio is not None and wall_ratio < 1.6,
                ),
                ("interactive 活跃期同租户 maintenance 被延后", deferrals >= 1),
                (
                    "跨租户 maintenance 不被其他租户 interactive 阻塞",
                    bool(cross_wait_ms) and cross_wait_ms[0] < 50,
                ),
                (
                    "LLM 全局 semaphore=30 生效，第 31 个并发开始等待",
                    active_max <= 30 and len([w for w in waits_ms if w > 1.0]) >= 1,
                ),
            ]
        ),
    }


# ── S4：重启恢复扫描 ────────────────────────────────────────────────


def _seed_nonterminal_turns(
    store: SessionStore, count: int, in_progress_ratio: float = 0.3
) -> None:
    """在真实 SQLite control store 播种 count 条非终态 turn。"""
    for i in range(count):
        turn_id = f"bench-turn-{i:06d}"
        thread_id = f"bench-thread-{i:06d}"
        record = store.create_turn(
            TurnRecord(
                id=turn_id,
                thread_id=thread_id,
                status=TurnStatus.QUEUED,
                input="bench",
                metadata={"tenantId": "tenant-recovery"},
                created_at=datetime.now(UTC),
                items=[],
                usage=None,
                error=None,
            )
        )
        if i < int(count * in_progress_ratio):
            store.transition_turn(
                record.id,
                expected_status=TurnStatus.QUEUED,
                status=TurnStatus.IN_PROGRESS,
            )


async def _s4_recovery_scan(count: int) -> dict[str, object]:
    """StartupRecoveryScanner：N 条非终态 turn → cancelled（IO 放线程池，不阻塞循环）。"""
    return await asyncio.to_thread(_s4_scan_sync, count)


def _s4_scan_sync(count: int) -> dict[str, object]:
    """同步核心：真实 SessionStore(SQLite) + StartupRecoveryScanner。"""
    store: SessionStore | None = None
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "control.db")
        try:
            seed_t0 = time.monotonic()
            _seed_nonterminal_turns(store, count)
            seed_s = time.monotonic() - seed_t0

            scanner = StartupRecoveryScanner([TurnAuditRecoverySource(store)])
            scan_t0 = time.monotonic()
            records = scanner.scan()
            scan_s = time.monotonic() - scan_t0

            by_action: dict[str, int] = {}
            apply_errors = 0
            for rec in records:
                by_action[rec.recovery_action.value] = (
                    by_action.get(rec.recovery_action.value, 0) + 1
                )
                if rec.recovery_result.startswith("apply_failed"):
                    apply_errors += 1

            residual = store.list_non_terminal_turns()
            per_record_ms = scan_s * 1000 / count if count else 0.0

            metrics: dict[str, object] = {
                "seeded_nonterminal_turns": count,
                "seed_s": round(seed_s, 3),
                "scan_elapsed_s": round(scan_s, 4),
                "scan_elapsed_ms_total": round(scan_s * 1000, 2),
                "per_record_ms": round(per_record_ms, 4),
                "records": len(records),
                "by_action": by_action,
                "apply_errors": apply_errors,
                "residual_nonterminal_after_scan": len(residual),
            }
            ok = (
                len(records) == count
                and by_action.get(RecoveryAction.CANCELLED.value, 0) == count
                and apply_errors == 0
                and len(residual) == 0
            )
            return {
                "ok": ok,
                "metrics": metrics,
                "checks": _checks(
                    [
                        ("每条非终态 turn 都有 RecoveryRecord", len(records) == count),
                        (
                            "动作全部可解释为 cancelled",
                            by_action.get("cancelled", 0) == count,
                        ),
                        ("无 apply 异常", apply_errors == 0),
                        ("扫描后残留 0", len(residual) == 0),
                    ]
                ),
            }
        finally:
            store.close()


# ── 主流程 ───────────────────────────────────────────────────────────


SCENARIOS = {
    "s1": _s1_global_overload,
    "s2": _s2_slow_consumer,
    "s3": _s3_isolation,
    "s4": _s4_recovery_scan,
}


async def _run_one(
    name: str, fn: Any, kwargs: dict[str, int], timeout_s: float
) -> dict[str, object]:
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(fn(**kwargs), timeout=timeout_s)
        result["elapsed_s"] = round(time.monotonic() - t0, 3)
        result["name"] = name
        return result
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "name": name,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_s": round(time.monotonic() - t0, 3),
        }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="s1,s2,s3,s4", help="逗号分隔场景子集")
    parser.add_argument("--flood", type=int, default=5000, help="s1 全局洪峰条数")
    parser.add_argument("--burst", type=int, default=200, help="s1 per-tenant 突发条数")
    parser.add_argument("--deltas", type=int, default=400, help="s2 delta 帧数")
    parser.add_argument("--terminals", type=int, default=300, help="s2 terminal 帧数")
    parser.add_argument("--tenants", type=int, default=8, help="s3 租户数")
    parser.add_argument("--turns", type=int, default=25, help="s3 每租户轮数")
    parser.add_argument(
        "--recovery-turns", type=int, default=2000, help="s4 播种非终态 turn 数"
    )
    parser.add_argument("--out", default=str(_DEFAULT_OUT), help="结果 JSON 路径")
    parser.add_argument("--timeout", type=float, default=60.0, help="单场景超时秒")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR)

    only = [s.strip() for s in args.only.split(",") if s.strip()]
    kwargs_map: dict[str, dict[str, int]] = {
        "s1": {"flood": args.flood},
        "s2": {"deltas": args.deltas, "terminals": args.terminals},
        "s3": {"tenants": args.tenants, "turns": args.turns},
        "s4": {"count": args.recovery_turns},
    }

    scenarios: list[dict[str, object]] = []
    # s1 per-tenant 突发并入 s1 结果（跑在 s1 之后单独场景名 s1b）
    for name in only:
        if name == "s1":
            scenarios.append(
                await _run_one(
                    "s1-global-overflow",
                    _s1_global_overload,
                    kwargs_map["s1"],
                    args.timeout,
                )
            )
            scenarios.append(
                await _run_one(
                    "s1b-per-tenant-rejection",
                    _s1_per_tenant_burst,
                    {"burst": args.burst},
                    args.timeout,
                )
            )
        elif name in SCENARIOS:
            scenarios.append(
                await _run_one(name, SCENARIOS[name], kwargs_map[name], args.timeout)
            )

    payload: dict[str, object] = {
        "bench": _BENCH_NAME,
        "run_at": datetime.now(UTC).isoformat(),
        "git_rev": _git_rev(),
        "params": vars(args),
        "interpretation": (
            "mock LLM/turn（可控延时 stub），数据为单进程 dev 环境下自系统容量边界与 "
            "overload/isolation/recovery 契约行为，不代表生产 LLM 端到端延迟。"
        ),
        "scenarios": scenarios,
        "all_ok": all(bool(s.get("ok")) for s in scenarios),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 人类可读摘要 ──
    print(f"\n== pilot_loadtest ({payload['git_rev']}) ==")
    for s in scenarios:
        status = "PASS" if s.get("ok") else "FAIL"
        print(f"[{status}] {s['name']}  ({s.get('elapsed_s')}s)")
        if not s.get("ok"):
            print(f"    error: {s.get('error', 'checks failed')}")
            checks_out = cast(list[dict[str, object]], s.get("checks", []))
            for c in checks_out:
                if not bool(c.get("passed")):
                    print(f"    check: {c.get('check')}")
    print(f"\n结果 JSON: {out.resolve()}")
    return 0 if payload["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
