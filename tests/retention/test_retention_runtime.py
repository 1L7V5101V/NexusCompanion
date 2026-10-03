"""RetentionRuntime 周期壳 + app 装配 + 文件 sweep 边界（tasks 4.1/4.2/6.1/6.2）。"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.config_models import RetentionConfig
from bootstrap.retention import RetentionRuntime, RetentionSweeper

pytestmark = pytest.mark.postgres


@pytest.fixture
def sweeper(rt_factory: async_sessionmaker) -> RetentionSweeper:
    return RetentionSweeper(rt_factory, RetentionConfig())


def test_runtime_start_stop_lifecycle(sweeper: RetentionSweeper) -> None:
    """启停：start 建 task（name=retention_sweep），stop 干净收束。"""

    async def _scenario() -> None:
        runtime = RetentionRuntime(sweeper, interval_s=3600)
        runtime.start()
        assert runtime._task is not None
        assert runtime._task.get_name() == "retention_sweep"
        # 重复 start 幂等（不建第二个 task）。
        task = runtime._task
        runtime.start()
        assert runtime._task is task
        await runtime.stop()
        assert runtime._task is None
        assert task.cancelled() or task.done()

    asyncio.run(_scenario())


def test_startup_does_not_run_first_round(
    rt_reset: None, sweeper: RetentionSweeper, exec_sql: Callable[..., Any]
) -> None:
    """spec「启动不立即执行删除」：start 后首个 interval 到期前零删除。"""

    async def _scenario() -> None:
        runtime = RetentionRuntime(sweeper, interval_s=3600)
        runtime.start()
        try:
            await asyncio.sleep(0.3)
            rows = exec_sql("SELECT count(*) FROM tool_audit_events")[0][0]
            assert rows == 0  # 无首轮删除风暴（DB 里也无异常写入）
            assert not runtime._task.done()
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_runtime_loop_runs_after_interval_and_isolates_exceptions(
    rt_reset: None, rt_factory: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """周期到点后执行；单轮异常被隔离（记录后下一轮继续）。"""

    async def _scenario() -> None:
        calls = {"n": 0}

        async def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls["n"] += 1
            raise RuntimeError("boom round")

        sweeper = RetentionSweeper(rt_factory, RetentionConfig())
        monkeypatch.setattr(sweeper, "run_once", _boom)
        runtime = RetentionRuntime(sweeper, interval_s=1)  # 秒级等待，测试可承受
        runtime.start()
        try:
            await asyncio.sleep(2.5)
            assert calls["n"] >= 2  # 第一轮炸了，第二轮照常触发
            assert not runtime._task.done()
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_run_once_dry_run_entry(sweeper: RetentionSweeper) -> None:
    """run_once(dry_run=True) 演练入口：默认零变更、可产出报告。"""

    async def _scenario() -> None:
        report = await RetentionRuntime(sweeper, interval_s=3600).run_once()
        assert report.dry_run is True
        assert report.ok is True

    asyncio.run(_scenario())


def test_sweeper_entity_exception_isolation(
    rt_reset: None, rt_factory: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-3 单实体异常隔离：一个实体失败，其余实体继续产出报告。"""

    async def _scenario() -> None:
        sweeper = RetentionSweeper(rt_factory, RetentionConfig())

        async def _boom(*args: Any, **kwargs: Any) -> tuple[int, int]:
            raise RuntimeError("audit path broken")

        monkeypatch.setattr(sweeper._tool_audit, "delete_expired_audit_batch", _boom)
        report = await sweeper.run_once(dry_run=False)
        tool = next(e for e in report.entities if e.target == "tool_audit_events")
        assert tool.errors  # 失败被记录
        admin = next(e for e in report.entities if e.target == "admin_audit_events")
        assert admin.errors == []  # 其余实体不受阻断
        assert report.ok is False

    asyncio.run(_scenario())


# ── 4.2 app 装配 ─────────────────────────────────────────────


