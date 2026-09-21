"""C15 `WorkQueueWorker` 验收（fake repository，无需 PG）。

覆盖 design ADR-4/ADR-5/ADR-6/ADR-7：lane 串行与跨租户并发、维护类延后、
handler 两段式、失租禁写、未注册 flow、停止语义、背压参数透传。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bootstrap.work_queue_worker import (
    WorkItemEnvelope,
    WorkQueueWorker,
    WorkQueueWorkerConfig,
)

_SENTINEL_SESSION = object()


def _item(
    item_id: str,
    tenant_id: str,
    *,
    work_kind: str = "maintenance",
    flow: str | None = "consolidation",
) -> dict[str, Any]:
    return {
        "id": item_id,
        "tenant_id": tenant_id,
        "work_kind": work_kind,
        "flow": flow,
        "idempotency_key": f"key-{item_id}",
        "conversation_id": None,
        "payload": {"seed": item_id},
        "attempt_count": 0,
    }


class FakeWorkRepo:
    """记录调用的假仓储；`batch` 决定下一轮 `claim_batch` 返回哪些行。"""

    def __init__(self, batch: list[dict[str, Any]] | None = None) -> None:
        self.batch = list(batch or [])
        self.claim_calls: list[dict[str, Any]] = []
        self.claim_attempts = 0
        self.claim_raises = 0  # >0 时前 N 次 claim_batch 抛错（模拟 DB 抖动）
        self.heartbeats: list[str] = []
        self.succeeded: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.released: list[tuple[str, str | None]] = []
        self.mutate_sessions: list[object] = []
        self.lease_lost = False

    async def claim_batch(
        self, owner: str, *, batch_size: int, lease_ttl_seconds: float
    ) -> list[dict[str, Any]]:
        self.claim_attempts += 1
        if self.claim_raises > 0:
            self.claim_raises -= 1
            raise RuntimeError("db down")
        self.claim_calls.append(
            {"owner": owner, "batch_size": batch_size, "lease_ttl": lease_ttl_seconds}
        )
        taken, self.batch = self.batch[:batch_size], self.batch[batch_size:]
        return taken

    async def heartbeat(
        self, tenant_id: str, work_item_id: str, owner: str, *, lease_ttl_seconds: float
    ) -> bool:
        self.heartbeats.append(work_item_id)
        return not self.lease_lost

    async def record_work_succeeded(
        self,
        tenant_id: str,
        work_item_id: str,
        owner: str,
        *,
        mutate: Any = None,
        started_at: Any = None,
    ) -> dict[str, Any]:
        # 真实仓储会在同一事务内调用 mutate；这里用 sentinel 代替 session
        if mutate is not None:
            await mutate(_SENTINEL_SESSION)
        self.succeeded.append(work_item_id)
        return {"id": work_item_id, "status": "succeeded"}

    async def record_work_failed(
        self,
        tenant_id: str,
        work_item_id: str,
        owner: str,
        error: str,
        *,
        max_attempts: int = 5,
        backoff_seconds: tuple[float, ...] = (),
        started_at: Any = None,
    ) -> dict[str, Any]:
        self.failed.append((work_item_id, error))
        return {"id": work_item_id, "status": "failed"}

    async def release_for_retry(
        self,
        tenant_id: str,
        work_item_id: str,
        owner: str,
        *,
        delay_seconds: float = 60.0,
        note: str | None = None,
        started_at: Any = None,
    ) -> dict[str, Any]:
        self.released.append((work_item_id, note))
        return {"id": work_item_id, "status": "queued"}


class RecordingHandler:
    """两段式 handler：记录 execute/persist 的调用与是否重叠。"""

    def __init__(self, *, delay: float = 0.0, fail_in: str | None = None) -> None:
        self.delay = delay
        self.fail_in = fail_in
        self.executed: list[str] = []
        self.persisted: list[tuple[str, object]] = []
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.gate: asyncio.Event | None = None

    async def execute(self, envelope: WorkItemEnvelope) -> object:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            elif self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_in == "execute":
                raise RuntimeError("execute boom")
            self.executed.append(envelope.work_item_id)
            return f"result-{envelope.work_item_id}"
        finally:
            self.active -= 1

    async def persist(
        self, session: Any, envelope: WorkItemEnvelope, result: object
    ) -> None:
        if self.fail_in == "persist":
            raise RuntimeError("persist boom")
        self.persisted.append((envelope.work_item_id, session))


def _worker(repo: FakeWorkRepo, handlers: dict[str, Any], **cfg: Any) -> WorkQueueWorker:
    return WorkQueueWorker(
        repo,  # type: ignore[arg-type]
        handlers,
        config=WorkQueueWorkerConfig(**cfg),
        worker_id="w1",
    )


# ── 配置（task 3.1） ──


def test_config_frozen_defaults() -> None:
    cfg = WorkQueueWorkerConfig()
    assert cfg.lease_ttl_seconds == 60.0
    assert cfg.heartbeat_interval_seconds == 20.0
    assert cfg.max_attempts == 5
    assert cfg.backoff_seconds == (60.0, 300.0, 1800.0, 7200.0, 21600.0)
    assert cfg.poll_interval_seconds == 1.0
    assert cfg.batch_size == 10


def test_config_invariants() -> None:
    with pytest.raises(ValueError):
        WorkQueueWorkerConfig(lease_ttl_seconds=10.0, heartbeat_interval_seconds=20.0)
    with pytest.raises(ValueError):
        WorkQueueWorkerConfig(max_attempts=0)
    with pytest.raises(ValueError):
        WorkQueueWorkerConfig(max_attempts=8)  # 退避表仅 5 档
    with pytest.raises(ValueError):
        WorkQueueWorkerConfig(batch_size=0)


# ── lane：同租户串行 / 跨租户并发（task 3.3） ──


async def test_same_tenant_is_serialized() -> None:
    """同一租户两条 work 不得并发执行（ADR-4/ADR-5）。"""
    repo = FakeWorkRepo([_item("i1", "t1"), _item("i2", "t1")])
    handler = RecordingHandler(delay=0.05)
    worker = _worker(repo, {"consolidation": handler})

    await worker.process_once()

    assert sorted(handler.executed) == ["i1", "i2"]
    assert handler.max_active == 1
    assert sorted(repo.succeeded) == ["i1", "i2"]


async def test_cross_tenant_runs_concurrently() -> None:
    """一个租户阻塞时，另一个租户仍可完成（跨租户并发）。"""
    repo = FakeWorkRepo([_item("a1", "ta"), _item("b1", "tb")])
    blocked = RecordingHandler()
    blocked.gate = asyncio.Event()
    fast = RecordingHandler()

    class PerTenantHandler(RecordingHandler):
        """同一 flow，但 ta 的 handler 阻塞、tb 的立即完成。"""

        async def execute(self, envelope: WorkItemEnvelope) -> object:
            if envelope.tenant_id == "ta":
                return await blocked.execute(envelope)
            return await fast.execute(envelope)

    worker = _worker(repo, {"consolidation": PerTenantHandler()})
    task = asyncio.create_task(worker.process_once())
    await asyncio.wait_for(fast.started.wait(), timeout=2)
    assert fast.executed == ["b1"], "另一个租户应先完成"
    assert blocked.executed == [], "ta 仍被阻塞"
    blocked.gate.set()
    assert await asyncio.wait_for(task, timeout=2) == 2
    assert sorted(blocked.executed + fast.executed) == ["a1", "b1"]


# ── 维护类延后（task 3.4 / ADR-5） ──


async def test_maintenance_deferred_releases_without_failing() -> None:
    """interactive 占优时维护类被延后 → release_for_retry（不算失败、不消耗预算）。"""
    repo = FakeWorkRepo(
        [
            _item("i1", "t1", work_kind="interactive", flow="consolidation"),
            _item("m1", "t1", work_kind="maintenance", flow="consolidation"),
        ]
    )
    handler = RecordingHandler()
    handler.gate = asyncio.Event()
    worker = _worker(
        repo,
        {"consolidation": handler},
        maintenance_acquire_timeout_seconds=0.05,
    )

    task = asyncio.create_task(worker.process_once())
    await asyncio.wait_for(handler.started.wait(), timeout=2)
    # 维护类在 acquire_timeout 后延后返回；interactive 仍持 lane
    for _ in range(200):
        if repo.released:
            break
        await asyncio.sleep(0.01)
    assert repo.released == [("m1", "maintenance 延后（interactive 占优）")]
    assert repo.failed == []
    handler.gate.set()
    assert await asyncio.wait_for(task, timeout=2) == 2
    assert repo.succeeded == ["i1"]


async def test_unregistered_flow_fails_loudly() -> None:
    """未注册 flow 不得执行；按业务失败记录（不静默丢弃、也不无限释放）。"""
    repo = FakeWorkRepo([_item("x1", "t1", flow="optimizer")])
    worker = _worker(repo, {"consolidation": RecordingHandler()})

    await worker.process_once()

    assert repo.succeeded == []
    assert len(repo.failed) == 1
    assert repo.failed[0][0] == "x1"
    assert "未注册 flow" in repo.failed[0][1]


# ── handler 两段式（task 3.2 / ADR-6） ──


async def test_handler_execute_outside_persist_inside_transaction() -> None:
    """长活在事务外，persist 收到终态事务的 session（ADR-6 提交侧）。"""
    repo = FakeWorkRepo([_item("i1", "t1")])
    handler = RecordingHandler()
    worker = _worker(repo, {"consolidation": handler})

    await worker.process_once()

    assert handler.executed == ["i1"]
    assert handler.persisted == [("i1", _SENTINEL_SESSION)]
    assert repo.succeeded == ["i1"]


async def test_execute_failure_counts_as_business_failure() -> None:
    repo = FakeWorkRepo([_item("i1", "t1")])
    worker = _worker(repo, {"consolidation": RecordingHandler(fail_in="execute")})

    await worker.process_once()

    assert len(repo.failed) == 1
    assert "execute 失败" in repo.failed[0][1]
    assert repo.succeeded == []


async def test_persist_failure_counts_as_business_failure() -> None:
    """persist 抛错 → 终态与副作用整体回滚 → 计入业务失败。"""
    repo = FakeWorkRepo([_item("i1", "t1")])
    worker = _worker(repo, {"consolidation": RecordingHandler(fail_in="persist")})

    await worker.process_once()

    assert len(repo.failed) == 1
    assert "persist 失败" in repo.failed[0][1]
    assert repo.succeeded == []


# ── 失租禁写（task 3.2 / ADR-4） ──


async def test_lease_lost_abandons_terminal_writes() -> None:
    """失租后不得写任何状态（成功或失败）。"""
    repo = FakeWorkRepo([_item("i1", "t1")])
    handler = RecordingHandler()
    handler.gate = asyncio.Event()
    worker = _worker(
        repo,
        {"consolidation": handler},
        heartbeat_interval_seconds=0.01,
        lease_ttl_seconds=0.5,
    )

    task = asyncio.create_task(worker.process_once())
    await asyncio.wait_for(handler.started.wait(), timeout=2)
    repo.lease_lost = True
    await asyncio.sleep(0.05)  # 让心跳跑一轮并标记失租
    handler.gate.set()
    assert await asyncio.wait_for(task, timeout=2) == 1

    assert repo.succeeded == []
    assert repo.failed == []


async def test_lease_lost_on_failure_abandons_state() -> None:
    """失租后即使 handler 抛错，也不得写入失败状态。

    注意：worker 是通过**下一轮心跳失败**得知租约丢失的（最长 heartbeat_interval
    之后），因此必须让心跳先跑一轮。
    """
    repo = FakeWorkRepo([_item("i1", "t1")])
    handler = RecordingHandler(fail_in="execute")
    handler.gate = asyncio.Event()
    worker = _worker(
        repo,
        {"consolidation": handler},
        heartbeat_interval_seconds=0.01,
        lease_ttl_seconds=0.5,
    )

    task = asyncio.create_task(worker.process_once())
    await asyncio.wait_for(handler.started.wait(), timeout=2)
    repo.lease_lost = True
    await asyncio.sleep(0.05)  # 让心跳跑一轮并标记失租
    handler.gate.set()  # 放行 execute → 抛错
    assert await asyncio.wait_for(task, timeout=2) == 1

    assert repo.failed == []


# ── 停止语义与背压（task 3.5 / task 3.6） ──


async def test_stop_closes_lane_and_stops_claiming() -> None:
    repo = FakeWorkRepo([_item("i1", "t1")])
    worker = _worker(repo, {"consolidation": RecordingHandler()})

    worker.stop()
    assert worker.router.closed is True
    assert await worker.process_once() == 0
    assert repo.claim_calls == [], "停止后不得再认领"


async def test_batch_size_is_passed_as_backpressure_bound() -> None:
    repo = FakeWorkRepo([_item(f"i{i}", f"t{i}") for i in range(10)])
    worker = _worker(repo, {"consolidation": RecordingHandler()}, batch_size=3)

    assert await worker.process_once() == 3
    assert repo.claim_calls[0]["batch_size"] == 3
    assert repo.claim_calls[0]["owner"] == "w1"
    assert repo.claim_calls[0]["lease_ttl"] == 60.0


# ── 轮询级异常隔离（ADR-7） ──


def test_config_error_backoff_invariants() -> None:
    with pytest.raises(ValueError):
        WorkQueueWorkerConfig(error_backoff_seconds=0)
    with pytest.raises(ValueError):
        WorkQueueWorkerConfig(
            error_backoff_seconds=10.0, max_error_backoff_seconds=1.0
        )
    cfg = WorkQueueWorkerConfig()
    assert cfg.error_backoff_seconds == 5.0
    assert cfg.max_error_backoff_seconds == 60.0


async def test_polling_error_does_not_escape_run() -> None:
    """DB 抖动（claim_batch 抛错）不得逃出 run()。

    `AppRuntime._run_primary_tasks` 把 runtime task 的异常当致命：一旦有任务抛错，
    同级任务会被全部取消并退出进程。因此轮询异常必须只记日志 + 退避重试。
    """
    repo = FakeWorkRepo()
    repo.claim_raises = 5  # 一直失败：run() 也不得退出或抛错
    worker = _worker(
        repo,
        {"consolidation": RecordingHandler()},
        poll_interval_seconds=0.01,
        error_backoff_seconds=0.01,
        max_error_backoff_seconds=0.02,
    )

    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.15)
    assert not task.done(), "run() 不应因轮询异常退出"
    assert repo.claim_attempts >= 2, "应退避后继续重试"

    worker.stop()
    await asyncio.wait_for(task, timeout=2)  # 正常收束，不抛
    assert task.exception() is None


async def test_polling_error_recovers_and_resumes_consuming() -> None:
    """轮询异常恢复后继续消费（不丢已就绪的 work）。"""
    repo = FakeWorkRepo([_item("i1", "t1")])
    repo.claim_raises = 2
    worker = _worker(
        repo,
        {"consolidation": RecordingHandler()},
        poll_interval_seconds=0.01,
        error_backoff_seconds=0.01,
        max_error_backoff_seconds=0.02,
    )

    task = asyncio.create_task(worker.run())
    for _ in range(300):
        if repo.succeeded:
            break
        await asyncio.sleep(0.01)
    worker.stop()
    await asyncio.wait_for(task, timeout=2)

    assert repo.claim_attempts >= 3, "两次失败后应重试成功"
    assert repo.succeeded == ["i1"]