def test_app_wiring_gates_and_shutdown_step() -> None:
    """装配门禁：enabled=false 不建 task；interval_s=0 只建 runtime 不起循环；
    shutdown 步骤序列含 retention（grep 级接线守卫）。"""
    from bootstrap.app import AppRuntime

    import bootstrap.app as app_module

    source = Path(app_module.__file__ or "").read_text(encoding="utf-8")
    assert '("retention.stop", self._stop_retention_runtime)' in source
    assert "config.retention.enabled" in source
    assert 'name="retention_sweep"' in Path(
        *["bootstrap", "retention", "runtime.py"]
    ).read_text(encoding="utf-8")

    async def _scenario() -> None:
        factory = SimpleNamespace()  # 装配只传引用，不触库
        enabled_cfg = RetentionConfig(enabled=True, interval_s=3600)
        stub = SimpleNamespace(
            webchat_durable=SimpleNamespace(session_factory=factory),
            config=SimpleNamespace(retention=enabled_cfg),
            retention_runtime=None,
        )
        AppRuntime._start_retention_runtime(stub)
        assert stub.retention_runtime is not None
        assert stub.retention_runtime._task is not None  # enabled + interval>0 → 起 task
        await AppRuntime._stop_retention_runtime(stub)
        assert stub.retention_runtime is None

        # interval_s=0：runtime 保留（run_once 演练入口可用），周期循环不起。
        stub2 = SimpleNamespace(
            webchat_durable=SimpleNamespace(session_factory=factory),
            config=SimpleNamespace(retention=RetentionConfig(interval_s=0)),
            retention_runtime=None,
        )
        AppRuntime._start_retention_runtime(stub2)
        assert stub2.retention_runtime is not None
        assert stub2.retention_runtime._task is None
        await AppRuntime._stop_retention_runtime(stub2)

    asyncio.run(_scenario())


# ── 6.1/6.2 文件 sweep 边界 ──────────────────────────────────


def _make_sharded_dir(tmp_path: Path) -> tuple[Path, Path, Path]:
    """按日期分片的观测目录：一片超龄、一片新鲜。"""
    root = tmp_path / "shards"
    root.mkdir()
    old = root / "2026-08-01.ndjson"
    new = root / "2026-10-01.ndjson"
    old.write_text("old shard\n", encoding="utf-8")
    new.write_text("new shard\n", encoding="utf-8")
    old_time = time.time() - 60 * 86400  # 60 天前
    os.utime(old, (old_time, old_time))
    return root, old, new


def test_file_roots_empty_reports_zero_scanned(
    rt_reset: None, sweeper: RetentionSweeper
) -> None:
    """task 6.1：未配置 root 时文件腿报告 scanned=0（存在但零动作）。"""

    async def _scenario() -> None:
        report = await sweeper.run_once(dry_run=False)
        files = [e for e in report.entities if e.category == "files"]
        assert len(files) == 1
        assert files[0].scanned == 0
        assert files[0].deleted == 0
        assert files[0].target == "(no file_roots configured)"

    asyncio.run(_scenario())


def test_file_roots_delete_only_overaged_shards(
    rt_reset: None, rt_factory: async_sessionmaker, tmp_path: Path
) -> None:
    """spec「分片产物按档过期」：仅超龄分片被删，报告计数与磁盘一致；dry-run 零变更。"""
    root, old, new = _make_sharded_dir(tmp_path)
    cfg = RetentionConfig(file_roots={"operational": [str(root)]})
    sweeper = RetentionSweeper(rt_factory, cfg)

    async def _scenario() -> None:
        dry = await sweeper.run_once(dry_run=True)
        file_dry = next(e for e in dry.entities if e.target == str(root))
        assert file_dry.deleted == 1
        assert old.exists() and new.exists()  # 演练零变更

        live = await sweeper.run_once(dry_run=False)
        file_live = next(e for e in live.entities if e.target == str(root))
        assert file_live.scanned == 2
        assert file_live.deleted == 1
        assert file_live.kept == 1
        assert file_live.bytes_freed > 0
        assert not old.exists()
        assert new.exists()

    asyncio.run(_scenario())


def test_active_sqlite_file_not_in_roots_untouched(
    rt_reset: None, sweeper: RetentionSweeper, tmp_path: Path, exec_sql: Callable[..., Any]
) -> None:
    """task 6.2 / spec「活动数据库不被当作过期文件删除」：
    内置 root 集为空 —— workspace/logs 活跃文件不配 root 就绝不被删。"""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    db_file = logs_dir / "passive.db"
    db_file.write_text("active sqlite", encoding="utf-8")
    old_time = time.time() - 400 * 86400
    os.utime(db_file, (old_time, old_time))  # mtime 远超任何窗口

    async def _scenario() -> None:
        report = await sweeper.run_once(dry_run=False)  # file_roots 为空
        assert db_file.exists()
        assert all(str(logs_dir) not in e.target for e in report.entities)

    asyncio.run(_scenario())
